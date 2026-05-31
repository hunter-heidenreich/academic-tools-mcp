import asyncio
import json
import time
from typing import Any
from urllib.parse import quote

import httpx

from .. import _clients, _http, _singleflight, _stats, cache, config

CROSSREF_BASE_URL = "https://api.crossref.org"
NAMESPACE = "crossref"

# The Crossref REST API returns JSON; a malformed/truncated 200 body raises
# ``json.JSONDecodeError`` on ``.json()``. It is handled alongside the HTTP
# errors so the tool always returns the uniform ``{error}`` contract rather
# than crashing on a garbled response.
_PARSE_ERRORS = (json.JSONDecodeError,)


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable Crossref response.

    A new dict each call (like ``_http.error_dict``) so a caller — or a
    single-flight follower sharing the result — can't mutate a shared object.
    """
    return {
        "error": "Crossref returned a response that could not be parsed.",
        "retryable": True,
    }


# Rate limiting for the polite pool: max 10 req/sec, 3 concurrent.
# Concurrency cap of 3 matches the polite-pool concurrency budget; gap
# of 100ms gives 10 req/sec sustained.
_MAX_CONCURRENT = 3
_request_sem = asyncio.Semaphore(_MAX_CONCURRENT)
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
            f"academic-tools-mcp/1.0 (https://github.com/academic-tools-mcp; mailto:{mailto})"
        )
    return headers


def _get_client():
    """Return the persistent AsyncClient for Crossref calls.

    The polite-pool User-Agent header is baked into the client at
    construction so every call automatically opts into the higher rate
    limits.
    """
    return _clients.get_client(NAMESPACE, headers=_build_headers(), timeout=30.0)


async def _throttled_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Execute a GET request respecting Crossref's rate limit.

    Refuses past ``_MAX_PENDING`` queued callers via
    ``LocalBackpressureError`` so an agent that fans out queries gets
    fast feedback rather than waiting tens of slots deep.
    """
    global _last_request_time, _pending
    if _pending >= _MAX_PENDING:
        _stats.incr(NAMESPACE, "backpressure_refusals")
        raise _http.LocalBackpressureError("Crossref", _pending, _MAX_PENDING, _MIN_REQUEST_GAP)
    _pending += 1
    try:
        async with _request_sem:
            async with _request_lock:
                now = time.monotonic()
                elapsed = now - _last_request_time
                wait_seconds = 0.0
                if _last_request_time > 0 and elapsed < _MIN_REQUEST_GAP:
                    wait_seconds = _MIN_REQUEST_GAP - elapsed
                    await asyncio.sleep(wait_seconds)
                _last_request_time = time.monotonic()
            _stats.log_request(NAMESPACE, url, wait_seconds)
            _stats.incr(NAMESPACE, "http_calls")
            return await _http.get_with_retry(
                client,
                url,
                backoff_seconds=max(_MIN_REQUEST_GAP, 1.0),
                provider=NAMESPACE,
                **kwargs,
            )
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
        doi = doi[len("https://doi.org/") :]
    elif doi.startswith("http://doi.org/"):
        doi = doi[len("http://doi.org/") :]
    elif doi.startswith("doi:"):
        doi = doi[len("doi:") :]
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
            params=params,
        )

        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("Crossref", e)

    if "message" not in data:
        return _parse_error_dict()

    items = data["message"].get("items", [])

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
        # TTL-aware (not cache.has) so a stale-but-present entry is refreshed
        # by fresher search data; mirrors arxiv.search_papers.
        if cache.get(NAMESPACE, "works", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS) is None:
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

        if "message" not in data:
            # Anomalous 200 with no work payload — treat like a parse
            # failure rather than positive-caching an empty {} for the TTL.
            return _parse_error_dict()

        work = data["message"]
        cache.put(NAMESPACE, "works", canonical, work)
        return work

    return await _single_flight.do(canonical, _fetch)
