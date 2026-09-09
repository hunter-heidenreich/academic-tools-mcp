"""OpenCitations client. Reference and citation edges for a DOI."""

from typing import Any
from urllib.parse import quote

import httpx

from ..net import clients, http
from ..net.throttle import Throttle
from ..store import cache, singleflight
from ..util import doinorm, useragent

OPENCITATIONS_BASE_URL = "https://api.opencitations.net/index/v2"
NAMESPACE = "opencitations"

# Agent-facing provider name; every site that names us reads it.
LABEL = "OpenCitations"

_PARSE_ERRORS = http.JSON_PARSE_ERRORS


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``clients.get_client``."""
    return clients.get_client(NAMESPACE, headers=useragent.headers(), timeout=30.0)


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable OpenCitations response."""
    return http.parse_error_dict(LABEL)


# 180 req/min documented. Concurrency of 2 so a graph traversal's references
# and citations fetches overlap rather than serialise.
_MAX_CONCURRENT = 2
_MIN_REQUEST_GAP = 0.334
_MAX_PENDING = 5

_single_flight = singleflight.SingleFlight()

# Incoming citations accrue continuously; a week bounds how stale a count gets.
_POSITIVE_TTL_SECONDS = 7 * 86400.0


_throttle = Throttle(
    namespace=NAMESPACE,
    label=LABEL,
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


async def _throttled_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at OpenCitations' rate. Url-only: ``_get_client`` is the only place to configure it."""
    return await _throttle.get(_get_client(), url, **kwargs)


def canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return doinorm.canonical(doi)


def _parse_ids(raw: Any) -> dict[str, str]:
    """Split OpenCitations' space-delimited ``prefix:value`` ID string into a dict.

    ``"omid:br/062102024238 doi:10.1103/physrevx.2.031001"`` ->
    ``{"omid": "br/062102024238", "doi": "10.1103/physrevx.2.031001"}``

    ``Any``, not ``str``: the value comes from untyped JSON.
    """
    if not isinstance(raw, str) or not raw:
        return {}
    ids: dict[str, str] = {}
    for token in raw.split():
        prefix, sep, value = token.partition(":")
        # A token with no `:`, or with either half empty, is dropped: these flatten
        # onto the record, where a blank `doi` reads to an agent as a real identifier.
        if sep and prefix and value:
            ids[prefix] = value
    return ids


def _format_record(raw: dict[str, Any], id_field: str) -> dict[str, Any]:
    """Flatten one index record: ``id_field``'s IDs, ``creation``, the self-citation flags."""
    record: dict[str, Any] = _parse_ids(raw.get(id_field))
    record["creation"] = raw.get("creation")
    record["journal_self_citation"] = raw.get("journal_sc") == "yes"
    record["author_self_citation"] = raw.get("author_sc") == "yes"
    return record


def _edges_of(records: Any, *, kind: str, id_field: str) -> dict[str, Any] | None:
    """``{kind: [...], count: N}`` from a raw index response, or ``None``.

    ``None`` is a wrong *shape* — anything not a list. Non-dict items are
    skipped and ``count`` follows the survivors. An empty list is a real
    answer, not a miss — don't turn it into one.
    """
    if not isinstance(records, list):
        return None
    formatted = [_format_record(r, id_field) for r in records if isinstance(r, dict)]
    return {kind: formatted, "count": len(formatted)}


async def _fetch_direction(
    doi: str, *, kind: str, id_field: str, force_refresh: bool
) -> dict[str, Any]:
    """Fetch one citation direction: ``{kind: [...], count: N}``, or an error dict.

    ``kind`` is the API path segment, the cache entity and the result key at once;
    ``id_field`` names the link's far end — ``cited`` for references, ``citing`` for
    citations.
    """
    canonical = canonical_doi(doi)
    not_found_error = f"No {kind} found on OpenCitations for DOI: {doi}"

    async def _fetch() -> dict[str, Any]:
        bare_doi = doinorm.normalize(doi)
        # safe="/" keeps the "doi:" prefix and the DOI's own slash literal.
        url = f"{OPENCITATIONS_BASE_URL}/{kind}/doi:{quote(bare_doi, safe='/')}"

        # Two checks, not one: `addresses_a_record` catches the `.`/`..` segment `quote`
        # cannot; the `doi:` prefix keeps the last segment non-empty, so an empty DOI would
        # ask upstream about `doi:` and cache under this DOI's key.
        if not bare_doi or not http.addresses_a_record(url):
            return http.not_found(not_found_error)

        try:
            response = await _throttled_get(url)

            if response.status_code == 404:
                # Rare: an unknown DOI answers 200 with `[]` (`_edges_of`). Still the
                # only branch here that can carry `not_found: True`.
                err = http.not_found(not_found_error)
                cache.put_negative(NAMESPACE, kind, canonical, err)
                return err

            response.raise_for_status()
            records = response.json()
        except _PARSE_ERRORS:
            # Transient, so uncached: a retry re-fetches.
            return _parse_error_dict()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        data = _edges_of(records, kind=kind, id_field=id_field)
        if data is None:
            return _parse_error_dict()

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
    """Fetch outgoing references for a DOI: ``{"references": [...], "count": N}``.

    Each record carries whatever IDs OpenCitations lists for the cited work (doi,
    omid, openalex, pmid), the ``creation`` date and the two self-citation flags.
    """
    return await _fetch_direction(
        doi, kind="references", id_field="cited", force_refresh=force_refresh
    )


async def get_citations(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch incoming citations for a DOI: ``{"citations": [...], "count": N}``.

    Same record shape as :func:`get_references`, for the works citing this DOI.
    """
    return await _fetch_direction(
        doi, kind="citations", id_field="citing", force_refresh=force_refresh
    )
