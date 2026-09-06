"""Thin async client for the Wikipedia API.

Provides search and page summary/existence checking via:
  - MediaWiki OpenSearch API for title matching
  - Wikimedia REST API for page summaries and existence verification

No authentication required. Rate-limited to ~1 req/sec as a courtesy.
"""

from typing import Any
from urllib.parse import quote

import httpx

from .. import _clients, _http, _singleflight, _useragent, cache, config
from .._throttle import Throttle

NAMESPACE = "wikipedia"

_OPENSEARCH_URL = "https://en.wikipedia.org/w/api.php"
_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary"

# Both Wikipedia endpoints return JSON; a malformed/truncated 200 body raises
# ``json.JSONDecodeError`` on ``.json()``. Handled alongside the HTTP errors so
# the tools always return the uniform ``{error}`` contract rather than crashing
# on a garbled response. Mirrors crossref/openalex/opencitations.
_PARSE_ERRORS = _http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable Wikipedia response.

    Delegates to ``_http.parse_error_dict``, the single home for the shape.
    """
    return _http.parse_error_dict("Wikipedia")


# Rate limiting: ~1 req/sec (well within 1,000 req/hour reader tier).
# Concurrency cap of 2 lets a search + summary lookup overlap; gap of
# 1s keeps the sustained rate under the per-hour budget. Gating lives
# in ``_throttle``.
_MAX_CONCURRENT = 2
_MIN_REQUEST_GAP = 1.0
_MAX_PENDING = 5

# Coalesces concurrent get_summary calls for the same canonical title.
_single_flight = _singleflight.SingleFlight()

# Positive cache TTL. Wikipedia summaries change as articles are edited;
# 30 days is long enough to amortise repeated reads in a session and
# short enough that significant edits surface within a month.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def _build_headers() -> dict[str, str]:
    """Build request headers from ``WIKIPEDIA_MAILTO``.

    Wikimedia's User-Agent policy asks for a descriptive agent with a way to
    contact the operator; the shared builder emits exactly that shape.
    """
    return _useragent.headers(config.get("WIKIPEDIA_MAILTO"))


def _get_client() -> httpx.AsyncClient:
    """Return the pooled AsyncClient for Wikipedia calls.

    Configured here only: ``_clients.get_client`` ignores kwargs on every later
    call for this namespace, so ``_build_headers`` runs here or nowhere — and
    Wikimedia's identification policy makes it mandatory, not polite.
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

    Returns a dict with title, description, extract (plain text summary),
    url, and page type. Returns an error dict if the page doesn't exist.
    Concurrent callers for the same title share one fetch.

    ``force_refresh=True`` drops both positive and negative cache entries
    before fetching — parity with every other cached getter, useful when an
    article has been edited since the cached 30-day-TTL fetch or to retry a
    title that previously 404'd.
    """
    # Normalize: spaces to underscores for the URL path
    url_title = title.strip().replace(" ", "_")

    # Cache key. Wikipedia titles are case-SENSITIVE beyond the first
    # character (only the leading letter is auto-capitalized), so "PET" and
    # "Pet" are distinct articles — lowercasing the whole title would collide
    # them and serve one's summary for the other. Fold only the first letter
    # (matching MediaWiki's own title normalization) and preserve the rest.
    canonical = url_title[:1].upper() + url_title[1:]

    async def _fetch() -> dict[str, Any]:
        try:
            client = _get_client()
            response = await _throttled_get(
                client,
                # Percent-encode the whole title segment (safe="" — a slash in
                # a title like "AC/DC" is part of the title, not a path
                # separator) so reserved chars (#, ?, /) can't truncate the
                # request or split the path to the wrong record.
                f"{_SUMMARY_URL}/{quote(url_title, safe='')}",
            )

            if response.status_code == 404:
                # ``not_found: True`` distinguishes a definitive 404 from a
                # transient error so page_exists can tell "doesn't exist" from
                # "couldn't check". Mirrors openalex.get_work / get_author.
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
            # Anomalous 200 whose body isn't a JSON object — treat like a parse
            # failure rather than crashing the data.get(...) calls below.
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
        # Definitive 404 — the page genuinely doesn't exist.
        return {
            "exists": False,
            "is_disambiguation": False,
            "title": title,
            "url": None,
        }
    if "error" in summary:
        # Transient failure (timeout / 5xx / backpressure / parse). We can't
        # conclude the page is missing — propagate the error as-is so the
        # caller retries rather than dropping a valid link on a network blip.
        return summary

    return {
        "exists": True,
        "is_disambiguation": summary.get("type") == "disambiguation",
        "title": summary.get("title", title),
        "url": summary.get("url", ""),
        "description": summary.get("description"),
    }
