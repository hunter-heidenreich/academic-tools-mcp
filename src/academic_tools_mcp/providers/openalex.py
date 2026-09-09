"""OpenAlex client: works, authors and the batched multi-work fetch."""

import asyncio
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from ..net import clients, http, stats
from ..net.throttle import Throttle
from ..store import cache, singleflight
from ..util import config, doinorm, useragent

OPENALEX_BASE_URL = "https://api.openalex.org"
NAMESPACE = "openalex"

# Agent-facing provider name; every site that names us reads it.
LABEL = "OpenAlex"

# JSON body: a malformed/truncated 200 is transient, never a miss.
_PARSE_ERRORS = http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable OpenAlex response."""
    return http.parse_error_dict(LABEL)


# The gap paces us at OpenAlex's documented 10 req/sec, so a fan-out can't burn the
# daily budget in seconds; the concurrency cap lets a reference-graph traversal run
# lookups in parallel (OpenAlex documents no concurrency limit); the burst cap, as with
# every other provider, gives a stacked caller feedback instead of silent queueing.
_MAX_CONCURRENT = 4
_MIN_REQUEST_GAP = 0.1
_MAX_PENDING = 5

# Coalesces concurrent calls for one DOI / author ID: the four unified paper tools
# and the OpenAlex-only ones share a single fetch per paper.
_single_flight = singleflight.SingleFlight()

# Long: a work's citation count and topics drift slowly, an author's h_index and
# works_count on the same timescale — long enough to amortise a session's reads, short
# enough that neither entity is frozen. Both entities share this TTL.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return doinorm.canonical(doi)


def best_pdf_url(work: dict[str, Any]) -> str | None:
    """Pick the best open-access *PDF* URL from a raw OpenAlex work, or None.

    A direct PDF link over a landing page, in order: ``best_oa_location``'s
    (OpenAlex's chosen best OA copy), ``primary_location``'s (the version of
    record, if OA), then ``open_access.oa_url`` — frequently a landing page.

    Type-checked all the way down, not annotation-trusted: this is the OA download
    trust boundary, and whatever it returns is fetched.
    """
    for obj_key, url_key in (
        ("best_oa_location", "pdf_url"),
        ("primary_location", "pdf_url"),
        ("open_access", "oa_url"),
    ):
        obj = work.get(obj_key)
        if not isinstance(obj, dict):
            continue
        url = obj.get(url_key)
        if isinstance(url, str) and url:
            return url
    return None


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
    """The polite-pool User-Agent. Sent either way; the mailto is what joins."""
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


async def _throttled_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at OpenAlex's rate. Url-only: it builds the pooled client itself."""
    return await _throttle.get(_get_client(), url, **kwargs)


# The openalex.org URL spellings an entity ID is pasted in — the latitude
# ``doinorm._DOI_URL_RE`` and ``arxiv._ARXIV_URL_RE`` carry, for the same reason.
# Gated on an entity-shaped tail, so an ORCID URL falls through untouched.
_OPENALEX_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|api\.)?openalex\.org/(?:\w+/)?([a-z]\d+)/?$",
    re.IGNORECASE,
)


def _normalize_author_id(author_id: str) -> str:
    """Normalize an author identifier for the API path.

    A bare OpenAlex ID (``A5023888391``), any openalex.org URL
    ``_OPENALEX_URL_RE`` covers, or an ORCID URL — which passes through
    verbatim, that being the spelling OpenAlex itself resolves.
    """
    author_id = author_id.strip()
    if m := _OPENALEX_URL_RE.match(author_id):
        return m.group(1)
    return author_id


def canonical_author_id(author_id: str) -> str:
    """Return a canonical author ID for cache keying."""
    return _normalize_author_id(author_id).lower()


# PubMed's own URL, plus the legacy ``/pubmed/`` path older papers still print.
# As permissive as ``_ARXIV_URL_RE``, for the same reason.
_PUBMED_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)"
    r"/(\d+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# The whole live PMID range fits in 8 digits; ``is_pmid`` owns the bare-run floor.
_PMID_RE = re.compile(r"^\d{1,8}$")


def normalize_pmid(pmid: str) -> str:
    """Normalize a PubMed identifier to bare digits.

    Accepts a bare run, an any-case ``pmid:`` prefix (OpenCitations' own
    spelling) and a PubMed URL per ``_PUBMED_URL_RE``. Anything unrecognised
    comes back stripped of whitespace and any prefix; ``is_pmid`` decides
    whether that is an error. Idempotent.
    """
    pmid = pmid.strip()

    # In a loop, as ``arxiv``/``doinorm`` do: doubled prefixes occur in pasted citations.
    while pmid[:5].lower() == "pmid:":
        pmid = pmid[5:].strip()

    if m := _PUBMED_URL_RE.match(pmid):
        return m.group(1)
    return pmid


