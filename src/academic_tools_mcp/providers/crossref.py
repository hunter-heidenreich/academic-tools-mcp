import json
from typing import Any
from urllib.parse import quote

import httpx

from .. import _clients, _http, _singleflight, cache, config
from .._throttle import Throttle

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
# of 100ms gives 10 req/sec sustained. Gating lives in ``_throttle``.
_MAX_CONCURRENT = 3
_MIN_REQUEST_GAP = 0.1  # 100ms -> ~10 req/sec max
_MAX_PENDING = 5

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


def canonical_doi(doi: str) -> str:
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
        canonical = canonical_doi(doi)
        # TTL-aware (not cache.has) so a stale-but-present entry is refreshed
        # by fresher search data; mirrors arxiv.search_papers.
        if cache.get(NAMESPACE, "works", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS) is None:
            cache.put(NAMESPACE, "works", canonical, item)

    return {"items": items, "total_results": data["message"].get("total-results")}


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

        if "message" not in data:
            # Anomalous 200 with no work payload — treat like a parse
            # failure rather than positive-caching an empty {} for the TTL.
            return _parse_error_dict()

        work = data["message"]
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
