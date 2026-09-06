"""The shared on-disk cache: atomic writes, TTLs, negative entries, single-flight."""

import contextlib
import copy
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import _singleflight, _stats, config


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


# Default cache root lives next to the project unless CACHE_DIR overrides it.
#
# Growth: the cache has NO global size or count bound. The only eviction is
# on-read TTL (``get(..., max_age_seconds=N)`` unlinks over-age entries); the
# orphan ``.tmp`` sweep below bounds leakage, not size. Entries that are
# never re-read with a ``max_age_seconds`` therefore persist indefinitely;
# operators prune ``.cache/`` manually if it grows too large.
_CACHE_ROOT = _resolve_cache_root()

# Default TTL for negative-cache entries. 24 hours is long enough to absorb
# burst retries on a known-bad identifier (and the agent's likely "let me
# try a few variations" flow) while short enough that a newly-registered
# DOI surfaces within a day.
_DEFAULT_NEG_TTL_SECONDS = 86400.0

# Sibling subdirectory under each entity holds negative entries. Keeping
# positive and negative state in separate trees means a corrupt /
# expired negative can never be misread as a positive.
_NEG_SUBDIR = "_neg"


def cache_dir(namespace: str, entity: str) -> Path:
    """Return the cache directory for a given namespace and entity type.

    e.g., namespace="openalex", entity="works" -> .cache/openalex/works/

    Public because the PDF-handling modules (``providers/*.download_pdf``,
    ``manual``, ``papers``) build canonical file paths under it — a cross-module
    use that shouldn't reach for a leading-underscore name.
    """
    return _CACHE_ROOT / namespace / entity


def _cache_key(identifier: str) -> str:
    """Generate a safe filename from an arbitrary identifier."""
    # Use a hash to avoid filesystem issues with special chars in DOIs, URLs, etc.
    return hashlib.sha256(identifier.encode()).hexdigest()


def get(
    namespace: str,
    entity: str,
    identifier: str,
    *,
    max_age_seconds: float | None = None,
    count: bool = True,
) -> dict[str, Any] | None:
    """Retrieve a cached response. Returns None on miss or corruption.

    ``count=False`` suppresses the hit/miss counters. Use it wherever the call
    is not a real lookup: ``cached_lookup``'s in-slot re-check (which would
    otherwise register a second miss for the same lookup, so a genuine miss
    counted twice while a hit counted once — making the reported hit rate
    systematically wrong), and the search cache-warming probes, which call
    ``get`` only to decide whether to overwrite.

    ``max_age_seconds`` (optional) treats entries older than that many
    seconds (by file mtime) as misses, and unlinks them so the next put
    writes cleanly. Use it on data that drifts over time (citation counts,
    bioRxiv published_doi appearing, OpenCitations graph growing). Omit
    it for data that's effectively immutable once written.

    A corrupt cache file (e.g. a truncated JSON left behind by a process
    killed mid-write before atomic writes existed, or external tampering)
    self-heals: the bad file is unlinked and None is returned so the next
    put() writes a clean value.
    """
    path = cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"
    if not path.exists():
        if count:
            _stats.incr(namespace, "cache_misses")
        return None
    if max_age_seconds is not None:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            if count:
                _stats.incr(namespace, "cache_misses")
            return None
        if age > max_age_seconds:
            with contextlib.suppress(OSError):
                path.unlink()
            if count:
                _stats.incr(namespace, "cache_misses")
            return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        with contextlib.suppress(OSError):
            path.unlink()
        if count:
            _stats.incr(namespace, "cache_misses")
        return None
    # Entries are always dicts (see put()'s signature). Anything else is a
    # tampered or foreign file — treat it like corruption rather than handing
    # back a value that violates this function's return contract.
    if not isinstance(data, dict):
        with contextlib.suppress(OSError):
            path.unlink()
        if count:
            _stats.incr(namespace, "cache_misses")
        return None
    if count:
        _stats.incr(namespace, "cache_hits")
    return data


# Files older than this are considered orphans of a long-dead writer.
# 1h is well past any legitimate write (atomic mkstemp -> os.replace
# completes in milliseconds) and short enough that an operator
# noticing leakage doesn't have to wait a day for the sweep to act.
_ORPHAN_TMP_AGE_SECONDS = 3600.0


