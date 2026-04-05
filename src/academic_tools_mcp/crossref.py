import asyncio
import time
from typing import Any

import httpx

from . import cache, config

CROSSREF_BASE_URL = "https://api.crossref.org"
NAMESPACE = "crossref"

# Rate limiting for polite pool: max 10 req/sec, 3 concurrent.
# We enforce a conservative 100ms minimum gap between requests.
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 0.1  # 100ms -> ~10 req/sec max


async def _throttled_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a GET request respecting Crossref's rate limit.

    Enforces: max 1 concurrent request, minimum 100ms gap between requests.
    With the polite pool (mailto header), Crossref allows 10 req/sec.
    """
    global _last_request_time
    async with _request_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if _last_request_time > 0 and elapsed < _MIN_REQUEST_GAP:
            await asyncio.sleep(_MIN_REQUEST_GAP - elapsed)
        response = await client.get(url, **kwargs)
        _last_request_time = time.monotonic()
        return response


def _build_headers() -> dict[str, str]:
    """Build request headers with polite pool mailto if configured."""
    headers: dict[str, str] = {}
    mailto = config.get("CROSSREF_MAILTO")
    if mailto:
        headers["User-Agent"] = (
            f"academic-tools-mcp/1.0 "
            f"(https://github.com/academic-tools-mcp; mailto:{mailto})"
        )
    return headers


# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------


def _normalize_doi(doi: str) -> str:
    """Normalize a DOI to bare form (e.g., 10.1234/example).

    Accepts:
      - bare DOI: 10.1234/example
      - prefixed: doi:10.1234/example
      - full URL: https://doi.org/10.1234/example
    """
    doi = doi.strip()
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    elif doi.startswith("http://doi.org/"):
        doi = doi[len("http://doi.org/"):]
    elif doi.startswith("doi:"):
        doi = doi[len("doi:"):]
    return doi


def _canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return _normalize_doi(doi).lower()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def search_works(
    bibliographic: str,
    year: int | None = None,
    rows: int = 5,
) -> list[dict[str, Any]]:
    """Search Crossref works by bibliographic query (title, author, etc.).

    Returns a list of matching work objects (the 'items' from the API response).
    Results are not cached since queries are ad-hoc.
    """
    headers = _build_headers()
    params: dict[str, str] = {
        "query.bibliographic": bibliographic,
        "rows": str(min(max(rows, 1), 20)),
    }
    if year is not None:
        params["filter"] = f"from-pub-date:{year},until-pub-date:{year}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _throttled_get(
            client,
            f"{CROSSREF_BASE_URL}/works",
            headers=headers,
            params=params,
        )

    response.raise_for_status()
    data = response.json()
    return data.get("message", {}).get("items", [])


async def get_work(doi: str) -> dict[str, Any]:
    """Fetch a work by DOI from Crossref, using cache when available.

    Returns the Crossref work object (the 'message' from the API response).
    """
    canonical = _canonical_doi(doi)

    cached = cache.get(NAMESPACE, "works", canonical)
    if cached is not None:
        return cached

    bare_doi = _normalize_doi(doi)
    headers = _build_headers()

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _throttled_get(
            client,
            f"{CROSSREF_BASE_URL}/works/{bare_doi}",
            headers=headers,
        )

    if response.status_code == 404:
        return {"error": f"No work found on Crossref for DOI: {doi}"}

    response.raise_for_status()
    data = response.json()

    work = data.get("message", {})
    cache.put(NAMESPACE, "works", canonical, work)
    return work
