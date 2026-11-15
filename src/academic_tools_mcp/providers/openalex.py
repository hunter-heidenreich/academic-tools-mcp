"""OpenAlex client: works, authors and the batched multi-work fetch."""

import asyncio
import re
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import quote

import httpx

from ..net import clients, http, stats
from ..net.throttle import SubGap, Throttle
from ..store import cache, singleflight
from ..util import config, doinorm, useragent
from .openalex_parse import (
    _BARE_PMID_FLOOR as _BARE_PMID_FLOOR,
    _BARE_WORK_ID_FLOOR as _BARE_WORK_ID_FLOOR,
    _OPENALEX_URL_RE as _OPENALEX_URL_RE,
    _PMID_RE,
    _PUBMED_URL_RE as _PUBMED_URL_RE,
    _RESPONSE_DOI_URL_RE as _RESPONSE_DOI_URL_RE,
    _ROR_RE as _ROR_RE,
    _ROR_URL_RE as _ROR_URL_RE,
    _WORK_ID_RE,
    _author_path_id,
    _canonical_from_response_doi,
    _extract_batch_pmid,
    _extract_batch_work_id,
    _institution_path_id,
    _looks_like_ror as _looks_like_ror,
    _meta_count,
    _normalize_author_id,
    _normalize_institution_id,
    _usable_http_url as _usable_http_url,
    best_pdf_url as best_pdf_url,
    canonical_author_id,
    canonical_doi,
    canonical_institution_id,
    canonical_work_id,
    is_pmid as is_pmid,
    is_work_id,
    normalize_pmid,
    normalize_work_id as normalize_work_id,
    reconstruct_abstract as reconstruct_abstract,
)

OPENALEX_BASE_URL = "https://api.openalex.org"
NAMESPACE = "openalex"

# Agent-facing provider name; every site that names us reads it.
LABEL = "OpenAlex"

# JSON body: a malformed/truncated 200 is transient, never a miss.
_PARSE_ERRORS = http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable OpenAlex response."""
    return http.parse_error_dict(LABEL)


# A 10x margin under OpenAlex's documented 100 req/sec: the credit budget binds long
# before the rate does. The concurrency cap lets a reference-graph traversal run lookups
# in parallel (OpenAlex documents no concurrency limit); the burst cap, as with every
# other provider, gives a stacked caller feedback instead of silent queueing.
_MAX_CONCURRENT = 4
_MIN_REQUEST_GAP = 0.1
_MAX_PENDING = 5

# Measured, since OpenAlex publishes the tiers but not the per-endpoint cost: a `search=`
# list costs 10 credits where a `filter=` list costs 1 and a singleton 0. Pacing search by
# that ratio spends the budget no faster per second than the cheap path does.
_SEARCH_CREDITS = 10
_LIST_CREDITS = 1
_SEARCH_REQUEST_GAP = _MIN_REQUEST_GAP * (_SEARCH_CREDITS / _LIST_CREDITS)

# Coalesces concurrent calls for one DOI / author ID: the four unified paper tools
# and the OpenAlex-only ones share a single fetch per paper.
_single_flight = singleflight.SingleFlight()

# Long: a work's citation count and topics drift slowly, an author's h_index and
# an institution's works_count on the same timescale — long enough to amortise a
# session's reads, short enough that no entity is frozen. All three share this TTL.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def _build_params() -> dict[str, str]:
    """Build query params from environment config.

    The key is the whole of it: OpenAlex retired `mailto` with the polite pool in Feb
    2026 and meters by key.
    """
    params: dict[str, str] = {}
    api_key = config.get("OPENALEX_API_KEY")
    if api_key:
        params["api_key"] = api_key
    return params


def _build_headers() -> dict[str, str]:
    """The User-Agent and its contact. Buys no rate tier; only the key moves the budget."""
    return useragent.headers(config.get("OPENALEX_MAILTO"))


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``clients.get_client``."""
    return clients.get_client(NAMESPACE, headers=_build_headers(), timeout=30.0)


