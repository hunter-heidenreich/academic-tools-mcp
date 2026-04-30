import asyncio
import time
from typing import Any

from . import _clients, _http, _singleflight, _stats, cache, config

OPENALEX_BASE_URL = "https://api.openalex.org"
NAMESPACE = "openalex"

# Rate limiting. OpenAlex's polite-pool soft cap is 10 req/sec; we set
# the gap conservatively at 100ms (10 req/sec) so a fan-out can't burn
# the whole daily budget in a few seconds. Burst cap of 5 mirrors the
# other providers — past 5 stacked requests, the agent gets fast
# feedback instead of silent serialisation.
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 0.1
_MAX_PENDING = 5
_pending: int = 0

# Coalesces concurrent calls for the same DOI / author ID so the
# unified-paper tools (metadata, authors, abstract, bibtex) plus the
# OpenAlex-only tools don't all fire in parallel for one paper.
_single_flight = _singleflight.SingleFlight()

# Positive cache TTL. OpenAlex works grow citation counts and gain
# authors / topics over time; 30 days is long enough to amortise
# repeated reads in a session and short enough that a paper's metadata
# isn't frozen forever. Authors share the same TTL — h_index and
# works_count drift on the same timescale.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def _normalize_doi(doi: str) -> str:
    """Normalize a DOI to the format OpenAlex expects in the URL path.

    Accepts:
      - bare DOI: 10.1234/example
      - prefixed: doi:10.1234/example
      - full URL: https://doi.org/10.1234/example
    Returns the doi: prefixed form for the API path.
    """
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    elif doi.startswith("doi:"):
        doi = doi[len("doi:"):]
    return doi


def _canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return _normalize_doi(doi).lower()


def _build_params() -> dict[str, str]:
    """Build query params from environment config."""
    params: dict[str, str] = {}
    api_key = config.get("OPENALEX_API_KEY")
    if api_key:
        params["api_key"] = api_key
    mailto = config.get("OPENALEX_MAILTO")
    if mailto:
        params["mailto"] = mailto
    return params


def _build_headers() -> dict[str, str]:
    """Build the User-Agent header for OpenAlex's polite pool.

    Falls back to a generic UA when no mailto is configured. Without a
    mailto, OpenAlex still serves requests but at the public-pool rate.
    """
    mailto = config.get("OPENALEX_MAILTO")
    if mailto:
        return {"User-Agent": f"academic-tools-mcp ({mailto})"}
    return {"User-Agent": "academic-tools-mcp"}


def _get_client():
    """Return the persistent AsyncClient for OpenAlex calls."""
    return _clients.get_client(
        NAMESPACE, headers=_build_headers(), timeout=30.0
    )


async def _throttled_get(url: str, **kwargs: Any):
    """Execute a GET respecting the rate gap and burst cap.

    Burst cap raises ``LocalBackpressureError`` rather than queueing
    so a 6th concurrent caller learns to back off instead of silently
    waiting half a second per slot.
    """
    global _last_request_time, _pending
    if _pending >= _MAX_PENDING:
        _stats.incr(NAMESPACE, "backpressure_refusals")
        raise _http.LocalBackpressureError(
            "OpenAlex", _pending, _MAX_PENDING, _MIN_REQUEST_GAP
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
            client = _get_client()
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


def _normalize_author_id(author_id: str) -> str:
    """Normalize an author identifier for the API path.

    Accepts:
      - OpenAlex ID: A5023888391
      - Full OpenAlex URL: https://openalex.org/A5023888391
      - ORCID URL: https://orcid.org/0000-0001-6187-6610
    """
    if author_id.startswith("https://openalex.org/"):
        author_id = author_id[len("https://openalex.org/"):]
    return author_id


def _canonical_author_id(author_id: str) -> str:
    """Return a canonical author ID for cache keying."""
    return _normalize_author_id(author_id).lower()


async def get_author(author_id: str) -> dict[str, Any]:
    """Fetch an author by OpenAlex ID or ORCID, using cache when available.

    Concurrent callers for the same author ID share one fetch via
    single-flight.
    """
    canonical = _canonical_author_id(author_id)

    cached = cache.get(NAMESPACE, "authors", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
    if cached is not None:
        return cached
    neg = cache.get_negative(NAMESPACE, "authors", canonical)
    if neg is not None:
        return neg

    async def _fetch() -> dict[str, Any]:
        cached = cache.get(NAMESPACE, "authors", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
        if cached is not None:
            return cached
        neg = cache.get_negative(NAMESPACE, "authors", canonical)
        if neg is not None:
            return neg

        api_id = _normalize_author_id(author_id)
        params = _build_params()

        try:
            response = await _throttled_get(
                f"{OPENALEX_BASE_URL}/authors/{api_id}",
                params=params,
            )

            if response.status_code == 404:
                err = {"error": f"No author found for ID: {author_id}"}
                cache.put_negative(NAMESPACE, "authors", canonical, err)
                return err

            response.raise_for_status()
            data = response.json()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("OpenAlex", e)

        cache.put(NAMESPACE, "authors", canonical, data)
        return data

    return await _single_flight.do(("author", canonical), _fetch)


async def get_work(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a work by DOI, using cache when available.

    Concurrent callers for the same DOI share one fetch via single-flight.

    ``force_refresh=True`` drops both positive and negative cache entries
    before fetching — useful when the agent needs a fresh citation
    count or to retry an identifier that previously 404'd.
    """
    canonical = _canonical_doi(doi)

    if force_refresh:
        cache.invalidate(NAMESPACE, "works", canonical)
    else:
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

        api_doi = f"doi:{_normalize_doi(doi)}"
        params = _build_params()

        try:
            response = await _throttled_get(
                f"{OPENALEX_BASE_URL}/works/{api_doi}",
                params=params,
            )

            if response.status_code == 404:
                err = {"error": f"No work found for DOI: {doi}"}
                cache.put_negative(NAMESPACE, "works", canonical, err)
                return err

            response.raise_for_status()
            data = response.json()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("OpenAlex", e)

        cache.put(NAMESPACE, "works", canonical, data)
        return data

    return await _single_flight.do(("work", canonical), _fetch)


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    """Reconstruct plain text from OpenAlex's inverted index abstract format."""
    if not inverted_index:
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(word for _, word in word_positions)
