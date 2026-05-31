import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import _stats, config


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
# Growth: the cache has NO global size or count bound. Bounding happens two
# ways only — on-read TTL eviction (``get(..., max_age_seconds=N)`` unlinks
# over-age entries) and the orphan ``.tmp`` sweep below. Entries that are
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


def _cache_dir(namespace: str, entity: str) -> Path:
    """Return the cache directory for a given namespace and entity type.

    e.g., namespace="openalex", entity="works" -> .cache/openalex/works/
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
) -> dict[str, Any] | None:
    """Retrieve a cached response. Returns None on miss or corruption.

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
    path = _cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"
    if not path.exists():
        _stats.incr(namespace, "cache_misses")
        return None
    if max_age_seconds is not None:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            _stats.incr(namespace, "cache_misses")
            return None
        if age > max_age_seconds:
            try:
                path.unlink()
            except OSError:
                pass
            _stats.incr(namespace, "cache_misses")
            return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        try:
            path.unlink()
        except OSError:
            pass
        _stats.incr(namespace, "cache_misses")
        return None
    # Entries are always dicts (see put()'s signature). Anything else is a
    # tampered or foreign file — treat it like corruption rather than handing
    # back a value that violates this function's return contract.
    if not isinstance(data, dict):
        try:
            path.unlink()
        except OSError:
            pass
        _stats.incr(namespace, "cache_misses")
        return None
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
    pos = _cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"
    neg = _neg_path(namespace, entity, identifier)
    for p in (pos, neg):
        try:
            p.unlink()
        except (FileNotFoundError, OSError):
            pass


def _atomic_write_json(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` with no torn/partial canonical writes.

    Lands in a sibling temp file and is moved into place with os.replace,
    which is atomic on POSIX/Windows — a concurrent or interrupted writer
    can never expose a half-written canonical file to a reader. Best-effort
    cleanup of the temp file on any failure path, including KeyboardInterrupt
    mid-write. mkstemp lands in the same directory so the rename stays on one
    filesystem (cross-fs rename is not atomic and would raise EXDEV).

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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def put(namespace: str, entity: str, identifier: str, data: dict[str, Any]) -> None:
    """Store a response in the cache. Atomic via _atomic_write_json."""
    final_path = _cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    _atomic_write_json(final_path, payload)


def has(namespace: str, entity: str, identifier: str) -> bool:
    """Check if a cached response exists."""
    path = _cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"
    return path.exists()


# ---------------------------------------------------------------------------
# Negative cache (TTL-bounded)
# ---------------------------------------------------------------------------

# A "negative" entry records that the upstream definitively returned
# "not found" (HTTP 404 or its API-specific equivalent). It's NOT for
# transient failures — those just retried via _http.get_with_retry and
# either succeeded or surfaced as a retryable error to the agent.


def _neg_path(namespace: str, entity: str, identifier: str) -> Path:
    return _cache_dir(namespace, entity) / _NEG_SUBDIR / f"{_cache_key(identifier)}.json"


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
        try:
            path.unlink()
        except OSError:
            pass
        return None
    # A non-dict entry (tampering / foreign writer) would crash the
    # _expires_at lookup below — self-heal instead.
    if not isinstance(entry, dict):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    expires_at = entry.get("_expires_at", 0)
    if not isinstance(expires_at, (int, float)) or expires_at < time.time():
        try:
            path.unlink()
        except OSError:
            pass
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
) -> None:
    """Store a negative result with a TTL. Atomic via _atomic_write_json.

    ``data`` should be the error payload the caller would otherwise return
    directly (e.g. ``{"error": "No paper found for arXiv ID: X"}``). An
    ``_expires_at`` field is added; everything else is preserved verbatim.
    """
    final_path = _neg_path(namespace, entity, identifier)
    entry = {**data, "_expires_at": time.time() + ttl_seconds}
    payload = json.dumps(entry, ensure_ascii=False, indent=2)
    _atomic_write_json(final_path, payload)
