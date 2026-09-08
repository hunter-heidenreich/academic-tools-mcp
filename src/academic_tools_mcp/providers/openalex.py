"""OpenAlex client: works, authors, topics and the batched multi-work fetch."""

import asyncio
import re
from typing import Any
from urllib.parse import quote

import httpx

from ..net import clients, http, stats
from ..net.throttle import Throttle
from ..store import cache, singleflight
from ..util import config, doinorm, useragent

OPENALEX_BASE_URL = "https://api.openalex.org"
NAMESPACE = "openalex"

# Agent-facing provider name; every site that names us reads it (providers.md).
LABEL = "OpenAlex"

# The OpenAlex API returns JSON; a malformed/truncated 200 body raises
# ``json.JSONDecodeError`` on ``.json()``. It is handled alongside the HTTP
# errors so the tool always returns the uniform ``{error}`` contract rather
# than crashing on a garbled response. Mirrors crossref/biorxiv.
_PARSE_ERRORS = http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable OpenAlex response."""
    return http.parse_error_dict(LABEL)


# Rate limiting. OpenAlex's polite-pool soft cap is 10 req/sec; we set
# the gap conservatively at 100ms (10 req/sec) so a fan-out can't burn
# the whole daily budget in a few seconds. Concurrency cap of 4 lets
# reference-graph traversals run multiple lookups in parallel — well
# under any concurrency limit OpenAlex enforces and a big win on the
# previous serialise-everything model. Burst cap of 5 mirrors the
# other providers — past 5 stacked requests, the agent gets fast
# feedback instead of silent serialisation.
_MAX_CONCURRENT = 4
_MIN_REQUEST_GAP = 0.1
_MAX_PENDING = 5

# Coalesces concurrent calls for the same DOI / author ID so the
# unified-paper tools (metadata, authors, abstract, bibtex) plus the
# OpenAlex-only tools don't all fire in parallel for one paper.
_single_flight = singleflight.SingleFlight()

# Positive cache TTL. OpenAlex works grow citation counts and gain
# authors / topics over time; 30 days is long enough to amortise
# repeated reads in a session and short enough that a paper's metadata
# isn't frozen forever. Authors share the same TTL — h_index and
# works_count drift on the same timescale.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return doinorm.canonical(doi)


def best_pdf_url(work: dict[str, Any]) -> str | None:
    """Pick the best open-access *PDF* URL from a raw OpenAlex work, or None.

    A direct PDF link over a landing page, in order: ``best_oa_location``'s
    (OpenAlex's chosen best OA copy), ``primary_location``'s (the version of
    record, if OA), then ``open_access.oa_url`` — frequently a landing page.

    Type-checked, not null-checked: this is the OA download trust boundary.
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


async def _fetch_singleton(
    *,
    entity: str,
    url: str,
    bare: str,
    canonical: str,
    not_found_error: str,
) -> dict[str, Any]:
    """GET one OpenAlex singleton endpoint, applying the shared caching decisions.

    The body every entity getter shares, so the 404-before-``raise_for_status``
    ordering, the negative-cache write, the shape guard and the uncached
    transient failures cannot drift between them. The caller owns the URL and
    its ``quote`` policy, the canonical key, the wording and the ``sf_key``.

    Both guards are needed: a ``doi:`` prefix keeps the last path segment
    non-empty, so ``bare`` is tested too (as ``opencitations`` does).
    """
    if not bare or not http.addresses_a_record(url):
        # Definitively a bad identifier, and refused before it is spent
        # upstream — as ``acl._strip_acl_prefix`` refuses an empty suffix.
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

    cache.put(NAMESPACE, entity, canonical, data)
    return data


async def get_author(author_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch an author by OpenAlex ID or ORCID, using cache when available.

    Concurrent callers for the same ID share one fetch. ``force_refresh=True``
    drops both cache halves first — h_index and works_count drift.
    """
    canonical = canonical_author_id(author_id)

    async def _fetch() -> dict[str, Any]:
        # Percent-encode as get_work does, so reserved characters can't split
        # the path. ``:`` and ``/`` stay literal so the ORCID-URL spelling
        # OpenAlex resolves survives byte-identical.
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
        # Percent-encode the DOI so reserved characters (#, ?, …) aren't
        # misread as a URL fragment/query and silently truncate the request
        # to the wrong record. The prefix/suffix slash stays literal
        # (safe="/"); the "doi:" path prefix is added outside the encode.
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


# Per-batch chunk size for /works?filter=doi:... fan-in. OpenAlex's
# upper bound on the number of OR-joined IDs in a filter is around 100;
# 50 keeps us comfortably under any URL-length limits while still
# collapsing 50× single GETs into one HTTP call. With a 100 ms gap +
# concurrency 4, the saving is dramatic on reference-graph traversals.
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

    **Deliberately not deep-copied**, unlike ``cache.cached_lookup``: the
    aliasing hazard was *within* a chunk, which per-key construction fixes,
    and copying fifty full works on every batch call is not the same cost as
    copying one record. A consumer that needs to mutate one copies that one.
    """
    # ``force_refresh`` rides in the key and nowhere else: the caller has already
    # invalidated, so all that is left is not coalescing with a plain call.
    sf_key = ("works_batch", tuple(sorted(chunk)), force_refresh)

    async def _runner() -> dict[str, dict[str, Any]]:
        # This path bypasses cache.cached_lookup, so it books its own misses —
        # one per DOI about to be resolved upstream.
        for _ in chunk:
            stats.incr(NAMESPACE, "cache_misses")
        return await _fetch_chunk_uncoalesced(chunk)

    return await _single_flight.do(sf_key, _runner)


async def _fetch_chunk_uncoalesced(chunk: list[str]) -> dict[str, dict[str, Any]]:
    """The actual batch GET + result mapping for one chunk."""
    out: dict[str, dict[str, Any]] = {}
    params = _build_params()
    # OpenAlex's filter syntax: pipe-separated values are OR'd. The
    # bare DOI (no doi.org prefix) is what the filter expects.
    params["filter"] = "doi:" + "|".join(chunk)
    params["per-page"] = str(len(chunk))

    try:
        response = await _throttled_get(f"{OPENALEX_BASE_URL}/works", params=params)
        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        # A garbled 200 body — transient. Surface a retryable error for
        # every DOI in the chunk and do NOT negative-cache (a retry must
        # re-fetch); we can't tell which DOI the upstream meant to error.
        # A fresh dict per key. ``dict.fromkeys`` aliased one object across up
        # to 50 keys, so a caller mutating its own error corrupted the other
        # 49 — against ``parse_error_dict``'s documented "fresh dict each
        # call" and ``cached_lookup``'s deep-copy discipline.
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
            # Unattributable: a non-dict entry, no usable ``doi``, or a DOI we
            # did not ask for. Still cache it if we can — it is real data — but
            # the response no longer accounts for itself.
            unattributed += 1
            if work_canonical is not None:
                cache.put(NAMESPACE, "works", work_canonical, work)
            continue
        cache.put(NAMESPACE, "works", work_canonical, work)
        out[work_canonical] = work
        seen_in_chunk.add(work_canonical)

    # A DOI we asked for and didn't get back is a definitive miss — but only if
    # the response accounted for itself. An unattributable record or a truncated
    # page means a "missing" DOI may well exist, and negative-caching it would
    # poison the entry for the negative TTL.
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

    # DOIs containing OpenAlex filter metacharacters ('|' = OR, ',' = AND)
    # can't be safely OR-joined into the batch filter — they'd corrupt the
    # query and silently shift which records resolve. Resolve them one at a
    # time via the singleton path endpoint (properly percent-encoded by
    # get_work) and batch the rest.
    safe_misses: list[str] = []
    unsafe_misses: list[str] = []
    for canonical in misses:
        target = unsafe_misses if "|" in canonical or "," in canonical else safe_misses
        target.append(canonical)

    chunks = [
        safe_misses[start : start + _BATCH_CHUNK_SIZE]
        for start in range(0, len(safe_misses), _BATCH_CHUNK_SIZE)
    ]

    # Chunks and singleton fallbacks run concurrently; the provider's
    # ``Throttle`` is what bounds real parallelism. return_exceptions=False is
    # fine here: every task already converts its failures into error dicts.
    results = await asyncio.gather(
        *(get_work(c, force_refresh=force_refresh) for c in unsafe_misses),
        *(_fetch_chunk(chunk, force_refresh=force_refresh) for chunk in chunks),
    )
    for canonical, result in zip(unsafe_misses, results[: len(unsafe_misses)], strict=True):
        out[canonical] = result
    for chunk_out in results[len(unsafe_misses) :]:
        out.update(chunk_out)

    # Re-keyed in input order rather than in resolution order. Total by
    # construction: every canonical is a cache hit or lands in one fan-out branch.
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