def gc_orphan_tmp_files(*, max_age_seconds: float = _ORPHAN_TMP_AGE_SECONDS) -> int:
    """Sweep ``.cache/`` for stale ``*.tmp`` files left behind by killed writers.

    ``_atomic_write_json`` lands in a sibling temp file via ``mkstemp``
    and renames into place — a process killed mid-write before the
    rename leaves the temp behind. The temp itself is harmless (a
    self-healing read on the canonical entry won't read it), but they
    accumulate forever without intervention. Called from the FastMCP
    lifespan startup so each server restart cleans up after the
    previous run's untimely deaths.

    Returns the number of files unlinked. Idempotent; safe to call any
    time. Files newer than ``max_age_seconds`` are skipped so we never
    race a live writer (which holds the file open for milliseconds).
    """
    if not _CACHE_ROOT.exists():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in _CACHE_ROOT.rglob("*.tmp"):
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


def invalidate(namespace: str, entity: str, identifier: str) -> None:
    """Drop both positive and negative cache entries for an identifier.

    Used by ``force_refresh=True`` on the unified paper tools. Idempotent:
    missing files are silently skipped. Both halves are unlinked together
    so a forced refresh of a previously-404'd identifier doesn't keep
    serving the cached error.
    """
    pos = cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"
    neg = _neg_path(namespace, entity, identifier)
    for p in (pos, neg):
        # FileNotFoundError is an OSError; suppressing the base class covers
        # the absent-file case and every other unlink failure alike.
        with contextlib.suppress(OSError):
            p.unlink(missing_ok=True)


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

    Every provider getter (``arxiv.get_paper``, ``openalex.get_work``, …) shares
    this exact shape; it lives here once so the subtle ordering can't drift:

      1. ``force_refresh`` -> ``invalidate`` both cache halves, then always fetch.
      2. Otherwise check the positive cache (TTL-aware) then the negative cache
         and short-circuit on a hit.
      3. Coalesce concurrent callers for ``sf_key`` (default ``canonical``) via
         single-flight; **inside** the slot, re-check both caches first so a
         follower picks up the leader's just-written entry instead of re-fetching.

    ``fetch`` owns its own caching: it decides whether to ``put`` (positive),
    ``put_negative`` (with whatever TTL), or cache nothing (transient parse
    errors). It is only called on a genuine miss. Provider-specific quirks
    (bioRxiv's medRxiv fallback, arxiv's three not-found shapes, the 404 branch)
    all live in that closure.

    Each caller receives an independent deep copy of the result: in-batch
    single-flight followers share the leader's object, so without the copy a
    caller mutating its result would corrupt the others' (and the cached dict
    a follower's inner-recheck handed back).
    """
    if force_refresh:
        invalidate(namespace, entity, canonical)
    else:
        cached = get(namespace, entity, canonical, max_age_seconds=positive_ttl)
        if cached is not None:
            return cached
        neg = get_negative(namespace, entity, canonical)
        if neg is not None:
            return neg

    async def _runner() -> dict[str, Any]:
        # Re-check inside the slot: a leader for this key may have populated the
        # cache while a follower's coroutine was suspended at the outer check.
        # On a forced refresh we still re-check so concurrent forced callers
        # share one fetch (the leader writes, followers see the fresh entry).
        # count=False: the outer check above already recorded this lookup's
        # hit/miss. Counting again made every genuine miss register twice.
        cached = get(namespace, entity, canonical, max_age_seconds=positive_ttl, count=False)
        if cached is not None:
            return cached
        neg = get_negative(namespace, entity, canonical)
        if neg is not None:
            return neg
        return await fetch()

    result = await single_flight.do(sf_key if sf_key is not None else canonical, _runner)
    return copy.deepcopy(result)


def _atomic_write_text(path: Path, payload: str, *, encoding: str = "utf-8") -> None:
    """Write text ``payload`` to ``path`` with no torn/partial canonical writes.

    Lands in a sibling temp file and is moved into place with os.replace,
    which is atomic on POSIX/Windows — a concurrent or interrupted writer
    can never expose a half-written canonical file to a reader. Best-effort
    cleanup of the temp file on any failure path, including KeyboardInterrupt
    mid-write. mkstemp lands in the same directory so the rename stays on one
    filesystem (cross-fs rename is not atomic and would raise EXDEV). The
    encoding is explicit (UTF-8 by default) so a non-UTF-8 host locale
    (e.g. ``LC_ALL=C``) can't mangle or fail on non-ASCII content.

    This is NOT a crash-durability guarantee: os.replace orders the rename,
    not the data flush, so a power loss could leave the rename durable while
    the file contents are not. We deliberately skip fsync — the cost on every
    write isn't worth it for a cache whose reads self-heal on corruption.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(directory),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _atomic_write_json(path: Path, payload: str) -> None:
    """Write a JSON ``payload`` to ``path`` atomically.

    Thin UTF-8 wrapper over :func:`_atomic_write_text` — see it for the
    torn-write / durability semantics.
    """
    _atomic_write_text(path, payload)


