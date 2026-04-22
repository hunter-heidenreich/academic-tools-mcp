import asyncio
import time
from typing import Any

import httpx

from . import _http, cache

OPENCITATIONS_BASE_URL = "https://api.opencitations.net/index/v2"
NAMESPACE = "opencitations"

# Rate limiting: 180 req/min = 3 req/sec.
# Enforce a minimum 334ms gap between requests.
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 0.334  # ~3 req/sec max


async def _throttled_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a GET request respecting OpenCitations' rate limit.

    Enforces: max 1 concurrent request, minimum 334ms gap (180 req/min).
    """
    global _last_request_time
    async with _request_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if _last_request_time > 0 and elapsed < _MIN_REQUEST_GAP:
            await asyncio.sleep(_MIN_REQUEST_GAP - elapsed)
        response = await client.get(url, **kwargs)
        _last_request_time = time.monotonic()
        return response


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
# ID parsing
# ---------------------------------------------------------------------------


def _parse_ids(raw: str | None) -> dict[str, str]:
    """Parse a space-delimited OpenCitations ID string into a dict.

    Input:  "omid:br/062102024238 doi:10.1103/physrevx.2.031001 openalex:W3101024234 pmid:20079334"
    Output: {"omid": "br/062102024238", "doi": "10.1103/physrevx.2.031001",
             "openalex": "W3101024234", "pmid": "20079334"}
    """
    if not raw:
        return {}
    ids: dict[str, str] = {}
    for token in raw.split():
        if ":" in token:
            prefix, _, value = token.partition(":")
            ids[prefix] = value
    return ids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _format_record(raw: dict[str, Any], id_field: str) -> dict[str, Any]:
    """Format a raw OpenCitations citation record into a clean dict."""
    record: dict[str, Any] = _parse_ids(raw.get(id_field))
    record["creation"] = raw.get("creation")
    record["journal_self_citation"] = raw.get("journal_sc") == "yes"
    record["author_self_citation"] = raw.get("author_sc") == "yes"
    return record


async def get_references(doi: str) -> dict[str, Any]:
    """Fetch outgoing references for a DOI from OpenCitations.

    Returns a dict with the list of citation records. Each record contains
    parsed IDs (doi, omid, openalex, pmid), creation date, and self-citation flags.
    """
    canonical = _canonical_doi(doi)

    cached = cache.get(NAMESPACE, "references", canonical)
    if cached is not None:
        return cached

    bare_doi = _normalize_doi(doi)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await _throttled_get(
                client,
                f"{OPENCITATIONS_BASE_URL}/references/doi:{bare_doi}",
            )

        if response.status_code == 404:
            return {"error": f"No references found on OpenCitations for DOI: {doi}"}

        response.raise_for_status()
        records = response.json()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("OpenCitations", e)

    references = [_format_record(r, "cited") for r in records]
    data: dict[str, Any] = {"references": references, "count": len(references)}

    cache.put(NAMESPACE, "references", canonical, data)
    return data


async def get_citations(doi: str) -> dict[str, Any]:
    """Fetch incoming citations for a DOI from OpenCitations.

    Returns a dict with the list of citation records (works that cite this DOI).
    """
    canonical = _canonical_doi(doi)

    cached = cache.get(NAMESPACE, "citations", canonical)
    if cached is not None:
        return cached

    bare_doi = _normalize_doi(doi)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await _throttled_get(
                client,
                f"{OPENCITATIONS_BASE_URL}/citations/doi:{bare_doi}",
            )

        if response.status_code == 404:
            return {"error": f"No citations found on OpenCitations for DOI: {doi}"}

        response.raise_for_status()
        records = response.json()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("OpenCitations", e)

    citations = [_format_record(r, "citing") for r in records]
    data: dict[str, Any] = {"citations": citations, "count": len(citations)}

    cache.put(NAMESPACE, "citations", canonical, data)
    return data