_throttle = Throttle(
    namespace=NAMESPACE,
    label=LABEL,
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


async def _throttled_get(url: str, *, metered: bool = True, **kwargs: Any) -> httpx.Response:
    """GET at OpenAlex's rate. Url-only: it builds the pooled client itself.

    ``metered=False`` for the classes priced at zero credits — the singletons and
    ``/autocomplete``. Default ``True``: an unmeasured call site is gated, not exempt.
    """
    return await _throttle.get(_get_client(), url, metered=metered, **kwargs)


_search_gap = SubGap(_throttle, min_gap_seconds=_SEARCH_REQUEST_GAP)


async def _throttled_search_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at OpenAlex's tighter *search* rate, then through the ordinary slot.

    `search=` only: the batch fetch hits the same `/works` URL at a tenth the cost, so
    the gate is per call site, not per URL.
    """
    await _search_gap.wait()
    return await _throttled_get(url, **kwargs)


def _warm_work_mappings(work: dict[str, Any], *, doi: str | None = None) -> dict[str, Any]:
    """Cross-warm ``work_ids`` and ``pmids`` mappings from a full OpenAlex work object.

    Every path that receives a full work (``search_works``, ``get_work``,
    ``get_works_batch``, ``resolve_pmid``, ``resolve_work_id``) warms these view
    entities so a follow-up resolution by OpenAlex work ID or PMID costs no HTTP
    request.
    """
    canonical_doi_val = doi if doi is not None else _canonical_from_response_doi(work.get("doi"))
    raw_id = work.get("id")
    openalex_id = raw_id if isinstance(raw_id, str) else None
    mapping = {"doi": canonical_doi_val, "openalex_id": openalex_id}
    if (
        isinstance(openalex_id, str)
        and (w_key := canonical_work_id(openalex_id))
        and _WORK_ID_RE.match(w_key)
    ):
        cache.warm(NAMESPACE, "work_ids", w_key, mapping, max_age_seconds=_POSITIVE_TTL_SECONDS)
    if isinstance(ids := work.get("ids"), dict) and isinstance(raw_pmid := ids.get("pmid"), str):
        pmid_key = normalize_pmid(raw_pmid)
        if _PMID_RE.match(pmid_key):
            cache.warm(NAMESPACE, "pmids", pmid_key, mapping, max_age_seconds=_POSITIVE_TTL_SECONDS)
    return mapping


def _traded_ids(work: dict[str, Any]) -> dict[str, Any]:
    """The id mapping ``pmids`` and ``work_ids`` cache: what a non-DOI id trades for.

    Not the work — that is warmed into ``works`` under its DOI (or under its
    canonical work ID when DOI-less), so the paper keeps one full entry on one
    TTL clock. ``doi`` is bare and canonical, or ``None`` for a work OpenAlex
    indexes without one.
    """
    work_doi = work.get("doi")
    doi = doinorm.canonical(work_doi) if isinstance(work_doi, str) and work_doi else None
    if doi:
        cache.warm(NAMESPACE, "works", doi, work, max_age_seconds=_POSITIVE_TTL_SECONDS)
    elif (
        isinstance(raw_id := work.get("id"), str)
        and (w_key := canonical_work_id(raw_id))
        and _WORK_ID_RE.match(w_key)
    ):
        cache.put(NAMESPACE, "works", w_key, work)
    return _warm_work_mappings(work, doi=doi)


async def resolve_pmid(pmid: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Trade a PMID for the paper's DOI, so no paper caches twice.

    Returns ``{doi, openalex_id}`` — ``doi`` ``None`` when OpenAlex indexes the
    work without one — or the shared ``{error, ...}`` contract. The fetch warms
    ``works`` under that DOI, so the ``get_work`` that follows costs no request;
    a 404 negative-caches under the PMID.
    """
    canonical = normalize_pmid(pmid)

    async def _fetch() -> dict[str, Any]:
        # ``safe=""``: a PMID has no path structure, so a stray slash is an escape.
        api_pmid = quote(canonical, safe="")
        return await _fetch_singleton(
            entity="pmids",
            url=f"{OPENALEX_BASE_URL}/works/pmid:{api_pmid}",
            bare=canonical,
            canonical=canonical,
            not_found_error=f"No work found for PMID: {pmid}",
            store=_traded_ids,
        )

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="pmids",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=("pmid", canonical),
    )


