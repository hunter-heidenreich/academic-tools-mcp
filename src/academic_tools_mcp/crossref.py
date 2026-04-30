import asyncio
import time
from typing import Any

import httpx

from . import _clients, _http, _singleflight, _stats, cache, config

CROSSREF_BASE_URL = "https://api.crossref.org"
NAMESPACE = "crossref"

# Rate limiting for the polite pool: max 10 req/sec, 3 concurrent.
# We enforce a conservative 100ms minimum gap between requests.
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 0.1  # 100ms -> ~10 req/sec max
_MAX_PENDING = 5
_pending: int = 0

# Coalesces concurrent calls for the same canonical DOI so the unified
# paper tools called in parallel don't all hit Crossref independently.
_single_flight = _singleflight.SingleFlight()

# Positive cache TTL. Crossref's reference list grows as publishers
# re-deposit metadata; 30 days is the same span used for OpenAlex works
# and gives reference-graph coverage time to improve without forcing a
# fetch on every reread.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


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


def _get_client():
    """Return the persistent AsyncClient for Crossref calls.

    The polite-pool User-Agent header is baked into the client at
    construction so every call automatically opts into the higher rate
    limits.
    """
    return _clients.get_client(
        NAMESPACE, headers=_build_headers(), timeout=30.0
    )


async def _throttled_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a GET request respecting Crossref's rate limit.

    Refuses past ``_MAX_PENDING`` queued callers via
    ``LocalBackpressureError`` so an agent that fans out queries gets
    fast feedback rather than waiting tens of slots deep.
    """
    global _last_request_time, _pending
    if _pending >= _MAX_PENDING:
        _stats.incr(NAMESPACE, "backpressure_refusals")
        raise _http.LocalBackpressureError(
            "Crossref", _pending, _MAX_PENDING, _MIN_REQUEST_GAP
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
) -> dict[str, Any]:
    """Search Crossref works by bibliographic query (title, author, etc.).

    Returns ``{"items": [...]}`` on success or ``{"error": ...}`` on
    transport / HTTP failure. Results are not cached (ad-hoc queries).
    """
    headers = _build_headers()
    params: dict[str, str] = {
        "query.bibliographic": bibliographic,
        "rows": str(min(max(rows, 1), 20)),
    }
    if year is not None:
        params["filter"] = f"from-pub-date:{year},until-pub-date:{year}"

    try:
        client = _get_client()
        response = await _throttled_get(
            client,
            f"{CROSSREF_BASE_URL}/works",
            headers=headers,
            params=params,
        )

        response.raise_for_status()
        data = response.json()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("Crossref", e)

    items = data.get("message", {}).get("items", [])

    # Opportunistically warm the works cache. Each search hit is the
    # same shape as a /works/{doi} response, so a follow-up get_work
    # call (the inevitable "now fetch the full record for this hit"
    # pattern) becomes a free cache hit. Mirrors arxiv.search_papers.
    # Use cache.has to avoid stomping a fresher entry.
    for item in items:
        doi = item.get("DOI")
        if not doi:
            continue
        canonical = _canonical_doi(doi)
        if not cache.has(NAMESPACE, "works", canonical):
            cache.put(NAMESPACE, "works", canonical, item)

    return {"items": items}


async def get_work(doi: str) -> dict[str, Any]:
    """Fetch a work by DOI from Crossref, using cache when available.

    Concurrent callers for the same DOI share one fetch via single-flight.
    Returns the Crossref work object (the 'message' from the API response).
    """
    canonical = _canonical_doi(doi)

    cached = cache.get(NAMESPACE, "works", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
    if cached is not None:
        return cached
    neg = cache.get_negative(NAMESPACE, "works", canonical)
    if neg is not None:
        return neg

    async def _fetch() -> dict[str, Any]:
        cached = cache.get(NAMESPACE, "works", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
        if cached is not None:
            return cached
        neg = cache.get_negative(NAMESPACE, "works", canonical)
        if neg is not None:
            return neg

        bare_doi = _normalize_doi(doi)
        headers = _build_headers()

        try:
            client = _get_client()
            response = await _throttled_get(
                client,
                f"{CROSSREF_BASE_URL}/works/{bare_doi}",
                headers=headers,
            )

            if response.status_code == 404:
                err = {"error": f"No work found on Crossref for DOI: {doi}"}
                cache.put_negative(NAMESPACE, "works", canonical, err)
                return err

            response.raise_for_status()
            data = response.json()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("Crossref", e)

        work = data.get("message", {})
        cache.put(NAMESPACE, "works", canonical, work)
        return work

    return await _single_flight.do(canonical, _fetch)
