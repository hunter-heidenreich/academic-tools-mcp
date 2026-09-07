"""Crossref client. Title search and metadata, with a tighter pace for search."""

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx

from .. import _clients, _doi, _http, _singleflight, _useragent, cache, config
from .._throttle import Throttle

CROSSREF_BASE_URL = "https://api.crossref.org"
NAMESPACE = "crossref"

_PARSE_ERRORS = _http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable Crossref response."""
    return _http.parse_error_dict("Crossref")


# The rate we take must follow the identity we send: hardcoding the polite
# figures would request at that rate anonymously. Tiers in providers.md.
_POLITE_MAX_CONCURRENT = 3
_POLITE_REQUEST_GAP = 0.1  # 100ms -> 10 req/sec
_POLITE_SEARCH_GAP = 0.334  # ~3 req/sec

_PUBLIC_MAX_CONCURRENT = 1
_PUBLIC_REQUEST_GAP = 0.2  # 200ms -> 5 req/sec
_PUBLIC_SEARCH_GAP = 1.0  # 1 req/sec

_MAX_PENDING = 5

# Our own ceiling on a triage list, not Crossref's (it allows 1000).
MAX_SEARCH_ROWS = 20


def in_polite_pool() -> bool:
    """Whether a contact address is configured, admitting us to the polite pool."""
    return bool(config.get("CROSSREF_MAILTO"))


def _resolve_policy() -> tuple[int, float, float]:
    """Return ``(max_concurrent, request_gap, search_gap)`` for the active tier."""
    if in_polite_pool():
        return _POLITE_MAX_CONCURRENT, _POLITE_REQUEST_GAP, _POLITE_SEARCH_GAP
    return _PUBLIC_MAX_CONCURRENT, _PUBLIC_REQUEST_GAP, _PUBLIC_SEARCH_GAP


_MAX_CONCURRENT, _MIN_REQUEST_GAP, _SEARCH_REQUEST_GAP = _resolve_policy()

_single_flight = _singleflight.SingleFlight()

# Same span as OpenAlex works: a reference list grows as publishers re-deposit.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def _build_headers() -> dict[str, str]:
    """Request headers; the User-Agent is unconditional, the mailto is not.

    Gating the whole header on ``CROSSREF_MAILTO`` would leave the default
    configuration identifying as ``python-httpx/x.y``.
    """
    return _useragent.headers(config.get("CROSSREF_MAILTO"))


def _get_client() -> httpx.AsyncClient:
    """Return the pooled AsyncClient for Crossref calls.

    Configured here only: ``_clients.get_client`` ignores kwargs on every later
    call for this namespace.
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


# A lock, not a second Throttle: its semaphore would let searches and singles
# together exceed Crossref's one concurrency budget.
_search_lock = asyncio.Lock()
_last_search_time = 0.0


def reset_search_pacing() -> None:
    """Rebuild the search lock and clear its timestamp (test seam).

    Mirrors ``Throttle.reset``: a lock left over from a previous event loop
    raises "bound to a different event loop".
    """
    global _search_lock, _last_search_time  # noqa: PLW0603 — process-wide search throttle
    _search_lock = asyncio.Lock()
    _last_search_time = 0.0


async def _throttled_search_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a search GET, honouring Crossref's tighter search rate limit.

    Stamped before the singles hand-off, so a queued search can start after its
    stamp — a known drift, argued in providers.md.
    """
    global _last_search_time  # noqa: PLW0603 — process-wide search throttle
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

    Thin wrapper over :mod:`_doi`, the single home for this logic — a local
    copy lands one paper under several cache keys.
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

    Returns ``{"items": [...], "total_results": N}`` on success — ``items``
    holds only dict-shaped hits — or ``{"error": ...}`` on transport / HTTP
    failure or a wrong-shape body. The result list is not cached (ad-hoc
    queries), but each hit with a DOI warms the works cache.
    """
    params: dict[str, str] = {
        "query.bibliographic": bibliographic,
        "rows": str(min(max(rows, 1), MAX_SEARCH_ROWS)),
    }
    if year is not None:
        # Year-only on purpose; a fully-specified date drops works whose own
        # deposited date is year-only (CrossRef/rest-api-doc#7).
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

    # Every rung is a shape that raises out of the provider unguarded, and none
    # is an empty result set: "no papers match" ends the agent's search.
    if not isinstance(data, dict) or "message" not in data:
        return _parse_error_dict()

    message = data["message"]
    if not isinstance(message, dict):
        return _parse_error_dict()

    items = message.get("items") or []
    if not isinstance(items, list):
        return _parse_error_dict()
    items = [item for item in items if isinstance(item, dict)]

    # A hit has the same shape as /works/{doi}, so the inevitable follow-up
    # get_work is a free cache hit. Mirrors arxiv.search_papers.
    for item in items:
        doi = item.get("DOI")
        # isinstance, not truthiness: a non-string DOI reaches _doi.normalize
        # and raises AttributeError, which no except clause here catches.
        if not isinstance(doi, str) or not doi:
            continue
        cache.warm(
            NAMESPACE, "works", canonical_doi(doi), item, max_age_seconds=_POSITIVE_TTL_SECONDS
        )

    return {"items": items, "total_results": message.get("total-results")}


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
                # Percent-encoded so a reserved character can't truncate the
                # request to the wrong record; the DOI's own slash stays literal.
                f"{CROSSREF_BASE_URL}/works/{quote(bare_doi, safe='/')}",
            )

            if response.status_code == 404:
                # Definitive, hence both the negative entry and the flag
                # tools/graph.py forwards.
                err = {"error": f"No work found on Crossref for DOI: {doi}", "not_found": True}
                cache.put_negative(NAMESPACE, "works", canonical, err)
                return err

            response.raise_for_status()
            data = response.json()
        except _PARSE_ERRORS:
            # Transient, not "not found" — uncached, so a retry re-fetches.
            return _parse_error_dict()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("Crossref", e)

        # Wrong shape, not an empty work: never positive-cached for the TTL.
        if not isinstance(data, dict) or "message" not in data:
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