async def resolve_work_id(work_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Trade an OpenAlex work ID for the paper's DOI, so no paper caches twice.

    Same contract as :func:`resolve_pmid`, for the id every OpenCitations graph row and
    every ``search_openalex`` hit carries — including the rows that carry no DOI, which
    is what made them unchainable. ``{doi, openalex_id}``, ``doi`` ``None`` for a work
    OpenAlex indexes without one, or the shared ``{error, ...}`` contract.
    """
    canonical = canonical_work_id(work_id)

    async def _fetch() -> dict[str, Any]:
        # ``safe=""``: a work ID has no path structure, so a stray slash is an escape.
        # No ``openalex:`` prefix — OpenAlex resolves the bare id under ``/works/``.
        api_id = quote(canonical, safe="")
        return await _fetch_singleton(
            entity="work_ids",
            url=f"{OPENALEX_BASE_URL}/works/{api_id}",
            bare=canonical,
            canonical=canonical,
            not_found_error=f"No work found for OpenAlex ID: {work_id}",
            store=_traded_ids,
        )

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="work_ids",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=("work_id", canonical),
    )


async def _fetch_singleton(
    *,
    entity: str,
    url: str,
    bare: str,
    canonical: str,
    not_found_error: str,
    store: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """GET one OpenAlex singleton endpoint, applying the shared caching decisions.

    The body every entity getter shares, so the 404-before-``raise_for_status``
    ordering, the negative-cache write, the shape guard and the uncached
    transient failures cannot drift between them. The caller owns the URL and
    its ``quote`` policy, the canonical key, the wording and the ``sf_key``.

    Both guards are needed: a ``doi:`` prefix keeps the last path segment
    non-empty, so ``bare`` is tested too (as ``opencitations`` does).

    ``store`` maps the validated body to what this entity caches and returns, for
    an entity that is a *view* of a work rather than the work itself — ``pmids``
    files an id mapping and warms ``works`` under the DOI, so one paper does not
    occupy two entries on two TTL clocks. Runs after the shape guard, so it is
    handed a dict with an ``id``. Default: cache the body verbatim.
    """
    if not bare or not http.addresses_a_record(url):
        # Bad identifier: refused before a request is spent, so nothing is cached.
        return http.not_found(not_found_error)

    try:
        # Zero credits, so a spent budget must not refuse it.
        response = await _throttled_get(url, params=_build_params(), metered=False)

        if response.status_code == 404:
            err = http.not_found(not_found_error)
            cache.put_negative(NAMESPACE, entity, canonical, err)
            return err

        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except http.HTTPX_ERRORS as e:
        return http.error_dict(LABEL, e)

    if not isinstance(data, dict) or "id" not in data:
        return _parse_error_dict()

    record = store(data) if store is not None else data
    cache.put(NAMESPACE, entity, canonical, record)
    return record


async def get_author(author_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch an author by OpenAlex ID or ORCID URL, using cache when available.

    Concurrent callers for the same ID share one fetch. ``force_refresh=True``
    drops both cache halves first — h_index and works_count drift.
    """
    canonical = canonical_author_id(author_id)

    async def _fetch() -> dict[str, Any]:
        bare_id = _normalize_author_id(author_id)
        # Neither shape holds a `/`, which `quote(safe="")` would hide from the URL guard.
        return await _fetch_singleton(
            entity="authors",
            url=f"{OPENALEX_BASE_URL}/authors/{_author_path_id(bare_id)}",
            bare="" if "/" in bare_id else bare_id,
            canonical=canonical,
            not_found_error=f"No author found for ID: {author_id}",
        )

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="authors",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=("author", canonical),
    )


