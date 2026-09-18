"""Wikipedia client. MediaWiki full-text search, Wikimedia REST for summaries; no auth."""

import html
import re
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

# One endpoint serves every ``action=``, so the name states the host rather than the
# action it happens to be spelled with.
_API_URL = "https://en.wikipedia.org/w/api.php"
_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary"

# ``list=search`` rows carry no url, so a hit's link is built from its title.
_ARTICLE_URL = "https://en.wikipedia.org/wiki"

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


# MediaWiki wraps every matched term in ``<span class="searchmatch">``, so a snippet is
# markup, not text — ``crossref.abstract_text``'s problem, one provider over.
_SEARCHMATCH_TAG_RE = re.compile(r"<[^>]*>")


def _snippet_text(raw: Any) -> str:
    """A hit's ``snippet`` as plain text, ``""`` when absent or not a string.

    Any tag, not just ``searchmatch``: markup read as content is what an agent quotes,
    and upstream is free to add a second one. Substituting a space, not nothing, keeps
    the words either side of a tag from fusing; the ``split()`` collapses it again.
    """
    if not isinstance(raw, str):
        return ""
    return " ".join(html.unescape(_SEARCHMATCH_TAG_RE.sub(" ", raw)).split())


def _article_url(title: str) -> str:
    """The desktop article URL for a hit, which ``list=search`` rows don't carry.

    Through ``canonical_title`` so a hit's link and the key ``get_summary`` later
    computes for it are one spelling; ``safe=""`` for the reason the summary path
    uses it — a slash in "AC/DC" is part of the title, not a separator.
    """
    return f"{_ARTICLE_URL}/{quote(canonical_title(title), safe='')}"


def _rejection_of(data: Any) -> str | None:
    """MediaWiki's ``error`` detail when the body is a refused request, else ``None``.

    The trap this exists for: a refusal arrives as **HTTP 200** carrying ``error`` and
    no ``query``, so ``raise_for_status`` never sees it and ``_search_of`` would call it
    a garbled body — selling a request that cannot succeed as worth retrying. arXiv's
    ``api/errors`` entry, same shape.

    ``""`` for a refusal naming neither code nor info; ``None`` is the discriminator,
    so callers test ``is None``.
    """
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not isinstance(error, dict):
        return None
    parts = [p for p in (error.get("code"), error.get("info")) if isinstance(p, str) and p]
    return " ".join(" ".join(parts).split())


def _search_of(data: Any) -> dict[str, Any] | None:
    """The slice of a ``list=search`` body ``search`` returns, or ``None`` for a wrong shape.

    ``Any``: an untyped body reaches ``.get`` here and ``.replace`` inside
    ``canonical_title``, and ``AttributeError`` is in neither ``_PARSE_ERRORS`` nor
    ``HTTPX_ERRORS`` — so an unguarded body escapes the provider entirely. A wrong shape
    is never an empty result set: a zero-hit query is a well-formed body whose ``search``
    is empty, and is not an error.
    """
    if not isinstance(data, dict):
        return None
    query = data.get("query")
    if not isinstance(query, dict):
        return None
    rows = query.get("search")
    if not isinstance(rows, list):
        return None

    # The title is the url's path segment and get_summary's key, so a row without a
    # usable one is dropped rather than handed over as a link to nothing.
    hits = [
        {
            "title": row["title"],
            "url": _article_url(row["title"]),
            "snippet": _snippet_text(row.get("snippet")),
        }
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("title"), str) and row["title"].strip()
    ]
    # Rows that yielded nothing usable are malformed, not "no results".
    if rows and not hits:
        return None

    info = query.get("searchinfo")
    info = info if isinstance(info, dict) else {}
    total = info.get("totalhits")
    suggested = info.get("suggestion")
    return {
        "results": hits,
        # A missing total is unknown, never zero; the tool layer folds it to an int.
        "total_results": total if isinstance(total, int) else None,
        # Not "suggestion": ``enrich_error`` owns that key on the error shape, and one
        # name meaning "recovery advice" on one branch and "did you mean" on the other
        # is a fork an agent cannot see.
        "did_you_mean": suggested if isinstance(suggested, str) and suggested else None,
    }


async def search(query: str, limit: int = 5) -> dict[str, Any]:
    """Full-text search of English Wikipedia — article text, not just titles.

    Returns ``{"results": [{"title", "url", "snippet"}, ...], "total_results",
    "did_you_mean"}``. ``total_results`` is MediaWiki's own match count and
    ``did_you_mean`` the spelling it would have searched instead, which is how a typo
    presents: zero hits plus a suggestion. A request MediaWiki refuses is ``{"error",
    "retryable": False}``; transport, HTTP and wrong-shape failures are retryable.

    Not cached, and warms nothing: a hit is a title and a snippet, and written to
    ``summaries`` it would poison ``get_summary``'s key.
    """
    capped = min(max(limit, 1), MAX_SEARCH_LIMIT)

    try:
        response = await _throttled_get(
            _API_URL,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": str(capped),
                # The hit list is for triage; the article extract is get_summary's job.
                "srprop": "snippet",
                # `suggestion` is what keeps a misspelling from reading as absence.
                "srinfo": "totalhits|suggestion",
                "format": "json",
                # Pins the shape the guards walk; the legacy format spells these fields
                # differently.
                "formatversion": "2",
            },
        )

        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except http.HTTPX_ERRORS as e:
        return http.error_dict(LABEL, e)

    # Before the shape guard: a refusal is a 200 whose body is well-formed JSON and not
    # a result set, so the shape guard would sell it as worth retrying.
    detail = _rejection_of(data)
    if detail is not None:
        return {
            "error": f"{LABEL} rejected the search query: {detail or query}",
            "retryable": False,
        }

    result = _search_of(data)
    if result is None:
        return _parse_error_dict()
    return result


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