def is_pmid(identifier: str) -> bool:
    """The shape test the tool layer resolves on, over the normalized form.

    **Two tiers, deliberately.** An explicit ``pmid:`` prefix or PubMed URL is
    unambiguous, so any 1-8 digit id is claimed. A *bare* run is claimed only at
    7-8 digits — the modern PMID range, and an unlikely hand-written label — so
    a freeform ``import_paper(file, "1234")`` label keeps routing to ``manual``.
    """
    stripped = identifier.strip()
    normalized = normalize_pmid(stripped)
    if not _PMID_RE.match(normalized):
        return False
    # An explicit marker is what ``normalize_pmid`` removed; a bare run is unchanged.
    return normalized != stripped or len(normalized) >= 7


def _pmid_ids(work: dict[str, Any]) -> dict[str, Any]:
    """The id mapping ``pmids`` caches: what a PMID trades itself for.

    Not the work — that is warmed into ``works`` under its DOI, so the paper
    keeps one full entry on one TTL clock. ``doi`` is bare and canonical, or
    ``None`` for a work OpenAlex indexes without one.
    """
    work_doi = work.get("doi")
    doi = doinorm.canonical(work_doi) if isinstance(work_doi, str) and work_doi else None
    if doi:
        cache.warm(NAMESPACE, "works", doi, work, max_age_seconds=_POSITIVE_TTL_SECONDS)
    work_id = work.get("id")
    return {"doi": doi, "openalex_id": work_id if isinstance(work_id, str) else None}


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
            store=_pmid_ids,
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
        response = await _throttled_get(url, params=_build_params())

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
        # Percent-encode so reserved characters can't split the path; ``:`` and ``/``
        # stay literal, keeping the ORCID-URL spelling OpenAlex resolves byte-identical.
        bare_id = _normalize_author_id(author_id)
        api_id = quote(bare_id, safe=":/")
        return await _fetch_singleton(
            entity="authors",
            url=f"{OPENALEX_BASE_URL}/authors/{api_id}",
            bare=bare_id,
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


async def get_work(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a work by DOI, using cache when available.

    Concurrent callers for the same DOI share one fetch. ``force_refresh=True``
    drops both cache halves first — for a fresh citation count, or to retry an
    identifier that previously 404'd.
    """
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

# The resolver prefixes a *response* DOI can carry: ``doinorm._DOI_URL_RE``'s host
# set, deliberately without its DOI-shape requirement on the tail.
_RESPONSE_DOI_URL_RE = re.compile(r"^https?://(?:dx\.|www\.)?doi\.org/", re.IGNORECASE)


def _canonical_from_response_doi(work_doi: Any) -> str | None:
    """The canonical bare DOI from an OpenAlex work's ``doi``, or None.

    Maps a batch response back to the keys we asked for, so it strips the
    resolver prefix *unconditionally* where ``doinorm.canonical`` strips only a
    DOI-shaped path. ``Any``: this runs outside any ``except``.
    """
    if not isinstance(work_doi, str):
        return None
    return _RESPONSE_DOI_URL_RE.sub("", work_doi.strip()).lower() or None


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

    results = data.get("results") or []
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
            continue
        cache.put(NAMESPACE, "works", work_canonical, work)
        out[work_canonical] = work
        seen_in_chunk.add(work_canonical)

    # A DOI we asked for and didn't get back is a definitive miss only if the response
    # accounted for itself: after an unattributable record or a truncated page it may well
    # exist, and negative-caching would poison a live DOI for the negative TTL.
    meta = data.get("meta")
    reported = meta.get("count") if isinstance(meta, dict) else None
    truncated = isinstance(reported, int) and reported > len(results)
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


def reconstruct_abstract(inverted_index: Any) -> str:
    """Reconstruct plain text from OpenAlex's inverted index abstract format.

    ``Any``: a malformed index reconstructs to ``""`` rather than raising past
    ``get_paper_abstract``, which has no ``except``.
    """
    if not isinstance(inverted_index, dict):
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        word_positions.extend((pos, word) for pos in positions if isinstance(pos, int))
    word_positions.sort()
    return " ".join(word for _, word in word_positions)