async def get_institution(institution_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch an institution by OpenAlex ID or ROR, using cache when available.

    Concurrent callers for the same ID share one fetch. ``force_refresh=True``
    drops both cache halves first — works_count and cited_by_count drift.
    """
    canonical = canonical_institution_id(institution_id)

    async def _fetch() -> dict[str, Any]:
        bare_id = _normalize_institution_id(institution_id)
        # Neither shape holds a `/`, which `quote(safe="")` would hide from the URL guard.
        return await _fetch_singleton(
            entity="institutions",
            url=f"{OPENALEX_BASE_URL}/institutions/{_institution_path_id(bare_id)}",
            bare="" if "/" in bare_id else bare_id,
            canonical=canonical,
            not_found_error=f"No institution found for ID: {institution_id}",
        )

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="institutions",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=("institution", canonical),
    )


def _store_work(work: dict[str, Any]) -> dict[str, Any]:
    """Cross-warm ``work_ids`` and ``pmids`` when a singleton work is fetched by DOI."""
    _warm_work_mappings(work)
    return work


async def get_work(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a work by DOI or OpenAlex work ID, using cache when available.

    Concurrent callers for the same identifier share one fetch. ``force_refresh=True``
    drops both cache halves first — for a fresh citation count, or to retry an
    identifier that previously 404'd.
    """
    if is_work_id(doi):
        canonical = canonical_work_id(doi)

        async def _fetch_by_work_id() -> dict[str, Any]:
            api_id = quote(canonical, safe="")
            return await _fetch_singleton(
                entity="works",
                url=f"{OPENALEX_BASE_URL}/works/{api_id}",
                bare=canonical,
                canonical=canonical,
                not_found_error=f"No work found for OpenAlex ID: {doi}",
                store=_store_work,
            )

        return await cache.cached_lookup(
            single_flight=_single_flight,
            namespace=NAMESPACE,
            entity="works",
            canonical=canonical,
            positive_ttl=_POSITIVE_TTL_SECONDS,
            fetch=_fetch_by_work_id,
            force_refresh=force_refresh,
            sf_key=("work", canonical),
        )

    canonical = canonical_doi(doi)

    async def _fetch() -> dict[str, Any]:
        # Percent-encode so a ``#``/``?`` in the DOI can't truncate the request to the
        # wrong record; the DOI's own slash stays literal and ``doi:`` is added outside.
        bare_doi = doinorm.normalize(doi)
        api_doi = f"doi:{quote(bare_doi, safe='/')}"
        return await _fetch_singleton(
            entity="works",
            url=f"{OPENALEX_BASE_URL}/works/{api_doi}",
            bare=bare_doi,
            canonical=canonical,
            not_found_error=f"No work found for DOI: {doi}",
            store=_store_work,
        )

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="works",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=("work", canonical),
    )


# ``/works?filter=doi:...`` fan-in size. OpenAlex ORs at most ~100 filter values;
# staying well under keeps the URL short and still collapses a chunk of GETs into one.
_BATCH_CHUNK_SIZE = 50

# Our own ceiling on a triage list, not OpenAlex's (per-page allows 200).
MAX_SEARCH_RESULTS = 50


async def search_works(query: str, *, year: int | None = None, rows: int = 10) -> dict[str, Any]:
    """Search OpenAlex works by free text over title, abstract and fulltext.

    Returns ``{"items": [...], "total_results": N | None}`` — dict-shaped hits
    only — or ``{"error": ...}`` on transport/HTTP failure or a wrong-shape body.
    The list is not cached (ad-hoc queries), but each hit with a DOI warms the
    works cache, exactly as ``crossref.search_works`` does.

    **No ``select=``, deliberately**: a projected work would poison the ``works``
    key it warms. The unread bytes never leave this process, and cost nothing:
    ``select=`` does not reduce what a query is metered.

    Paced by ``_search_gap``: this is the expensive call class.
    """
    params = _build_params()
    params["search"] = query
    params["per-page"] = str(min(max(rows, 1), MAX_SEARCH_RESULTS))
    if year is not None:
        params["filter"] = f"publication_year:{year}"

    try:
        response = await _throttled_search_get(f"{OPENALEX_BASE_URL}/works", params=params)
        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except http.HTTPX_ERRORS as e:
        return http.error_dict(LABEL, e)

    # A wrong shape here either raises out of the provider or reads as an empty
    # result set, and "no papers match" ends the agent's search.
    if not isinstance(data, dict):
        return _parse_error_dict()
    results = data.get("results")
    if not isinstance(results, list):
        return _parse_error_dict()
    items = [item for item in results if isinstance(item, dict)]

    for item in items:
        # Strips the resolver prefix a response DOI carries, and shape-guards:
        # this runs outside the ``try`` above.
        if canonical := _canonical_from_response_doi(item.get("doi")):
            cache.warm(NAMESPACE, "works", canonical, item, max_age_seconds=_POSITIVE_TTL_SECONDS)
        elif (
            isinstance(raw_id := item.get("id"), str)
            and (w_key := canonical_work_id(raw_id))
            and _WORK_ID_RE.match(w_key)
        ):
            cache.warm(NAMESPACE, "works", w_key, item, max_age_seconds=_POSITIVE_TTL_SECONDS)
        _warm_work_mappings(item, doi=canonical)

    return {"items": items, "total_results": _meta_count(data)}


