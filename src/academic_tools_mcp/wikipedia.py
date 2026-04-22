"""Thin async client for the Wikipedia API.

Provides search and page summary/existence checking via:
  - MediaWiki OpenSearch API for title matching
  - Wikimedia REST API for page summaries and existence verification

No authentication required. Rate-limited to ~1 req/sec as a courtesy.
"""

import asyncio
import time
from typing import Any

import httpx

from . import _http, cache, config

NAMESPACE = "wikipedia"

_OPENSEARCH_URL = "https://en.wikipedia.org/w/api.php"
_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary"


def _build_user_agent() -> str:
    """Build User-Agent from WIKIPEDIA_MAILTO env var, or use a default."""
    mailto = config.get("WIKIPEDIA_MAILTO")
    if mailto:
        return f"AcademicToolsMCP/1.0 (mailto:{mailto}) httpx"
    return "AcademicToolsMCP/1.0 httpx"

# Rate limiting: ~1 req/sec (well within 1,000 req/hour reader tier)
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 1.0


async def _throttled_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a GET request with polite rate limiting."""
    global _last_request_time
    async with _request_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if _last_request_time > 0 and elapsed < _MIN_REQUEST_GAP:
            await asyncio.sleep(_MIN_REQUEST_GAP - elapsed)
        response = await client.get(url, **kwargs)
        _last_request_time = time.monotonic()
        return response


def _headers() -> dict[str, str]:
    """Standard headers for Wikipedia API requests."""
    return {"User-Agent": _build_user_agent()}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def search(query: str, limit: int = 5) -> dict[str, Any]:
    """Search Wikipedia for articles matching a query.

    Returns ``{"results": [{"title", "url"}, ...]}`` on success or
    ``{"error": ...}`` on transport / HTTP failure.
    """
    capped = min(max(limit, 1), 10)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await _throttled_get(
                client,
                _OPENSEARCH_URL,
                params={
                    "action": "opensearch",
                    "search": query,
                    "limit": str(capped),
                    "format": "json",
                },
                headers=_headers(),
            )

        response.raise_for_status()
        data = response.json()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("Wikipedia", e)

    # OpenSearch returns [query, [titles], [descriptions], [urls]]
    if not isinstance(data, list) or len(data) < 4:
        return {"results": []}

    titles = data[1] or []
    urls = data[3] or []

    return {
        "results": [
            {"title": t, "url": u}
            for t, u in zip(titles, urls)
        ]
    }


# ---------------------------------------------------------------------------
# Page summary / existence
# ---------------------------------------------------------------------------


async def get_summary(title: str) -> dict[str, Any]:
    """Fetch a page summary from the Wikipedia REST API.

    Returns a dict with title, description, extract (plain text summary),
    url, and page type. Returns an error dict if the page doesn't exist.
    """
    # Normalize: spaces to underscores for the URL path
    url_title = title.strip().replace(" ", "_")

    # Check cache first
    canonical = url_title.lower()
    cached = cache.get(NAMESPACE, "summaries", canonical)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await _throttled_get(
                client,
                f"{_SUMMARY_URL}/{url_title}",
                headers=_headers(),
            )

        if response.status_code == 404:
            return {"error": f"Wikipedia page not found: {title}"}

        response.raise_for_status()
        data = response.json()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("Wikipedia", e)

    result = {
        "title": data.get("title", ""),
        "description": data.get("description"),
        "extract": data.get("extract", ""),
        "url": (data.get("content_urls") or {}).get("desktop", {}).get("page", ""),
        "type": data.get("type", ""),
        "pageid": data.get("pageid"),
    }

    cache.put(NAMESPACE, "summaries", canonical, result)
    return result


async def page_exists(title: str) -> dict[str, Any]:
    """Check if a Wikipedia page exists and is a standard article.

    Returns a dict with 'exists', 'is_disambiguation', 'url', and 'title'.
    Useful for verifying Wikipedia URLs before suggesting them as links.
    """
    summary = await get_summary(title)

    if "error" in summary:
        return {
            "exists": False,
            "is_disambiguation": False,
            "title": title,
            "url": None,
        }

    return {
        "exists": True,
        "is_disambiguation": summary.get("type") == "disambiguation",
        "title": summary.get("title", title),
        "url": summary.get("url", ""),
        "description": summary.get("description"),
    }
