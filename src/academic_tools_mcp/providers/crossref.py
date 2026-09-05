import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx

from .. import _clients, _doi, _http, _singleflight, _useragent, cache, config
from .._throttle import Throttle

CROSSREF_BASE_URL = "https://api.crossref.org"
NAMESPACE = "crossref"

# The Crossref REST API returns JSON; a malformed/truncated 200 body raises
# ``json.JSONDecodeError`` on ``.json()``. It is handled alongside the HTTP
# errors so the tool always returns the uniform ``{error}`` contract rather
# than crashing on a garbled response.
_PARSE_ERRORS = _http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable Crossref response.

    Delegates to ``_http.parse_error_dict``, the single home for the shape.
    """
    return _http.parse_error_dict("Crossref")


# Crossref runs two service tiers, and which one we get depends on whether we
# identify ourselves. The rate constants used to be hardcoded to the *polite*
# tier unconditionally while the User-Agent carrying the mailto was set only
# when CROSSREF_MAILTO was configured — so the documented default (an empty
# .env; README: "Nothing is required to get started") requested at 2x the
# public-pool rate, 3x its concurrency, and 10x its search rate, anonymously.
#
# Limits per Crossref's REST API docs, mirrored in .claude/rules/providers.md:
#
#            singles      search       concurrent
#   polite   10 req/sec   3 req/sec    3
#   public    5 req/sec   1 req/sec    1
#
# Search is rate-limited far more tightly than singleton lookups and used to
# share the singles throttle entirely, so the search limit was never enforced
# in either tier.
_POLITE_MAX_CONCURRENT = 3
_POLITE_REQUEST_GAP = 0.1  # 100ms -> 10 req/sec
_POLITE_SEARCH_GAP = 0.334  # ~3 req/sec

_PUBLIC_MAX_CONCURRENT = 1
_PUBLIC_REQUEST_GAP = 0.2  # 200ms -> 5 req/sec
_PUBLIC_SEARCH_GAP = 1.0  # 1 req/sec

_MAX_PENDING = 5


def in_polite_pool() -> bool:
    """Whether a contact address is configured, admitting us to the polite pool."""
    return bool(config.get("CROSSREF_MAILTO"))


def _resolve_policy() -> tuple[int, float, float]:
    """Return ``(max_concurrent, request_gap, search_gap)`` for the active tier."""
    if in_polite_pool():
        return _POLITE_MAX_CONCURRENT, _POLITE_REQUEST_GAP, _POLITE_SEARCH_GAP
    return _PUBLIC_MAX_CONCURRENT, _PUBLIC_REQUEST_GAP, _PUBLIC_SEARCH_GAP


_MAX_CONCURRENT, _MIN_REQUEST_GAP, _SEARCH_REQUEST_GAP = _resolve_policy()

# Coalesces concurrent calls for the same canonical DOI so the unified
# paper tools called in parallel don't all hit Crossref independently.
_single_flight = _singleflight.SingleFlight()

# Positive cache TTL. Crossref's reference list grows as publishers
# re-deposit metadata; 30 days is the same span used for OpenAlex works
# and gives reference-graph coverage time to improve without forcing a
# fetch on every reread.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def _build_headers() -> dict[str, str]:
    """Build request headers, carrying the polite-pool mailto when configured.

    The descriptive User-Agent is sent **unconditionally**. It used to be set
    only when ``CROSSREF_MAILTO`` was present, so the default configuration
    identified itself as ``python-httpx/x.y`` — while still requesting at the
    polite-pool *rate*. See ``_resolve_policy`` for the matching fix.
    """
    return _useragent.headers(config.get("CROSSREF_MAILTO"))


def _get_client():
    """Return the persistent AsyncClient for Crossref calls.

    The polite-pool User-Agent header is baked into the client at
    construction so every call automatically opts into the higher rate
    limits.
    """
    return _clients.get_client(NAMESPACE, headers=_build_headers(), timeout=30.0)


_throttle = Throttle(
    namespace=NAMESPACE,
    label="Crossref",
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


async def _throttled_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Execute a GET respecting Crossref's rate limit (see ``Throttle.get``)."""
    return await _throttle.get(client, url, **kwargs)


# Search pacing rides *on top of* the shared throttle rather than using a
# second Throttle: Crossref's concurrency budget covers all requests, so a
# separate semaphore would let searches and singles together exceed it. The
# lock serialises searches (they are rarely issued in parallel anyway) and
# enforces the tighter search gap before handing off to the normal slot.
_search_lock = asyncio.Lock()
_last_search_time = 0.0


def reset_search_pacing() -> None:
    """Rebuild the search lock and clear its timestamp (test seam).

    ``asyncio.Lock`` binds to the running event loop on first await, so a lock
    left over from a previous loop raises "bound to a different event loop".
    Mirrors ``Throttle.reset``.
    """
    global _search_lock, _last_search_time
    _search_lock = asyncio.Lock()
    _last_search_time = 0.0