async def search_authors(query: str, *, rows: int = 10) -> dict[str, Any]:
    """Search OpenAlex authors by name.

    Returns ``{"items": [...], "total_results": N | None}`` — dict-shaped hits
    only — or ``{"error": ...}`` on transport/HTTP failure or a wrong-shape body.
    The list is not cached (ad-hoc queries), but each hit warms the ``authors``
    cache under the key ``get_author`` reads, so chaining one costs no request.

    **No ``select=``**, as in ``search_works``: a projected author would poison
    the ``authors`` key it warms. No year filter — it does not narrow a person.
    Paced by ``_search_gap``, as ``search_works`` is.
    """
    params = _build_params()
    params["search"] = query
    params["per-page"] = str(min(max(rows, 1), MAX_SEARCH_RESULTS))

    try:
        response = await _throttled_search_get(f"{OPENALEX_BASE_URL}/authors", params=params)
        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except http.HTTPX_ERRORS as e:
        return http.error_dict(LABEL, e)

    # A wrong shape here either raises out of the provider or reads as an empty
    # result set, and "no such person" ends the agent's search.
    if not isinstance(data, dict):
        return _parse_error_dict()
    results = data.get("results")
    if not isinstance(results, list):
        return _parse_error_dict()
    items = [item for item in results if isinstance(item, dict)]

    for item in items:
        # Shape-guarded before `canonical_author_id` strips it: this runs
        # outside the ``try`` above.
        raw_id = item.get("id")
        if isinstance(raw_id, str) and (canonical := canonical_author_id(raw_id)):
            cache.warm(NAMESPACE, "authors", canonical, item, max_age_seconds=_POSITIVE_TTL_SECONDS)

    return {"items": items, "total_results": _meta_count(data)}


# Closed: the value reaches a URL path segment, so an unvalidated one is a request-side hole.
AutocompleteEntity = Literal["works", "authors", "institutions", "sources"]


async def autocomplete(query: str, *, entity_type: AutocompleteEntity = "works") -> dict[str, Any]:
    """Typeahead over one OpenAlex entity collection. Costs no credits.

    Returns ``{"items": [...], "total_results": N | None}`` — dict-shaped hits only — or
    ``{"error": ...}`` on failure, as ``search_works`` does. Hits are projections, so this
    **never warms the cache**: one written under a singleton key would answer a later
    ``get_work`` / ``get_author`` / ``get_institution`` with a fraction of the object, the
    hazard ``search_works`` refuses ``select=`` over.

    No ``rows``: OpenAlex rejects ``per-page`` here and always serves ten.
    """
    params = _build_params()
    params["q"] = query

    try:
        # `safe=""`: a collection name has no path structure, so a stray slash is an escape.
        url = f"{OPENALEX_BASE_URL}/autocomplete/{quote(entity_type, safe='')}"
        response = await _throttled_get(url, params=params, metered=False)  # zero credits
        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except http.HTTPX_ERRORS as e:
        return http.error_dict(LABEL, e)

    # A wrong shape here either raises out of the provider or reads as an empty
    # result set, and "no matches" ends the agent's search.
    if not isinstance(data, dict):
        return _parse_error_dict()
    results = data.get("results")
    if not isinstance(results, list):
        return _parse_error_dict()

    return {
        "items": [item for item in results if isinstance(item, dict)],
        "total_results": _meta_count(data),
    }


