"""Wikipedia client. MediaWiki OpenSearch for titles, Wikimedia REST for summaries; no auth."""

from typing import Any
from urllib.parse import quote

import httpx

from ..net import clients, http
from ..net.throttle import Throttle
from ..store import cache, singleflight
from ..util import config, useragent

NAMESPACE = "wikipedia"

# Agent-facing provider name; every site that names us reads it.
LABEL = "Wikipedia"

_OPENSEARCH_URL = "https://en.wikipedia.org/w/api.php"
_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary"

# Exported so ``search_wikipedia``'s validation bound isn't a second spelling of it.
MAX_SEARCH_LIMIT = 10

_PARSE_ERRORS = http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable Wikipedia response."""
    return http.parse_error_dict(LABEL)


# 200 req/min is the documented ceiling for an identified anonymous client, so the gap
# leaves ample headroom; concurrency stays under Wikimedia's recommended cap of 3.
_MAX_CONCURRENT = 2
_MIN_REQUEST_GAP = 1.0
_MAX_PENDING = 5

_single_flight = singleflight.SingleFlight()

# Articles are edited continuously; a month bounds how stale a summary gets.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def _build_headers() -> dict[str, str]:
    """Headers from ``WIKIPEDIA_MAILTO``. Wikimedia's UA policy wants a contact."""
    return useragent.headers(config.get("WIKIPEDIA_MAILTO"))


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``clients.get_client``.

    Headers are mandatory, not polite: an unidentified client is capped at 10 req/min,
    where a compliant User-Agent gets 200.
    """
    return clients.get_client(NAMESPACE, headers=_build_headers(), timeout=15.0)


_throttle = Throttle(
    namespace=NAMESPACE,
    label=LABEL,
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


async def _throttled_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at Wikipedia's rate. Url-only: ``_get_client`` is the only place to configure it."""
    return await _throttle.get(_get_client(), url, **kwargs)


def canonical_title(title: str) -> str:
    """Return the cache-key form, which is also the URL path segment.

    MediaWiki reads space and underscore as one character and collapses runs of them;
    only the leading letter is auto-capitalized, so ``PET`` and ``Pet`` stay distinct.
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
        response = await _throttled_get(
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
    except http.HTTPX_ERRORS as e:
        return http.error_dict(LABEL, e)

    # OpenSearch returns [query, [titles], [descriptions], [urls]].
    if not isinstance(data, list) or len(data) < 4:
        return _parse_error_dict()

    # Two strings zip into per-character "hits", and a wrong shape is never "no matches".
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


def _summary_of(data: Any) -> dict[str, Any] | None:
    """The slice of a REST summary ``get_summary`` returns, or ``None`` for a wrong shape.

    ``Any``: a scalar body raises ``AttributeError``, caught by nothing — the call sits
    outside ``_fetch``'s ``try``. A wrong shape is never an empty page.
    """
    if not isinstance(data, dict):
        return None
    return {
        "title": data.get("title", ""),
        "description": data.get("description"),
        "extract": data.get("extract", ""),
        "url": _desktop_page_url(data.get("content_urls")),
        "type": data.get("type", ""),
        "pageid": data.get("pageid"),
    }


async def get_summary(title: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a page summary from the Wikipedia REST API.

    Returns ``{title, description, extract, url, type, pageid}``. A 404 is a
    negative-cached ``not_found``; transport and wrong-shape failures are retryable.
    """
    canonical = canonical_title(title)
    not_found_error = f"Wikipedia page not found: {title}"

    async def _fetch() -> dict[str, Any]:
        # safe="": a slash in a title like "AC/DC" is part of the title, not a separator.
        url = f"{_SUMMARY_URL}/{quote(canonical, safe='')}"

        # `.`/`..` (unreserved, so `quote` can't help) and an empty title both shorten this
        # to `/page`, whose 200 dict would cache as the title. Uncached — nothing spent.
        if not http.addresses_a_record(url):
            return http.not_found(not_found_error)

        try:
            response = await _throttled_get(url)

            if response.status_code == 404:
                err = http.not_found(not_found_error)
                cache.put_negative(NAMESPACE, "summaries", canonical, err)
                return err

            response.raise_for_status()
            data = response.json()
        except _PARSE_ERRORS:
            return _parse_error_dict()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        result = _summary_of(data)
        if result is None:
            return _parse_error_dict()

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
