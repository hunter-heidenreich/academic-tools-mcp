"""OpenCitations client. Reference and citation edges for a DOI."""

from collections.abc import Callable
from functools import partial
from typing import Any
from urllib.parse import quote

import httpx

from ..net import clients, http
from ..net.throttle import Throttle
from ..store import cache, singleflight
from ..util import config, doinorm, useragent

OPENCITATIONS_BASE_URL = "https://api.opencitations.net/index/v2"
NAMESPACE = "opencitations"

# Agent-facing provider name; every site that names us reads it.
LABEL = "OpenCitations"

_PARSE_ERRORS = http.JSON_PARSE_ERRORS


def _build_headers() -> dict[str, str]:
    """The User-Agent, plus the access token when configured.

    Set on the dict rather than threaded through ``useragent.headers``, whose one job
    is the User-Agent.
    """
    headers = useragent.headers(config.get("OPENCITATIONS_MAILTO"))
    if token := config.get("OPENCITATIONS_ACCESS_TOKEN"):
        headers["authorization"] = token
    return headers


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``clients.get_client``."""
    return clients.get_client(NAMESPACE, headers=_build_headers(), timeout=30.0)


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable OpenCitations response."""
    return http.parse_error_dict(LABEL)


def _error_dict(exc: Exception) -> dict[str, Any]:
    """The shared error dict, plus the one hint only this module can give.

    A rejected token 403s *every* call and a 403 is neither retryable nor a miss, so
    the provider goes dark on a typo with nothing naming the cause. Gated on a token
    being configured: a 403 without one is some other refusal.
    """
    result = http.error_dict(LABEL, exc)
    if (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 403
        and config.get("OPENCITATIONS_ACCESS_TOKEN")
    ):
        result.setdefault(
            "suggestion",
            "OpenCitations rejected the configured OPENCITATIONS_ACCESS_TOKEN. Unset it "
            "to fall back to unauthenticated access — the token buys no rate tier — or "
            "register a new one at https://opencitations.net/accesstoken.",
        )
    return result


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


def _count_of(records: Any) -> dict[str, Any] | None:
    """``{count: N}`` from a raw count response, or ``None`` for a wrong shape.

    The tally arrives as a *string* in a single-element list — ``[{"count": "217"}]``
    — so it is coerced here rather than reaching an agent that compares it against
    ``_edges_of``'s int. ``[{"count": "0"}]`` carries the empty list's "never indexed
    or genuinely edgeless" ambiguity and stays a real answer; an *empty* list does
    not, since these endpoints always carry a row.
    """
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        return None
    raw = records[0].get("count")
    try:
        # `str`-guarded by `int` itself: it rejects None, a list and a dict alike, and
        # accepts the int a future API version might send instead.
        return {"count": int(raw)}  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


async def _fetch_kind(
    doi: str,
    *,
    kind: str,
    noun: str,
    parse: Callable[[Any], dict[str, Any] | None],
    force_refresh: bool,
) -> dict[str, Any]:
    """Fetch one OpenCitations endpoint for a DOI, or return an error dict.

    ``kind`` is the API path segment, the cache entity and the single-flight
    discriminator at once — one ``SingleFlight`` serves the module, so two endpoints
    sharing a key would collide. ``noun`` only words a miss, since ``reference-count``
    is not English. ``parse`` maps the validated body to what this endpoint caches,
    as ``openalex._fetch_singleton``'s ``store=`` does.
    """
    canonical = canonical_doi(doi)
    not_found_error = f"No {noun} found on OpenCitations for DOI: {doi}"

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
                # Rare: an unknown DOI answers 200 with `[]` / a zero count (`_edges_of`,
                # `_count_of`). Still the only branch here that can carry `not_found: True`.
                err = http.not_found(not_found_error)
                cache.put_negative(NAMESPACE, kind, canonical, err)
                return err

            response.raise_for_status()
            records = response.json()
        except _PARSE_ERRORS:
            # Transient, so uncached: a retry re-fetches.
            return _parse_error_dict()
        except http.HTTPX_ERRORS as e:
            return _error_dict(e)

        data = parse(records)
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


async def _fetch_edges(
    doi: str, *, kind: str, id_field: str, force_refresh: bool
) -> dict[str, Any]:
    """One citation direction: ``{kind: [...], count: N}``, or an error dict.

    ``id_field`` names the link's far end — ``cited`` for references, ``citing`` for
    citations.
    """
    return await _fetch_kind(
        doi,
        kind=kind,
        noun=kind,
        parse=partial(_edges_of, kind=kind, id_field=id_field),
        force_refresh=force_refresh,
    )


async def _fetch_count(doi: str, *, kind: str, force_refresh: bool) -> dict[str, Any]:
    """One direction's tally: ``{"count": N}``, or an error dict."""
    return await _fetch_kind(
        doi,
        kind=kind,
        # "No citation-count found for DOI" reads as a broken message, not a miss.
        noun=kind.replace("-", " "),
        parse=_count_of,
        force_refresh=force_refresh,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_references(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch outgoing references for a DOI: ``{"references": [...], "count": N}``.

    Each record carries whatever IDs OpenCitations lists for the cited work (doi,
    omid, openalex, pmid), the ``creation`` date and the two self-citation flags.
    """
    return await _fetch_edges(doi, kind="references", id_field="cited", force_refresh=force_refresh)


async def get_citations(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch incoming citations for a DOI: ``{"citations": [...], "count": N}``.

    Same record shape as :func:`get_references`, for the works citing this DOI.
    """
    return await _fetch_edges(doi, kind="citations", id_field="citing", force_refresh=force_refresh)


async def get_reference_count(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch the outgoing-reference tally for a DOI: ``{"count": N}``.

    The tally only, on its own entity and TTL clock — a ``0`` carries
    :func:`get_references`' ambiguity unchanged.
    """
    return await _fetch_count(doi, kind="reference-count", force_refresh=force_refresh)


async def get_citation_count(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch the incoming-citation tally for a DOI: ``{"count": N}``.

    Bytes instead of megabytes, for a work whose citation list is too large to fetch.
    A fallback, not a faster path: :func:`get_citations` stays the primary.
    """
    return await _fetch_count(doi, kind="citation-count", force_refresh=force_refresh)
