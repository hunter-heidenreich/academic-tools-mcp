import hashlib
import json
from pathlib import Path
from typing import Any

# Default cache root lives next to the project
_CACHE_ROOT = Path(__file__).resolve().parent.parent.parent / ".cache"


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
    """Retrieve a cached response. Returns None on cache miss."""
    path = _cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def put(namespace: str, entity: str, identifier: str, data: dict[str, Any]) -> None:
    """Store a response in the cache."""
    directory = _cache_dir(namespace, entity)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_cache_key(identifier)}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def has(namespace: str, entity: str, identifier: str) -> bool:
    """Check if a cached response exists."""
    path = _cache_dir(namespace, entity) / f"{_cache_key(identifier)}.json"
    return path.exists()
