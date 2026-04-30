import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

# Default cache root lives next to the project
_CACHE_ROOT = Path(__file__).resolve().parent.parent.parent / ".cache"

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


def get(namespace: str, entity: str, identifier: str) -> dict[str, Any] | None:
    """Retrieve a cached response. Returns None on miss or corruption.

    A corrupt cache file (e.g. a truncated JSON left behind by a process
    killed mid-write before atomic writes existed, or external tampering)
    self-heals: the bad file is unlinked and None is returned so the next
    put() writes a clean value.
    """
    path = _cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _atomic_write_json(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` atomically.

    Lands in a sibling temp file and is moved into place with os.replace,
    which is atomic on POSIX/Windows. Best-effort cleanup of the temp
    file on any failure path, including KeyboardInterrupt mid-write.
    mkstemp lands in the same directory so the rename stays on one
    filesystem (cross-fs rename is not atomic and would raise EXDEV).
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
        entry = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
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
    return {k: v for k, v in entry.items() if not k.startswith("_")}


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
