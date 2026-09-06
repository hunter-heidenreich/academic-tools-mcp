"""The shared on-disk cache: atomic writes, TTLs, negative entries, single-flight."""

import contextlib
import copy
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import _singleflight, _stats, atomic, config


def _resolve_cache_root() -> Path:
    """Resolve the on-disk cache root.

    Honours the ``CACHE_DIR`` env var (so an installed wheel, where the
    project tree isn't writable, can point the cache somewhere sensible);
    otherwise defaults to ``.cache`` next to the project.
    """
    configured = config.get("CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent.parent / ".cache"


# Bound at import, so CACHE_DIR must be set before it. No size bound: the only
# eviction is on-read TTL.
CACHE_ROOT = _resolve_cache_root()

# Absorbs burst retries on a known-bad identifier; still surfaces a
# newly-registered DOI within a day.
_DEFAULT_NEG_TTL_SECONDS = 86400.0

# Negatives live in their own tree, so a corrupt or expired one can never be
# misread as a positive.
_NEG_SUBDIR = "_neg"


# Well past any legitimate write (mkstemp -> os.replace takes milliseconds),
# short enough that an operator watching leakage doesn't wait a day.
_ORPHAN_TMP_AGE_SECONDS = 3600.0


def cache_dir(namespace: str, entity: str) -> Path:
    """Build the directory for a namespace/entity pair. Creates nothing.

    e.g., namespace="openalex", entity="works" -> .cache/openalex/works/

    Public because the PDF and markdown path builders live in other modules.
    Reads ``CACHE_ROOT`` per call — the seam tests redirect. Don't capture it.
    """
    return CACHE_ROOT / namespace / entity


def _cache_key(identifier: str) -> str:
    """Hash an arbitrary identifier into a safe, exact filename.

    Exact, never normalizing: canonicalize before calling (``_doi.canonical``).
    Hashing also keeps case-variant identifiers apart on a case-insensitive
    filesystem, so macOS and Linux agree on what is one entry.
    """
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def _entry_path(namespace: str, entity: str, identifier: str) -> Path:
    return cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"


def _neg_path(namespace: str, entity: str, identifier: str) -> Path:
    return cache_dir(namespace, entity) / _NEG_SUBDIR / f"{_cache_key(identifier)}.json"


def _unlink_quietly(path: Path) -> None:
    """Best-effort unlink. FileNotFoundError is an OSError, so absent is fine."""
    with contextlib.suppress(OSError):
        path.unlink()


def _read_entry(path: Path, *, max_age_seconds: float | None = None) -> dict[str, Any] | None:
    """Read a cache file, or heal it away. The one home for that bargain.

    Returns None for anything that is not a live, well-formed dict — over-age
    by mtime, unreadable, not JSON, not a dict — unlinking the file in every
    case but "absent", so the next put writes cleanly. Skipping fsync on write
    is only survivable because every read comes through here.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if max_age_seconds is not None and time.time() - mtime > max_age_seconds:
        _unlink_quietly(path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        _unlink_quietly(path)
        return None
    # Entries are always dicts (see put()'s signature). Anything else is a
    # tampered or foreign file, not a hit.
    if not isinstance(data, dict):
        _unlink_quietly(path)
        return None
    return data


def _write_entry(namespace: str, path: Path, entry: dict[str, Any]) -> bool:
    """Serialise ``entry`` to ``path`` atomically; report whether it landed.

    ``indent=2`` trades bytes for a record an operator can read unaided. Only
    ``OSError`` is absorbed — an unserialisable payload is a programming error.
    """
    payload = json.dumps(entry, ensure_ascii=False, indent=2)
    try:
        atomic.write_text(path, payload)
    except OSError:
        # ENOSPC, EROFS, EACCES, EDQUOT, a name too long...
        _stats.incr(namespace, "cache_write_failures")
        return False
    return True


def get(
    namespace: str,
    entity: str,
    identifier: str,
    *,
    max_age_seconds: float | None = None,
    count: bool = True,
) -> dict[str, Any] | None:
    """Retrieve a cached response, or None if there isn't a live one.

    ``max_age_seconds`` treats entries older than that (by mtime) as absent;
    exactly that age still serves. Pass it for data that drifts — citation
    counts, bioRxiv's late ``published_doi``, the OpenCitations graph — and omit
    it for data that is immutable once written.

    Only a serve moves a counter, and only ``cache_hits``: ``cache_misses``
    means "went upstream", which the fetch side books. ``count=False`` for reads
    that aren't a lookup being served — the search cache-warming probes.
    """
    data = _read_entry(_entry_path(namespace, entity, identifier), max_age_seconds=max_age_seconds)
    if count and data is not None:
        _stats.incr(namespace, "cache_hits")
    return data


def put(namespace: str, entity: str, identifier: str, data: dict[str, Any]) -> bool:
    """Store a response in the cache.

    Returns whether the write landed. Never propagate ``False``: the network
    response is already paid for, and serving it uncached costs one re-fetch.
    """
    return _write_entry(namespace, _entry_path(namespace, entity, identifier), data)


# ---------------------------------------------------------------------------
# Negative cache (TTL-bounded)
# ---------------------------------------------------------------------------

# A negative entry records a *definitive* "not found" — HTTP 404 or a
# provider's equivalent. Never a transient failure: those stay retryable.


def get_negative(namespace: str, entity: str, identifier: str) -> dict[str, Any] | None:
    """Return the cached negative result if present and unexpired, else None.

    The caller can return it as-is: ``_expires_at`` is stripped, so the agent
    sees the shape it would get from a fresh 404. Expired entries unlink on
    read; :func:`_read_entry` handles the rest.
    """
    path = _neg_path(namespace, entity, identifier)
    entry = _read_entry(path)
    if entry is None:
        return None
    # Exactly at expiry is still live, matching get()'s `age > max_age` boundary.
    expires_at = entry.get("_expires_at", 0)
    if not isinstance(expires_at, (int, float)) or expires_at < time.time():
        _unlink_quietly(path)
        return None
    _stats.incr(namespace, "negative_hits")
    # Only our own key: caller keys like _canonical_id must round-trip.
    entry.pop("_expires_at", None)
    return entry


def put_negative(
    namespace: str,
    entity: str,
    identifier: str,
    data: dict[str, Any],
    *,
    ttl_seconds: float = _DEFAULT_NEG_TTL_SECONDS,
) -> bool:
    """Store a negative result with a TTL. Returns whether the write landed.

    ``data`` is the error payload the caller would otherwise return directly
    (``{"error": "No paper found for arXiv ID: X"}``). Every key round-trips
    except ``_expires_at``, which is reserved: added here, stripped on read.
    """
    entry = {**data, "_expires_at": time.time() + ttl_seconds}
    return _write_entry(namespace, _neg_path(namespace, entity, identifier), entry)


def invalidate(namespace: str, entity: str, identifier: str) -> None:
    """Drop both cache halves for one identifier.

    Both together, so a forced refresh of a previously-404'd identifier stops
    serving the cached error. Also the way a stale sections index is dropped
    when a paper's markdown is replaced.
    """
    _unlink_quietly(_entry_path(namespace, entity, identifier))
    _unlink_quietly(_neg_path(namespace, entity, identifier))


async def cached_lookup(
    *,
    single_flight: "_singleflight.SingleFlight",
    namespace: str,
    entity: str,
    canonical: str,
    positive_ttl: float,
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    force_refresh: bool = False,
    sf_key: Any = None,
) -> dict[str, Any]:
    """Run the shared cached-getter protocol around a provider's ``fetch``.

    Every provider getter shares this shape; it lives here once so the ordering
    can't drift:

      1. ``force_refresh`` -> ``invalidate`` both halves, then always fetch.
      2. Otherwise short-circuit on a positive (TTL-aware) or negative hit.
      3. Coalesce concurrent callers for ``sf_key`` (default ``canonical``) via
         single-flight, re-checking **inside** the slot.

    Only whoever runs the factory sees that re-check — plain followers await the
    leader's future — so it pays off when a caller is promoted to leader after
    the previous one was cancelled mid-write, and when concurrent forced
    refreshes share a single fetch.

    ``fetch`` is called only on a genuine miss and owns its own caching: ``put``,
    ``put_negative`` with whatever TTL, or nothing at all for a transient error.
    Provider quirks (bioRxiv's medRxiv fallback, arxiv's three not-found shapes)
    live in that closure.

    Each caller gets an independent deep copy — single-flight followers share the
    leader's object, and the in-slot re-check hands the same dict to each.
    """

    def short_circuit() -> dict[str, Any] | None:
        cached = get(namespace, entity, canonical, max_age_seconds=positive_ttl)
        if cached is not None:
            return cached
        return get_negative(namespace, entity, canonical)

    if force_refresh:
        invalidate(namespace, entity, canonical)
    else:
        early = short_circuit()
        if early is not None:
            return early

    async def _runner() -> dict[str, Any]:
        hit = short_circuit()
        if hit is not None:
            return hit
        _stats.incr(namespace, "cache_misses")
        return await fetch()

    result = await single_flight.do(sf_key if sf_key is not None else canonical, _runner)
    return copy.deepcopy(result)


def gc_orphan_tmp_files(*, max_age_seconds: float = _ORPHAN_TMP_AGE_SECONDS) -> int:
    """Sweep ``.cache/`` for stale ``*.tmp`` files left behind by killed writers.

    Covers all three writers that land under the cache root — ``atomic.write_text``,
    ``atomic.copy``, and ``_pdf_download.stream_to_file``, whose orphans are whole
    PDFs. A stranded temp is harmless (no read path looks at one) but nothing else
    removes it, so the FastMCP lifespan calls this and each restart clears the
    previous run's.

    Returns the number unlinked; idempotent. Skips files newer than
    ``max_age_seconds``, so a live writer is never raced; exactly that age is swept.
    """
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in CACHE_ROOT.rglob("*.tmp"):
        try:
            if path.stat().st_mtime > cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError:
            # Concurrent unlink, permissions, race with a writer — all
            # benign; the next sweep will pick it up if needed.
            continue
    return removed