async def _throttled_search_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a search GET, honouring Crossref's tighter search rate limit."""
    global _last_search_time
    async with _search_lock:
        elapsed = time.monotonic() - _last_search_time
        if _last_search_time > 0 and elapsed < _SEARCH_REQUEST_GAP:
            await asyncio.sleep(_SEARCH_REQUEST_GAP - elapsed)
        _last_search_time = time.monotonic()
    return await _throttled_get(client, url, **kwargs)


# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------


def _normalize_doi(doi: str) -> str:
    """Normalize a DOI to bare form (e.g., 10.1234/example).

    Thin wrapper over :mod:`_doi`, the single home for this logic. Never add a
    local copy: divergent normalization lands one paper under several cache
    keys, chosen by whichever tool the agent happened to call first.
    """
    return _doi.normalize(doi)


def canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return _doi.canonical(doi)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def search_works(
    bibliographic: str,
    year: int | None = None,
    rows: int = 5,
) -> dict[str, Any]:
    """Search Crossref works by bibliographic query (title, author, etc.).

    Returns ``{"items": [...]}`` on success or ``{"error": ...}`` on
    transport / HTTP failure. Results are not cached (ad-hoc queries).
    """
    params: dict[str, str] = {
        "query.bibliographic": bibliographic,
        "rows": str(min(max(rows, 1), 20)),
    }
    if year is not None:
        params["filter"] = f"from-pub-date:{year},until-pub-date:{year}"

    try:
        client = _get_client()
        response = await _throttled_search_get(
            client,
            f"{CROSSREF_BASE_URL}/works",
            params=params,
        )

        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("Crossref", e)

    # `"x" in data` raises TypeError on a JSON `null`/scalar body, and
    # TypeError is in neither _PARSE_ERRORS nor HTTPX_ERRORS — it escaped the
    # provider entirely. Guard the type before indexing, as openalex,
    # opencitations and wikipedia already do.
    if not isinstance(data, dict) or "message" not in data:
        return _parse_error_dict()

    message = data["message"]
    items = message.get("items", []) if isinstance(message, dict) else []

    # Opportunistically warm the works cache. Each search hit is the
    # same shape as a /works/{doi} response, so a follow-up get_work
    # call (the inevitable "now fetch the full record for this hit"
    # pattern) becomes a free cache hit. Mirrors arxiv.search_papers.
    # Use cache.has to avoid stomping a fresher entry.
    for item in items:
        doi = item.get("DOI")
        if not doi:
            continue
        canonical = canonical_doi(doi)
        # TTL-aware (not cache.has) so a stale-but-present entry is refreshed
        # by fresher search data; mirrors arxiv.search_papers.
        if (
            cache.get(
                NAMESPACE,
                "works",
                canonical,
                max_age_seconds=_POSITIVE_TTL_SECONDS,
                count=False,
            )
            is None
        ):
            cache.put(NAMESPACE, "works", canonical, item)

    total = message.get("total-results") if isinstance(message, dict) else None
    return {"items": items, "total_results": total}


async def get_work(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a work by DOI from Crossref, using cache when available.

    Concurrent callers for the same DOI share one fetch via single-flight.
    Returns the Crossref work object (the 'message' from the API response).

    ``force_refresh=True`` drops both positive and negative cache entries
    before fetching — useful when the reference list may have grown since the
    cached fetch, or to retry an identifier that previously 404'd.
    """
    canonical = canonical_doi(doi)

    async def _fetch() -> dict[str, Any]:
        bare_doi = _normalize_doi(doi)

        try:
            client = _get_client()
            response = await _throttled_get(
                client,
                # Percent-encode the DOI so reserved characters (#, ?, …)
                # aren't misread as a URL fragment/query and silently
                # truncate the request to the wrong record. The
                # prefix/suffix slash stays literal (safe="/") — Crossref's
                # proven-working form.
                f"{CROSSREF_BASE_URL}/works/{quote(bare_doi, safe='/')}",
            )

            if response.status_code == 404:
                err = {"error": f"No work found on Crossref for DOI: {doi}"}
                cache.put_negative(NAMESPACE, "works", canonical, err)
                return err

            response.raise_for_status()
            data = response.json()
        except _PARSE_ERRORS:
            # A 200 body we couldn't parse — truncated/garbled. Transient,
            # not "not found": surface a retryable error and do NOT
            # negative-cache it so a retry re-fetches rather than serving a
            # poisoned entry.
            return _parse_error_dict()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("Crossref", e)

        if not isinstance(data, dict) or "message" not in data:
            # Anomalous 200 with no work payload — treat like a parse
            # failure rather than positive-caching an empty {} for the TTL.
            # The isinstance guard matters as much as the key check: a JSON
            # `null` body made `"message" not in data` raise TypeError, which
            # is caught by neither _PARSE_ERRORS nor HTTPX_ERRORS.
            return _parse_error_dict()

        work = data["message"]
        if not isinstance(work, dict):
            return _parse_error_dict()
        cache.put(NAMESPACE, "works", canonical, work)
        return work

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="works",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
    )
