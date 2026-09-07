"""Thin async client for the Wikipedia API.

MediaWiki OpenSearch for title matching; the Wikimedia REST API for page
summaries and existence verification. No authentication required.
"""

from typing import Any
from urllib.parse import quote

import httpx

from .. import _clients, _http, _singleflight, _useragent, cache, config
from .._throttle import Throttle

NAMESPACE = "wikipedia"

_OPENSEARCH_URL = "https://en.wikipedia.org/w/api.php"
_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary"

_PARSE_ERRORS = _http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable Wikipedia response."""
    return _http.parse_error_dict("Wikipedia")


# ~1 req/sec keeps the sustained rate well inside the 1,000/hour reader tier;
# concurrency of 2 lets a search and a summary lookup overlap.
_MAX_CONCURRENT = 2
_MIN_REQUEST_GAP = 1.0
_MAX_PENDING = 5

_single_flight = _singleflight.SingleFlight()

# Articles are edited continuously; a month bounds how stale a summary gets.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def _build_headers() -> dict[str, str]:
    """Headers from ``WIKIPEDIA_MAILTO``. Wikimedia's UA policy wants a contact."""
    return _useragent.headers(config.get("WIKIPEDIA_MAILTO"))


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``_clients.get_client``.

    The headers are mandatory here, not polite: Wikimedia may block an
    unidentified agent outright.
    """
    return _clients.get_client(NAMESPACE, headers=_build_headers(), timeout=15.0)


_throttle = Throttle(
    namespace=NAMESPACE,
    label="Wikipedia",
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


async def _throttled_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Execute a GET respecting Wikipedia's polite rate limit (see ``Throttle.get``)."""
    return await _throttle.get(client, url, **kwargs)


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
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("Wikipedia", e)

    # OpenSearch returns [query, [titles], [descriptions], [urls]]
    if not isinstance(data, list) or len(data) < 4:
        return {"results": []}

    titles = data[1] or []
    urls = data[3] or []

    return {"results": [{"title": t, "url": u} for t, u in zip(titles, urls, strict=False)]}


# ---------------------------------------------------------------------------
# Page summary / existence
# ---------------------------------------------------------------------------


async def get_summary(title: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a page summary from the Wikipedia REST API.

    Returns title, description, extract (plain text), url, and page type, or an
    error dict if the page doesn't exist.
    """
    url_title = title.strip().replace(" ", "_")

    # Only the leading letter is auto-capitalized, so "PET" and "Pet" are
    # distinct articles: folding the whole title would serve one for the other.
    canonical = url_title[:1].upper() + url_title[1:]

    async def _fetch() -> dict[str, Any]:
        try:
            client = _get_client()
            # safe="": a slash in a title like "AC/DC" is part of the title,
            # not a path separator, so the whole segment is escaped.
            response = await _throttled_get(client, f"{_SUMMARY_URL}/{quote(url_title, safe='')}")

            if response.status_code == 404:
                # The flag ``page_exists`` reads to tell "doesn't exist" from
                # "couldn't check".
                err = {"error": f"Wikipedia page not found: {title}", "not_found": True}
                cache.put_negative(NAMESPACE, "summaries", canonical, err)
                return err

            response.raise_for_status()
            data = response.json()
        except _PARSE_ERRORS:
            return _parse_error_dict()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("Wikipedia", e)

        if not isinstance(data, dict):
            # Wrong shape, not an empty page — the ``data.get`` calls below
            # would crash on it, and it must never cache for the TTL.
            return _parse_error_dict()

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

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="summaries",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
    )


async def page_exists(title: str) -> dict[str, Any]:
    """Check if a Wikipedia page exists and is a standard article.

    Returns a dict with 'exists', 'is_disambiguation', 'url', and 'title'.
    Useful for verifying Wikipedia URLs before suggesting them as links.
    """
    summary = await get_summary(title)

    if summary.get("not_found"):
        return {
            "exists": False,
            "is_disambiguation": False,
            "title": title,
            "url": None,
        }
    if "error" in summary:
        # Transient: nothing was established, so propagate rather than report a
        # network blip as confident non-existence.
        return summary

    return {
        "exists": True,
        "is_disambiguation": summary.get("type") == "disambiguation",
        "title": summary.get("title", title),
        "url": summary.get("url", ""),
        "description": summary.get("description"),
    }