async def _fetch_chunk(
    chunk: list[str],
    *,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch one ``/works?filter=doi:a|b|c`` chunk, coalesced by single-flight.

    Returns ``{canonical: work_or_error}`` for every DOI in ``chunk``; the
    single-flight key sorts it, so argument order can't defeat coalescing.

    **Deliberately not deep-copied**, unlike ``cache.cached_lookup``: the aliasing
    hazard is *within* a chunk and per-key construction closes it, and copying a whole
    chunk of works on every batch call is not the cost of copying one record. A consumer
    that needs to mutate one copies that one.
    """
    # ``force_refresh`` rides in the key and nowhere else: the caller has already
    # invalidated, so all that is left is not coalescing with a plain call.
    sf_key = ("works_batch", tuple(sorted(chunk)), force_refresh)

    async def _runner() -> dict[str, dict[str, Any]]:
        # Bypasses ``cache.cached_lookup``, so it books its own misses — one per DOI.
        for _ in chunk:
            stats.incr(NAMESPACE, "cache_misses")
        return await _fetch_chunk_uncoalesced(chunk)

    return await _single_flight.do(sf_key, _runner)


async def _fetch_chunk_uncoalesced(chunk: list[str]) -> dict[str, dict[str, Any]]:
    """The actual batch GET + result mapping for one chunk."""
    out: dict[str, dict[str, Any]] = {}
    params = _build_params()
    # OpenAlex filter syntax: ``|`` is OR, and it wants the bare DOI, no resolver prefix.
    params["filter"] = "doi:" + "|".join(chunk)
    params["per-page"] = str(len(chunk))

    try:
        response = await _throttled_get(f"{OPENALEX_BASE_URL}/works", params=params)
        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        # A garbled 200 body is transient: every DOI in the chunk gets a retryable
        # error, nothing is negative-cached (a retry must re-fetch), and we can't tell
        # which DOI upstream meant to error on. Invariant: one fresh dict per key —
        # ``dict.fromkeys`` would alias one object across the chunk, so a caller
        # mutating its error corrupts all of them.
        return {c: _parse_error_dict() for c in chunk}
    except http.HTTPX_ERRORS as e:
        return {c: http.error_dict(LABEL, e) for c in chunk}

    if not isinstance(data, dict):
        return {c: _parse_error_dict() for c in chunk}

    # No ``or []`` ahead of the guard: it turns a falsy wrong shape — and a body with no
    # ``results`` at all — into an empty page, which the accounting below then reads as
    # every DOI in the chunk being definitively absent.
    results = data.get("results")
    if not isinstance(results, list):
        return {c: _parse_error_dict() for c in chunk}

    chunk_set = set(chunk)
    seen_in_chunk: set[str] = set()
    unattributed = 0
    for work in results:
        work_canonical = (
            _canonical_from_response_doi(work.get("doi")) if isinstance(work, dict) else None
        )
        if work_canonical is None or work_canonical not in chunk_set:
            # Unattributable — a non-dict entry, no usable ``doi``, or a DOI we did not
            # ask for: still real data worth caching, but the response no longer accounts
            # for itself.
            unattributed += 1
            if work_canonical is not None:
                cache.put(NAMESPACE, "works", work_canonical, work)
                _warm_work_mappings(work, doi=work_canonical)
            continue
        cache.put(NAMESPACE, "works", work_canonical, work)
        _warm_work_mappings(work, doi=work_canonical)
        out[work_canonical] = work
        seen_in_chunk.add(work_canonical)

    # A DOI we asked for and didn't get back is a definitive miss only if the response
    # accounted for itself: after an unattributable record or a truncated page it may well
    # exist, and negative-caching would poison a live DOI for the negative TTL.
    reported = _meta_count(data)
    truncated = reported is not None and reported > len(results)
    trustworthy = unattributed == 0 and not truncated

    for canonical in chunk:
        if canonical in seen_in_chunk:
            continue
        err: dict[str, Any] = {"error": f"No work found for DOI: {canonical}"}
        if trustworthy:
            err["not_found"] = True
            cache.put_negative(NAMESPACE, "works", canonical, err)
        else:
            # Inconclusive rather than absent — let the caller retry.
            err["retryable"] = True
        out[canonical] = err

    return out


async def get_works_batch(
    dois: list[str],
    *,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch many OpenAlex works in batched HTTP calls.

    Returns ``{canonical_doi: work_or_error_dict}`` for every input DOI, keyed
    in first-appearance order. Cached entries (positive or negative) are served
    without a network call; the *misses* are grouped into
    ``/works?filter=doi:...|...`` calls of up to ``_BATCH_CHUNK_SIZE``, and each
    resolved work is written to the singleton cache, so a later ``get_work`` is
    a free hit. ``force_refresh=True`` drops cached entries first.

    **Batching buys wall clock, not credits**: a `filter=` list costs 1 where the
    singletons it replaces cost 0, but those singletons are paced ``_MIN_REQUEST_GAP``
    apart, so a full chunk of them takes seconds where the one list call does not.

    A transient failure contaminates its whole chunk: one HTTP failure doesn't
    say which DOI the upstream meant to error on.
    """
    canonicals_in_order: list[str] = []
    seen: set[str] = set()
    for doi in dois:
        canonical = canonical_doi(doi)
        if canonical in seen:
            continue
        seen.add(canonical)
        canonicals_in_order.append(canonical)

    out: dict[str, dict[str, Any]] = {}
    misses: list[str] = []

    for canonical in canonicals_in_order:
        if force_refresh:
            cache.invalidate(NAMESPACE, "works", canonical)
            misses.append(canonical)
            continue
        cached = cache.get(NAMESPACE, "works", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
        if cached is not None:
            out[canonical] = cached
            continue
        neg = cache.get_negative(NAMESPACE, "works", canonical)
        if neg is not None:
            out[canonical] = neg
            continue
        misses.append(canonical)

    # A DOI holding a filter metacharacter (``|`` = OR, ``,`` = AND) would corrupt the
    # OR-joined query and silently shift which records resolve, so it goes through
    # ``get_work``'s percent-encoded path endpoint instead.
    safe_misses: list[str] = []
    unsafe_misses: list[str] = []
    for canonical in misses:
        target = unsafe_misses if "|" in canonical or "," in canonical else safe_misses
        target.append(canonical)

    chunks = [
        safe_misses[start : start + _BATCH_CHUNK_SIZE]
        for start in range(0, len(safe_misses), _BATCH_CHUNK_SIZE)
    ]

    # The ``Throttle``, not this gather, bounds the real parallelism.
    # ``return_exceptions=False`` is safe: every task converts its own failures already.
    results = await asyncio.gather(
        *(get_work(c, force_refresh=force_refresh) for c in unsafe_misses),
        *(_fetch_chunk(chunk, force_refresh=force_refresh) for chunk in chunks),
    )
    for canonical, result in zip(unsafe_misses, results[: len(unsafe_misses)], strict=True):
        out[canonical] = result
    for chunk_out in results[len(unsafe_misses) :]:
        out.update(chunk_out)

    # Input order, not resolution order; total by construction — every canonical is a
    # cache hit or lands in one fan-out branch.
    return {c: out[c] for c in canonicals_in_order}


async def _fetch_id_chunk(
    chunk: list[str],
    *,
    entity: str,
    filter_key: str,
    extract_key: Callable[[dict[str, Any]], str | None],
    not_found_label: str,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch one ``/works?filter=<filter_key>:a|b|c`` chunk, coalesced by single-flight."""
    sf_key = (f"{entity}_batch", tuple(sorted(chunk)), force_refresh)

    async def _runner() -> dict[str, dict[str, Any]]:
        for _ in chunk:
            stats.incr(NAMESPACE, "cache_misses")
        return await _fetch_id_chunk_uncoalesced(
            chunk,
            entity=entity,
            filter_key=filter_key,
            extract_key=extract_key,
            not_found_label=not_found_label,
        )

    return await _single_flight.do(sf_key, _runner)


async def _fetch_id_chunk_uncoalesced(
    chunk: list[str],
    *,
    entity: str,
    filter_key: str,
    extract_key: Callable[[dict[str, Any]], str | None],
    not_found_label: str,
) -> dict[str, dict[str, Any]]:
    """The actual batch GET + mapping extraction for one PMID or work-ID chunk."""
    out: dict[str, dict[str, Any]] = {}
    params = _build_params()
    params["filter"] = f"{filter_key}:" + "|".join(chunk)
    params["per-page"] = str(len(chunk))

    try:
        response = await _throttled_get(f"{OPENALEX_BASE_URL}/works", params=params)
        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        return {c: _parse_error_dict() for c in chunk}
    except http.HTTPX_ERRORS as e:
        return {c: http.error_dict(LABEL, e) for c in chunk}

    if not isinstance(data, dict):
        return {c: _parse_error_dict() for c in chunk}

    results = data.get("results")
    if not isinstance(results, list):
        return {c: _parse_error_dict() for c in chunk}

    chunk_set = set(chunk)
    seen_in_chunk: set[str] = set()
    unattributed = 0
    for work in results:
        if not isinstance(work, dict):
            unattributed += 1
            continue
        key = extract_key(work)
        has_work_id = (
            isinstance(raw_id := work.get("id"), str)
            and bool(w_key := canonical_work_id(raw_id))
            and bool(_WORK_ID_RE.match(w_key))
        )
        mapping = (
            _traded_ids(work)
            if (
                key is not None
                or _canonical_from_response_doi(work.get("doi")) is not None
                or has_work_id
            )
            else None
        )
        if key is None or key not in chunk_set or mapping is None:
            unattributed += 1
            continue
        cache.put(NAMESPACE, entity, key, mapping)
        out[key] = mapping
        seen_in_chunk.add(key)

    reported = _meta_count(data)
    truncated = reported is not None and reported > len(results)
    trustworthy = unattributed == 0 and not truncated

    for canonical in chunk:
        if canonical in seen_in_chunk:
            continue
        err = http.not_found(f"No work found for {not_found_label}: {canonical}")
        if trustworthy:
            cache.put_negative(NAMESPACE, entity, canonical, err)
        else:
            err = {"error": err["error"], "retryable": True}
        out[canonical] = err

    return out


async def _resolve_ids_batch(
    identifiers: list[str],
    *,
    entity: str,
    filter_key: str,
    canonicalize: Callable[[str], str],
    shape_re: re.Pattern[str],
    extract_key: Callable[[dict[str, Any]], str | None],
    not_found_label: str,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Shared batch resolver for PMIDs and OpenAlex work IDs."""
    canonicals_in_order: list[str] = []
    seen: set[str] = set()
    for ident in identifiers:
        canonical = canonicalize(ident)
        if canonical in seen:
            continue
        seen.add(canonical)
        canonicals_in_order.append(canonical)

    out: dict[str, dict[str, Any]] = {}
    misses: list[str] = []

    for canonical in canonicals_in_order:
        if not shape_re.match(canonical):
            out[canonical] = http.not_found(f"No work found for {not_found_label}: {canonical}")
            continue
        if force_refresh:
            cache.invalidate(NAMESPACE, entity, canonical)
            misses.append(canonical)
            continue
        cached = cache.get(NAMESPACE, entity, canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
        if cached is not None:
            out[canonical] = cached
            continue
        neg = cache.get_negative(NAMESPACE, entity, canonical)
        if neg is not None:
            out[canonical] = neg
            continue
        misses.append(canonical)

    chunks = [
        misses[start : start + _BATCH_CHUNK_SIZE]
        for start in range(0, len(misses), _BATCH_CHUNK_SIZE)
    ]

    results = await asyncio.gather(
        *(
            _fetch_id_chunk(
                chunk,
                entity=entity,
                filter_key=filter_key,
                extract_key=extract_key,
                not_found_label=not_found_label,
                force_refresh=force_refresh,
            )
            for chunk in chunks
        )
    )
    for chunk_out in results:
        out.update(chunk_out)

    return {c: out[c] for c in canonicals_in_order}


async def resolve_pmids_batch(
    pmids: list[str],
    *,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Resolve many PMIDs in batched ``/works?filter=pmid:...|...`` calls.

    Returns ``{normalized_pmid: mapping_or_error_dict}`` in first-appearance
    order, warming ``works``, ``work_ids``, and ``pmids`` for every resolved
    work so follow-up ``resolve_pmid`` and ``get_works_batch`` calls hit the
    cache with zero additional HTTP requests.
    """
    return await _resolve_ids_batch(
        pmids,
        entity="pmids",
        filter_key="pmid",
        canonicalize=normalize_pmid,
        shape_re=_PMID_RE,
        extract_key=_extract_batch_pmid,
        not_found_label="PMID",
        force_refresh=force_refresh,
    )


async def resolve_work_ids_batch(
    work_ids: list[str],
    *,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Resolve many OpenAlex work IDs in batched ``/works?filter=openalex:...|...`` calls.

    Returns ``{canonical_work_id: mapping_or_error_dict}`` in first-appearance
    order, warming ``works``, ``work_ids``, and ``pmids`` for every resolved
    work so follow-up ``resolve_work_id`` and ``get_works_batch`` calls hit the
    cache with zero additional HTTP requests.
    """
    return await _resolve_ids_batch(
        work_ids,
        entity="work_ids",
        filter_key="openalex",
        canonicalize=canonical_work_id,
        shape_re=_WORK_ID_RE,
        extract_key=_extract_batch_work_id,
        not_found_label="OpenAlex ID",
        force_refresh=force_refresh,
    )
