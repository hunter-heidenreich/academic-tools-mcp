"""Thin async client for the Wikipedia API.

MediaWiki OpenSearch for title matching; the Wikimedia REST API for page
summaries. No authentication required.
"""

from typing import Any
from urllib.parse import quote

import httpx

from .. import _clients, _http, _singleflight, _useragent, cache, config
from .._throttle import Throttle

NAMESPACE = "wikipedia"

_OPENSEARCH_URL = "https://en.wikipedia.org/w/api.php"
_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary"

# Exported so ``search_wikipedia``'s validation bound isn't a second spelling of it.
MAX_SEARCH_LIMIT = 10

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


def canonical_title(title: str) -> str:
    """Return the cache-key form, which is also the URL path segment.

    MediaWiki reads space and underscore as one character and collapses runs
    of them; only the leading letter is auto-capitalized, so ``PET`` and
    ``Pet`` stay distinct articles.
    """
    underscored = "_".join(title.replace("_", " ").split())
    first, rest = underscored[:1], underscored[1:]
    upper = first.upper()
    # A one-character upper() only: ``"ß".upper()`` is ``"SS"``, a different title.
    return (upper if len(upper) == 1 else first) + rest


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def search(query: str, limit: int = 5) -> dict[str, Any]:
    """Search Wikipedia for articles matching a query.

    Returns ``{"results": [{"title", "url"}, ...]}`` on success or
    ``{"error": ...}`` on transport / HTTP failure or a wrong-shape body.
    """
    capped = min(max(limit, 1), MAX_SEARCH_LIMIT)

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

    # OpenSearch returns [query, [titles], [descriptions], [urls]].
    if not isinstance(data, list) or len(data) < 4:
        return _parse_error_dict()

    # Neither rung is an empty result set: a wrong shape reported as "no
    # matches" ends the agent's search, and two strings zip into per-char hits.
    titles, urls = data[1], data[3]
    if not isinstance(titles, list) or not isinstance(urls, list):
        return _parse_error_dict()

    return {
        "results": [
            {"title": t, "url": u}
            for t, u in zip(titles, urls, strict=False)
            if isinstance(t, str) and isinstance(u, str)
        ]
    }


# ---------------------------------------------------------------------------
# Page summary
# ---------------------------------------------------------------------------


def _desktop_page_url(content_urls: Any) -> str:
    """The desktop page URL from a summary's ``content_urls``, or ``""``.

    ``Any``: a non-dict at either level degrades to ``""`` rather than raising
    the ``AttributeError`` neither ``_PARSE_ERRORS`` nor ``HTTPX_ERRORS`` catches.
    """
    if not isinstance(content_urls, dict):
        return ""
    desktop = content_urls.get("desktop")
    if not isinstance(desktop, dict):
        return ""
    page = desktop.get("page")
    return page if isinstance(page, str) else ""


async def get_summary(title: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a page summary from the Wikipedia REST API.

    Returns title, description, extract (plain text), url, and page type, or an
    error dict if the page doesn't exist.
    """
    canonical = canonical_title(title)
    not_found_error = f"Wikipedia page not found: {title}"

    async def _fetch() -> dict[str, Any]:
        # safe="": a slash in a title like "AC/DC" is part of the title,
        # not a path separator, so the whole segment is escaped.
        url = f"{_SUMMARY_URL}/{quote(canonical, safe='')}"

        # The guard `quote` cannot be: `.`/`..` are unreserved, so RFC 3986
        # removes the segment and /page answers 200 with a dict, which would
        # cache as this title. Uncached — no request was spent.
        if not _http.addresses_a_record(url):
            return {"error": not_found_error, "not_found": True}

        try:
            client = _get_client()
            response = await _throttled_get(client, url)

            if response.status_code == 404:
                err = {"error": not_found_error, "not_found": True}
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
            "url": _desktop_page_url(data.get("content_urls")),
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
