import asyncio
from typing import Any
from urllib.parse import quote

from .. import _clients, _doi, _http, _singleflight, _useragent, cache, config
from .._throttle import Throttle

OPENALEX_BASE_URL = "https://api.openalex.org"
NAMESPACE = "openalex"

# The OpenAlex API returns JSON; a malformed/truncated 200 body raises
# ``json.JSONDecodeError`` on ``.json()``. It is handled alongside the HTTP
# errors so the tool always returns the uniform ``{error}`` contract rather
# than crashing on a garbled response. Mirrors crossref/biorxiv.
_PARSE_ERRORS = _http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable OpenAlex response.

    Delegates to ``_http.parse_error_dict``, the single home for the shape.
    """
    return _http.parse_error_dict("OpenAlex")


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
_single_flight = _singleflight.SingleFlight()

# Positive cache TTL. OpenAlex works grow citation counts and gain
# authors / topics over time; 30 days is long enough to amortise
# repeated reads in a session and short enough that a paper's metadata
# isn't frozen forever. Authors share the same TTL — h_index and
# works_count drift on the same timescale.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def _normalize_doi(doi: str) -> str:
    """Normalize a DOI to the format OpenAlex expects in the URL path.

    Returns the bare DOI; the caller adds the ``doi:`` path prefix. Thin
    wrapper over :mod:`_doi`, the single home for this logic.
    """
    return _doi.normalize(doi)


def canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return _doi.canonical(doi)


def best_pdf_url(work: dict[str, Any]) -> str | None:
    """Pick the best open-access *PDF* URL from a raw OpenAlex work.

    Prefers a direct PDF link over a landing page, in order:
      1. ``best_oa_location.pdf_url`` — OpenAlex's chosen best OA copy
      2. ``primary_location.pdf_url`` — the version of record, if OA
      3. ``open_access.oa_url`` — last resort; frequently an HTML
         landing/abstract page rather than a PDF

    Returns ``None`` when no usable URL is present. Each sub-object is
    guarded with ``or {}`` because OpenAlex returns these keys as explicit
    ``null`` for closed-access works.
    """
    for loc_key in ("best_oa_location", "primary_location"):
        loc = work.get(loc_key) or {}
        url = loc.get("pdf_url")
        if url:
            return url
    return (work.get("open_access") or {}).get("oa_url") or None


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
    return _useragent.headers(config.get("OPENALEX_MAILTO"))


def _get_client():
    """Return the persistent AsyncClient for OpenAlex calls."""
    return _clients.get_client(NAMESPACE, headers=_build_headers(), timeout=30.0)


_throttle = Throttle(
    namespace=NAMESPACE,
    label="OpenAlex",
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


async def _throttled_get(url: str, **kwargs: Any):
    """Execute a GET respecting OpenAlex's rate limit (see ``Throttle.get``).

    Url-only signature (unlike the other providers' ``client, url`` form): it
    builds the pooled polite-pool client internally via ``_get_client``.
    """
    return await _throttle.get(_get_client(), url, **kwargs)


def _normalize_author_id(author_id: str) -> str:
    """Normalize an author identifier for the API path.

    Accepts:
      - OpenAlex ID: A5023888391
      - Full OpenAlex URL: https://openalex.org/A5023888391
      - ORCID URL: https://orcid.org/0000-0001-6187-6610
    """
    if author_id.startswith("https://openalex.org/"):
        author_id = author_id[len("https://openalex.org/") :]
    return author_id


def canonical_author_id(author_id: str) -> str:
    """Return a canonical author ID for cache keying."""
    return _normalize_author_id(author_id).lower()


async def get_author(author_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch an author by OpenAlex ID or ORCID, using cache when available.

    Concurrent callers for the same author ID share one fetch via
    single-flight.

    ``force_refresh=True`` drops both positive and negative cache entries
    before fetching — author metadata (h_index, works_count) drifts on the
    same 30-day timescale as works, so a caller may want a fresh read.
    """
    canonical = canonical_author_id(author_id)

    async def _fetch() -> dict[str, Any]:
        api_id = _normalize_author_id(author_id)
        params = _build_params()

        try:
            response = await _throttled_get(
                f"{OPENALEX_BASE_URL}/authors/{api_id}",
                params=params,
            )

            if response.status_code == 404:
                err = {"error": f"No author found for ID: {author_id}", "not_found": True}
                cache.put_negative(NAMESPACE, "authors", canonical, err)
                return err

            response.raise_for_status()
            data = response.json()
        except _PARSE_ERRORS:
            return _parse_error_dict()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("OpenAlex", e)

        if not isinstance(data, dict) or "id" not in data:
            # Anomalous 200 (non-dict, or missing the entity id) — treat like
            # a parse failure rather than positive-caching garbage for the TTL.
            return _parse_error_dict()

        cache.put(NAMESPACE, "authors", canonical, data)
        return data

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

    Concurrent callers for the same DOI share one fetch via single-flight.

    ``force_refresh=True`` drops both positive and negative cache entries
    before fetching — useful when the agent needs a fresh citation
    count or to retry an identifier that previously 404'd.
    """
    canonical = canonical_doi(doi)

    async def _fetch() -> dict[str, Any]:
        # Percent-encode the DOI so reserved characters (#, ?, …) aren't
        # misread as a URL fragment/query and silently truncate the request
        # to the wrong record. The prefix/suffix slash stays literal
        # (safe="/"); the "doi:" path prefix is added outside the encode.
        api_doi = f"doi:{quote(_normalize_doi(doi), safe='/')}"
        params = _build_params()

        try:
            response = await _throttled_get(
                f"{OPENALEX_BASE_URL}/works/{api_doi}",
                params=params,
            )

            if response.status_code == 404:
                err = {"error": f"No work found for DOI: {doi}", "not_found": True}
                cache.put_negative(NAMESPACE, "works", canonical, err)
                return err

            response.raise_for_status()
            data = response.json()
        except _PARSE_ERRORS:
            return _parse_error_dict()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("OpenAlex", e)

        if not isinstance(data, dict) or "id" not in data:
            return _parse_error_dict()

        cache.put(NAMESPACE, "works", canonical, data)
        return data

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


def _canonical_from_response_doi(work_doi: str | None) -> str | None:
    """Return the canonical lowercase bare DOI from an OpenAlex work doi.

    OpenAlex returns DOIs as ``https://doi.org/10.1234/foo``; we cache
    by bare lowercase form. Used to map batch responses back to the
    canonical keys we asked for.
    """
    if not work_doi:
        return None
    if work_doi.startswith("https://doi.org/"):
        return work_doi[len("https://doi.org/") :].lower()
    if work_doi.startswith("http://doi.org/"):
        return work_doi[len("http://doi.org/") :].lower()
    return work_doi.lower()


async def _fetch_chunk(
    chunk: list[str],
    *,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch one ``/works?filter=doi:a|b|c`` chunk, coalesced by single-flight.

    Returns ``{canonical: work_or_error}`` for every DOI in ``chunk``.

    Single-flight keyed on the chunk contents, sorted so two callers passing
    the same DOIs in a different order still share one fetch.

    **Deliberately not deep-copied**, unlike ``cache.cached_lookup``: the
    aliasing hazard here was *within* a chunk (one error dict behind 50 keys),
    which per-key construction fixes outright, and every consumer treats the
    work objects as read-only. The costs are not comparable either — that
    copies one record, this would copy fifty full OpenAlex works, the largest
    objects this codebase moves, on every batch call including cache-warm ones.
    A consumer that ever needs to mutate one copies that one.
    """
    sf_key = ("works_batch", tuple(sorted(chunk)), force_refresh)

    async def _runner() -> dict[str, dict[str, Any]]:
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
    except _http.HTTPX_ERRORS as e:
        return {c: _http.error_dict("OpenAlex", e) for c in chunk}

    if not isinstance(data, dict):
        return {c: _parse_error_dict() for c in chunk}

    results = data.get("results") or []
    if not isinstance(results, list):
        return {c: _parse_error_dict() for c in chunk}

    chunk_set = set(chunk)
    seen_in_chunk: set[str] = set()
    unmatched_returned = 0
    for work in results:
        if not isinstance(work, dict):
            continue
        work_canonical = _canonical_from_response_doi(work.get("doi"))
        if work_canonical is None:
            continue
        if work_canonical not in chunk_set:
            # OpenAlex answered with a DOI whose stored string differs from
            # the one we asked for. The record is still valid data, so cache
            # it under its own key — but note that we can no longer tell
            # which requested DOI it satisfies.
            unmatched_returned += 1
            cache.put(NAMESPACE, "works", work_canonical, work)
            continue
        cache.put(NAMESPACE, "works", work_canonical, work)
        out[work_canonical] = work
        seen_in_chunk.add(work_canonical)

    # A DOI we asked for and didn't get back is normally a definitive miss,
    # cached negatively so a re-batch in the same session doesn't re-ask
    # (same shape as get_work's 404 path). But that inference only holds if
    # the response actually accounted for everything: if OpenAlex returned a
    # record whose DOI string didn't match what we asked for, or reported
    # more matches than it sent us (a truncated / paginated response), then a
    # "missing" DOI may well exist and negative-caching it would poison the
    # entry for 24h.
    meta = data.get("meta")
    reported = meta.get("count") if isinstance(meta, dict) else None
    truncated = isinstance(reported, int) and reported > len(results)
    trustworthy = unmatched_returned == 0 and not truncated

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

    Returns ``{canonical_doi: work_or_error_dict}`` for every input DOI.
    Cached entries (positive or negative) are served without a network
    call; misses are grouped into ``/works?filter=doi:...|...`` calls
    of up to ``_BATCH_CHUNK_SIZE`` each. Each successfully-resolved
    work is written to the singleton cache, so a follow-up
    ``get_work(doi)`` is a free hit.

    Compared to N parallel ``get_work()`` calls, this collapses N HTTP
    round trips into ⌈N / _BATCH_CHUNK_SIZE⌉ — the dominant win on
    reference-graph traversals where N is 30–200. force_refresh=True
    drops cached entries before fetching.

    Per-DOI failures (transport error during the batch GET, or the
    upstream omitting a requested DOI from results) appear as
    ``{"error": ...}`` values in the returned dict; transient errors
    contaminate the whole chunk because we cannot tell from one HTTP
    failure which DOI in the batch the upstream meant to error on.
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
    safe_misses = [c for c in misses if "|" not in c and "," not in c]
    unsafe_misses = [c for c in misses if "|" in c or "," in c]

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

    return out


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
