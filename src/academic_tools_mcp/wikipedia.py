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

from . import _clients, _http, _singleflight, _stats, cache, config

NAMESPACE = "wikipedia"

_OPENSEARCH_URL = "https://en.wikipedia.org/w/api.php"
_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary"


def _build_user_agent() -> str:
    """Build User-Agent from WIKIPEDIA_MAILTO env var, or use a default."""
    mailto = config.get("WIKIPEDIA_MAILTO")
    if mailto:
        return f"AcademicToolsMCP/1.0 (mailto:{mailto}) httpx"
    return "AcademicToolsMCP/1.0 httpx"


# Rate limiting: ~1 req/sec (well within 1,000 req/hour reader tier).
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 1.0
_MAX_PENDING = 5
_pending: int = 0

# Coalesces concurrent get_summary calls for the same canonical title.
_single_flight = _singleflight.SingleFlight()

# Positive cache TTL. Wikipedia summaries change as articles are edited;
# 30 days is long enough to amortise repeated reads in a session and
# short enough that significant edits surface within a month.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def _headers() -> dict[str, str]:
    """Standard headers for Wikipedia API requests."""
    return {"User-Agent": _build_user_agent()}


def _get_client():
    """Return the persistent AsyncClient for Wikipedia calls.

    The User-Agent header (with mailto when configured) is baked in at
    construction so every call meets Wikimedia's identification policy.
    """
    return _clients.get_client(
        NAMESPACE, headers=_headers(), timeout=15.0
    )


async def _throttled_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a GET request with polite rate limiting.

    Refuses past ``_MAX_PENDING`` queued callers via
    ``LocalBackpressureError`` so an agent that fans out gets fast
    feedback rather than waiting through 5 seconds of stacked gaps.
    """
    global _last_request_time, _pending
    if _pending >= _MAX_PENDING:
        _stats.incr(NAMESPACE, "backpressure_refusals")
        raise _http.LocalBackpressureError(
            "Wikipedia", _pending, _MAX_PENDING, _MIN_REQUEST_GAP
        )
    _pending += 1
    try:
        async with _request_lock:
            now = time.monotonic()
            elapsed = now - _last_request_time
            wait_seconds = 0.0
            if _last_request_time > 0 and elapsed < _MIN_REQUEST_GAP:
                wait_seconds = _MIN_REQUEST_GAP - elapsed
                await asyncio.sleep(wait_seconds)
            _stats.log_request(NAMESPACE, url, wait_seconds)
            _stats.incr(NAMESPACE, "http_calls")
            response = await _http.get_with_retry(
                client, url,
                backoff_seconds=max(_MIN_REQUEST_GAP, 1.0),
                provider=NAMESPACE,
                **kwargs,
            )
            _last_request_time = time.monotonic()
            return response
    finally:
        _pending -= 1


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
        client = _get_client()
        response = await _throttled_get(
            client,
            _OPENSEARCH_URL,
            params={
                "action": "opensearch",
                "search": query,
                "limit": str(capped),
                "format": "json",
            },
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
    Concurrent callers for the same title share one fetch.
    """
    # Normalize: spaces to underscores for the URL path
    url_title = title.strip().replace(" ", "_")

    # Check cache first
    canonical = url_title.lower()
    cached = cache.get(NAMESPACE, "summaries", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
    if cached is not None:
        return cached
    neg = cache.get_negative(NAMESPACE, "summaries", canonical)
    if neg is not None:
        return neg

    async def _fetch() -> dict[str, Any]:
        cached = cache.get(NAMESPACE, "summaries", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
        if cached is not None:
            return cached
        neg = cache.get_negative(NAMESPACE, "summaries", canonical)
        if neg is not None:
            return neg

        try:
            client = _get_client()
            response = await _throttled_get(
                client,
                f"{_SUMMARY_URL}/{url_title}",
            )

            if response.status_code == 404:
                err = {"error": f"Wikipedia page not found: {title}"}
                cache.put_negative(NAMESPACE, "summaries", canonical, err)
                return err

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

    return await _single_flight.do(canonical, _fetch)


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