def _atomic_copy(src: Path, dst: Path) -> None:
    """Copy ``src`` onto ``dst`` with no torn/partial canonical writes.

    Streams the bytes through a sibling temp file in ``dst``'s directory and
    os.replaces into place on success — the same atomicity guarantee as
    :func:`_atomic_write_text`, but for arbitrary (potentially large) binary
    files like PDFs. A plain ``shutil.copy``/``copy2`` straight to ``dst``
    truncates and writes the canonical path in place, so a crash / disk-full
    mid-copy would leave a half-written file that downstream readers treat as
    complete. Copies file metadata (mtime/mode) like ``copy2``. Best-effort
    temp cleanup on any failure path. mkstemp lands beside ``dst`` so the
    rename stays on one filesystem.
    """
    directory = dst.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=dst.name + ".",
        suffix=".tmp",
        dir=str(directory),
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out)
        shutil.copystat(src, tmp_path)
        os.replace(tmp_path, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def put(namespace: str, entity: str, identifier: str, data: dict[str, Any]) -> bool:
    """Store a response in the cache. Atomic via ``_atomic_write_json``.

    Returns whether the write landed. **A failed write is not an error the
    caller should propagate**: every provider calls this from inside its
    ``fetch`` closure *after* the network request already succeeded, so
    raising on a full or read-only disk threw away data we had just paid an
    HTTP request for, and surfaced as an uncaught ``OSError`` out of an MCP
    tool rather than the ``{error}`` contract. Serving the response uncached
    is strictly better: the agent gets its answer and the next call simply
    re-fetches.

    Genuine programming errors still propagate — only ``OSError`` is
    absorbed, and the failure is counted so an operator can see it.
    """
    final_path = cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        _atomic_write_json(final_path, payload)
    except OSError:
        # ENOSPC, EROFS, EACCES, EDQUOT, a name too long...
        _stats.incr(namespace, "cache_write_failures")
        return False
    return True


# ---------------------------------------------------------------------------
# Negative cache (TTL-bounded)
# ---------------------------------------------------------------------------

# A "negative" entry records that the upstream definitively returned
# "not found" (HTTP 404 or its API-specific equivalent). It's NOT for
# transient failures — those just retried via _http.get_with_retry and
# either succeeded or surfaced as a retryable error to the agent.


def _neg_path(namespace: str, entity: str, identifier: str) -> Path:
    return cache_dir(namespace, entity) / _NEG_SUBDIR / f"{_cache_key(identifier)}.json"


def get_negative(namespace: str, entity: str, identifier: str) -> dict[str, Any] | None:
    """Return the cached negative result if present and unexpired, else None.

    The returned dict is the original error payload — the ``_expires_at``
    bookkeeping field is stripped, so the caller can return it as-is and
    the agent sees the same shape it would for a fresh 404.

    Self-heals: an expired or corrupt entry is unlinked on read so the
    next call gets a clean miss.
    """
    path = _neg_path(namespace, entity, identifier)
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    # A non-dict entry (tampering / foreign writer) would crash the
    # _expires_at lookup below — self-heal instead.
    if not isinstance(entry, dict):
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    expires_at = entry.get("_expires_at", 0)
    if not isinstance(expires_at, (int, float)) or expires_at < time.time():
        with contextlib.suppress(OSError):
            path.unlink()
        return None
    _stats.incr(namespace, "negative_hits")
    # Strip only our own bookkeeping key — caller payload keys that happen to
    # start with '_' (e.g. _canonical_id) must round-trip untouched.
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
    """Store a negative result with a TTL. Atomic via ``_atomic_write_json``.

    ``data`` should be the error payload the caller would otherwise return
    directly (e.g. ``{"error": "No paper found for arXiv ID: X"}``). An
    ``_expires_at`` field is added; everything else is preserved verbatim.

    Returns whether the write landed. Like ``put``, a write failure is
    absorbed rather than raised: the caller is about to return a perfectly
    good "not found" and should not turn that into an exception because the
    disk is full. The only cost of not caching it is a repeat lookup.
    """
    final_path = _neg_path(namespace, entity, identifier)
    entry = {**data, "_expires_at": time.time() + ttl_seconds}
    payload = json.dumps(entry, ensure_ascii=False, indent=2)
    try:
        _atomic_write_json(final_path, payload)
    except OSError:
        _stats.incr(namespace, "cache_write_failures")
        return False
    return True
