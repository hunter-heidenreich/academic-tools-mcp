"""OpenCitations client. Reference and citation edges for a DOI."""

from typing import Any
from urllib.parse import quote

import httpx

from .. import _clients, _doi, _http, _singleflight, _useragent, cache
from .._throttle import Throttle

OPENCITATIONS_BASE_URL = "https://api.opencitations.net/index/v2"
NAMESPACE = "opencitations"

_PARSE_ERRORS = _http.JSON_PARSE_ERRORS


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``_clients.get_client``."""
    return _clients.get_client(NAMESPACE, headers=_useragent.headers(), timeout=30.0)


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable OpenCitations response."""
    return _http.parse_error_dict("OpenCitations")


# 180 req/min documented. Concurrency of 2 so a graph traversal's references
# and citations fetches overlap rather than serialise.
_MAX_CONCURRENT = 2
_MIN_REQUEST_GAP = 0.334
_MAX_PENDING = 5

_single_flight = _singleflight.SingleFlight()

# Incoming citations accrue continuously; a week bounds how stale a count gets.
_POSITIVE_TTL_SECONDS = 7 * 86400.0


_throttle = Throttle(
    namespace=NAMESPACE,
    label="OpenCitations",
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


async def _throttled_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Execute a GET respecting OpenCitations' rate limit (see ``Throttle.get``)."""
    return await _throttle.get(client, url, **kwargs)


def _normalize_doi(doi: str) -> str:
    """Normalize a DOI to bare form (e.g., 10.1234/example).

    Thin wrapper over :mod:`_doi`, the single home for this logic — a local
    copy lands one paper under several cache keys.
    """
    return _doi.normalize(doi)


def canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return _doi.canonical(doi)


def _parse_ids(raw: Any) -> dict[str, str]:
    """Parse a space-delimited OpenCitations ID string into a dict.

    Input:  "omid:br/062102024238 doi:10.1103/physrevx.2.031001 openalex:W3101024234 pmid:20079334"
    Output: {"omid": "br/062102024238", "doi": "10.1103/physrevx.2.031001",
             "openalex": "W3101024234", "pmid": "20079334"}

    ``Any``, not ``str``: the value comes from untyped JSON.
    """
    if not isinstance(raw, str) or not raw:
        return {}
    ids: dict[str, str] = {}
    for token in raw.split():
        prefix, sep, value = token.partition(":")
        # An empty value is dropped: these flatten onto the record and reach
        # the agent, where a blank `doi` reads as a real identifier.
        if sep and prefix and value:
            ids[prefix] = value
    return ids


def _format_record(raw: dict[str, Any], id_field: str) -> dict[str, Any]:
    """Format a raw OpenCitations citation record into a clean dict."""
    record: dict[str, Any] = _parse_ids(raw.get(id_field))
    record["creation"] = raw.get("creation")
    record["journal_self_citation"] = raw.get("journal_sc") == "yes"
    record["author_self_citation"] = raw.get("author_sc") == "yes"
    return record


async def _fetch_direction(
    doi: str, *, kind: str, id_field: str, force_refresh: bool
) -> dict[str, Any]:
    """Fetch one citation direction for a DOI, returning ``{kind: [...], count: N}``.

    ``kind`` is the API path segment, the cache entity and the result key at
    once; ``id_field`` names the *other* end of the link, which inverts —
    outgoing references read ``cited``, incoming citations read ``citing``.
    """
    canonical = canonical_doi(doi)
    not_found_error = f"No {kind} found on OpenCitations for DOI: {doi}"

    async def _fetch() -> dict[str, Any]:
        bare_doi = _normalize_doi(doi)
        # safe="/" keeps the "doi:" prefix and the DOI's own slash literal.
        url = f"{OPENCITATIONS_BASE_URL}/{kind}/doi:{quote(bare_doi, safe='/')}"

        # Neither check is one `quote` can do: a `.`/`..` segment is removed
        # after encoding, and the `doi:` prefix hides an empty identifier from
        # `addresses_a_record`. Both shorten the path to a live endpoint whose
        # answer would cache under this DOI's key.
        if not bare_doi or not _http.addresses_a_record(url):
            return {"error": not_found_error, "not_found": True}

        try:
            client = _get_client()
            response = await _throttled_get(client, url)

            if response.status_code == 404:
                # Rare — an unknown DOI answers 200 with [] (see below) — but
                # the only branch here that can carry `not_found: True`.
                err = {"error": not_found_error, "not_found": True}
                cache.put_negative(NAMESPACE, kind, canonical, err)
                return err

            response.raise_for_status()
            records = response.json()
        except _PARSE_ERRORS:
            # Transient, so uncached: a retry re-fetches.
            return _parse_error_dict()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("OpenCitations", e)

        if not isinstance(records, list):
            # Wrong shape, not an empty result — never cached for the TTL.
            return _parse_error_dict()

        # An empty list is a real answer: OpenCitations answers "never indexed"
        # and "indexed with zero edges" identically, so it is cached like any
        # other result rather than treated as a miss.
        formatted = [_format_record(r, id_field) for r in records if isinstance(r, dict)]
        data: dict[str, Any] = {kind: formatted, "count": len(formatted)}

        cache.put(NAMESPACE, kind, canonical, data)
        return data

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity=kind,
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=(kind, canonical),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_references(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch outgoing references for a DOI from OpenCitations.

    Each record carries the parsed IDs of the cited work (doi, omid, openalex,
    pmid), its creation date, and the two self-citation flags.
    """
    return await _fetch_direction(
        doi, kind="references", id_field="cited", force_refresh=force_refresh
    )


async def get_citations(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch incoming citations for a DOI from OpenCitations.

    Same record shape as :func:`get_references`, for the works citing this DOI.
    """
    return await _fetch_direction(
        doi, kind="citations", id_field="citing", force_refresh=force_refresh
    )
