"""Reference & citation graph tools."""

import asyncio
from typing import Any

from ..app import (
    DOI,
    FORCE_REFRESH,
    PAGE,
    PAGE_SIZE,
    REF_SOURCE,
    enrich_error,
    mcp,
    page_bounds,
    resolve_paper_identifier,
)
from ..net import http
from ..providers import crossref, opencitations
from ..util import doinorm

# Auto-selection bias: OpenCitations rows are bare DOI links where Crossref's
# carry metadata, so a row-or-two lead must not flip `auto` to the poorer source.
_CROSSREF_HYSTERESIS = 1.2


# Everything an agent might branch on, forwarded from a provider error into a
# multi-source response.
_FORWARDED_ERROR_KEYS = (
    "error",
    "retryable",
    "suggestion",
    "retry_after_seconds",
    "backpressure",
    "max_concurrency",
    "not_found",
)


def _reject_non_doi(doi: str) -> dict[str, Any] | None:
    """Error dict when ``doi`` is not DOI-shaped, else ``None``.

    Both graph providers are DOI-only, so without this an arXiv ID buys a 404
    round-trip and negative-caches it under an identifier that could never have
    resolved. ``doinorm.looks_like_doi`` is the metadata dispatcher's predicate too.
    """
    if doinorm.looks_like_doi(doi):
        return None
    return {
        **http.not_found(f"Not a DOI: {doi!r}. Reference and citation graphs are DOI-only."),
        "suggestion": (
            "Pass a DOI (e.g. 10.1038/nature12373), in bare, doi: or "
            "https://doi.org/ form, or a PMID. For an arXiv paper, call "
            "get_paper_metadata first and use the doi field, or "
            "search_crossref_by_title to find one."
        ),
    }


async def _resolve_doi(doi: str, *, force_refresh: bool) -> tuple[str, dict[str, Any] | None]:
    """Canonical DOI for a graph call, or the error that ends it.

    The one entry every graph tool takes, so the PMID trade and the DOI-only
    rejection keep one order across all four. Resolving first is what makes the
    ``pmid`` these tools hand out on every OpenCitations row one they also take.
    """
    doi, pmid_error = await resolve_paper_identifier(doi, force_refresh=force_refresh)
    if pmid_error is not None:
        return doi, pmid_error
    if (bad := _reject_non_doi(doi)) is not None:
        return doi, bad
    return doinorm.canonical(doi), None


def _source_error(result: dict[str, Any]) -> dict[str, Any]:
    """Lean copy of a provider error dict for embedding in a multi-source response.

    Forwards the whole structured signal, not just ``error``: trimmed to the
    message, the agent cannot tell a transient failure from a definitive one or act
    on it. ``backpressure`` + ``max_concurrency`` are the least obvious pair — we
    refused locally, and this is how much parallelism is safe.
    """
    return {k: result[k] for k in _FORWARDED_ERROR_KEYS if k in result}


def _crossref_refs(work: dict[str, Any]) -> list[dict[str, Any]]:
    """A Crossref work's reference rows, or ``[]``.

    ``crossref.get_work``'s shape ladder stops at ``message`` being a dict, so the
    list *and its rows* are checked here — a string would ``len()`` to a character
    count and slice character-wise; a non-dict row would reach
    ``_format_crossref_reference`` as an ``AttributeError``. The one list both the
    count tool and the page tool read, so filtering here keeps them agreeing about a
    work. Mirrors ``opencitations._edges_of``.
    """
    refs = work.get("reference")
    if not isinstance(refs, list):
        return []
    return [ref for ref in refs if isinstance(ref, dict)]


def _format_crossref_reference(ref: dict[str, Any]) -> dict[str, Any]:
    """Extract lean fields from a raw Crossref reference object."""
    entry: dict[str, Any] = {}
    if ref.get("DOI"):
        entry["doi"] = ref["DOI"]
    if ref.get("author"):
        entry["author"] = ref["author"]
    if ref.get("article-title"):
        entry["title"] = ref["article-title"]
    if ref.get("year"):
        entry["year"] = ref["year"]
    if ref.get("journal-title"):
        entry["journal"] = ref["journal-title"]
    if ref.get("volume"):
        entry["volume"] = ref["volume"]
    if ref.get("first-page"):
        entry["first_page"] = ref["first-page"]
    if ref.get("unstructured"):
        entry["unstructured"] = ref["unstructured"]
    if not entry and ref.get("key"):
        # A bookkeeping-only row is a real deposit with no usable metadata;
        # a bare {} would read as a formatter that lost the entry.
        entry["key"] = ref["key"]
    return entry


@mcp.tool
async def get_paper_references_count(
    doi: DOI, force_refresh: FORCE_REFRESH = False
) -> dict[str, Any]:
    """Survey outgoing-reference coverage across Crossref and OpenCitations.

    Both providers are fetched in parallel. Counts often differ — call this first
    to pick the better-covered source before paginating with get_paper_references.

    Returns ``{doi, sources: {crossref: {count} | error, opencitations: {count} | error}}``.
    An error object carries ``error`` plus whichever of ``retryable``,
    ``retry_after_seconds``, ``not_found``, ``backpressure``, ``max_concurrency``,
    ``suggestion`` the provider set; one source erroring still reports the other's
    count. The echoed ``doi`` is canonical, not the spelling you passed.

    A PMID resolves to its DOI first; any other non-DOI identifier is rejected
    locally, without a request, as ``{error, not_found: true, suggestion}`` — the
    graph tools are DOI-only.
    """
    doi, bad = await _resolve_doi(doi, force_refresh=force_refresh)
    if bad is not None:
        return bad

    cr_task = crossref.get_work(doi, force_refresh=force_refresh)
    oc_task = opencitations.get_references(doi, force_refresh=force_refresh)
    cr_result, oc_result = await asyncio.gather(cr_task, oc_task)

    sources: dict[str, dict[str, Any]] = {}
    if "error" in cr_result:
        sources["crossref"] = _source_error(cr_result)
    else:
        sources["crossref"] = {"count": len(_crossref_refs(cr_result))}

    if "error" in oc_result:
        sources["opencitations"] = _source_error(oc_result)
    else:
        sources["opencitations"] = {"count": oc_result.get("count", 0)}

    return {"doi": doi, "sources": sources}


def _page(
    entries: list[Any], *, source: str, key: str, doi: str, page: int, page_size: int
) -> dict[str, Any]:
    """The paginated-list response shape every graph tool returns.

    One home for the slice arithmetic and the key set, so the references and
    citations tools cannot drift apart on ``has_more`` or on which fields a
    page carries. ``total`` is ``len(entries)`` — the list
    actually being sliced, never a provider's own count field, since it is what
    ``has_more`` and the offsets have to agree with.
    """
    total = len(entries)
    start, end = page_bounds(page, page_size)
    return {
        "_source": source,
        "doi": doi,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        key: entries[start:end],
    }


def _crossref_refs_page(
    work: dict[str, Any], doi: str, page: int, page_size: int
) -> dict[str, Any]:
    result = _page(
        _crossref_refs(work),
        source="crossref",
        key="references",
        doi=doi,
        page=page,
        page_size=page_size,
    )
    result["references"] = [_format_crossref_reference(r) for r in result["references"]]
    return result


def _opencitations_page(
    data: dict[str, Any], doi: str, page: int, page_size: int, *, kind: str
) -> dict[str, Any]:
    """One page of an OpenCitations payload, in either direction.

    ``kind`` is the provider's own ``references`` / ``citations`` key and the only
    difference between the two directions here, as in
    ``opencitations._fetch_direction``.
    """
    entries = data.get(kind)
    return _page(
        entries if isinstance(entries, list) else [],
        source="opencitations",
        key=kind,
        doi=doi,
        page=page,
        page_size=page_size,
    )


@mcp.tool
async def get_paper_references(
    doi: DOI,
    source: REF_SOURCE = "auto",
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Page through outgoing references (bibliography) from the chosen source.

    ``auto`` surveys both providers on page 1 and pages from the winner — Crossref
    unless OpenCitations has materially more rows — saving a turn over
    get_paper_references_count; the winner is echoed in ``_source``. An errored
    provider loses automatically and the response gains ``partial_failure`` naming
    it, so an empty result isn't mistaken for a confident "no references".

    Paginating past page 1 must pin ``source`` to the ``_source`` from page 1
    (``auto`` there is an error) — re-surveying could pick a different source
    mid-walk and silently shift the offsets. Omit ``force_refresh`` on pages
    2..N so they reuse the cache page 1 warmed.

    Returns ``{_source, doi, total, page, page_size, has_more, references: [...]}``.
    The echoed ``doi`` is canonical, not the spelling you passed. The per-entry
    shape differs by source:
      - crossref: keys present vary with publisher deposit quality — doi, author,
        title, year, journal, volume, first_page, unstructured (raw citation text
        when structured fields are absent), key (last resort: the row was
        deposited with no usable metadata).
      - opencitations: whichever cross-referenced IDs it listed — doi (cited
        paper), omid, openalex, pmid — plus creation (date string, may be null),
        journal_self_citation, author_self_citation. No bibliographic metadata,
        and ``total: 0`` means "no edges in this index", not "no references":
        OpenCitations answers an unindexed DOI and one with zero edges alike.

    Errors: ``{error, suggestion}`` plus a verdict — ``retryable`` on a transient
    failure, ``not_found: true`` on a definitive miss (including the local non-DOI
    rejection, which costs no request: both providers are DOI-only), and neither
    on an unclassified 4xx, so branch on ``retryable is true``, never on its
    absence. ``source="auto"`` past page 1 is ``retryable: false``. When *both*
    providers fail the response adds ``sources: {crossref, opencitations}``, each
    carrying ``error`` plus any of ``retryable``, ``retry_after_seconds``,
    ``not_found``, ``backpressure``, ``max_concurrency``, ``suggestion``, and the
    top-level ``retryable`` is the disjunction of the two.
    """
    doi, bad = await _resolve_doi(doi, force_refresh=force_refresh)
    if bad is not None:
        return bad

    if source == "crossref":
        work = await crossref.get_work(doi, force_refresh=force_refresh)
        if "error" in work:
            return enrich_error(
                work,
                "Check the DOI format or use search_crossref_by_title to find the correct DOI.",
            )
        return _crossref_refs_page(work, doi, page, page_size)

    if source == "opencitations":
        data = await opencitations.get_references(doi, force_refresh=force_refresh)
        if "error" in data:
            return enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
        return _opencitations_page(data, doi, page, page_size, kind="references")

    # source == "auto", page 1 only: drifted counts (a later force_refresh, a TTL
    # lapse mid-walk) could pick a different provider and shift the offsets.
    if page > 1:
        return {
            "error": "source='auto' only resolves on page 1; pin _source to paginate.",
            "retryable": False,
            "suggestion": (
                "Re-call get_paper_references with source set to the _source "
                "value returned on page 1 ('crossref' or 'opencitations') so the "
                "pagination offsets stay consistent across pages."
            ),
        }

    cr_task = crossref.get_work(doi, force_refresh=force_refresh)
    oc_task = opencitations.get_references(doi, force_refresh=force_refresh)
    cr_work, oc_data = await asyncio.gather(cr_task, oc_task)

    cr_ok = "error" not in cr_work
    oc_ok = "error" not in oc_data
    cr_count = len(_crossref_refs(cr_work)) if cr_ok else -1
    oc_count = oc_data.get("count", 0) if oc_ok else -1

    if not cr_ok and not oc_ok:
        return {
            "error": "Both reference sources failed for this DOI.",
            # Disjunction: retrying the pair helps if *either* might answer, and an
            # agent branching on the top level (every tool-layer error's shape) needs one.
            "retryable": bool(cr_work.get("retryable") or oc_data.get("retryable")),
            "sources": {
                "crossref": _source_error(cr_work),
                "opencitations": _source_error(oc_data),
            },
            "suggestion": (
                "Both Crossref and OpenCitations are unreachable or have "
                "no record for this DOI. Check the DOI format with "
                "search_crossref_by_title, or retry — retryable errors are "
                "flagged 'retryable': true in each source's error."
            ),
        }

    # An errored source counted -1 above, so the survivor wins this comparison.
    if oc_count > cr_count * _CROSSREF_HYSTERESIS:
        page_result = _opencitations_page(oc_data, doi, page, page_size, kind="references")
    else:
        page_result = _crossref_refs_page(cr_work, doi, page, page_size)

    # Surface the survivor's twin failing, so a short page isn't read as a
    # confident "no references".
    if cr_ok != oc_ok:
        failed = "opencitations" if cr_ok else "crossref"
        failed_result = oc_data if cr_ok else cr_work
        page_result["partial_failure"] = {"source": failed, **_source_error(failed_result)}

    return page_result


@mcp.tool
async def get_paper_citations_count(
    doi: DOI, force_refresh: FORCE_REFRESH = False
) -> dict[str, Any]:
    """Count incoming citations (papers that cite this work) via OpenCitations.

    Returns ``{doi, count}``, the echoed ``doi`` canonical rather than the spelling
    you passed. ``count: 0`` means "no edges in this index", not "nothing cites this
    work" — OpenCitations answers an unindexed DOI and one with zero edges alike.
    It is the only source of incoming citations (no Crossref equivalent), so there
    is no source survey — call this, then page with get_paper_citations.

    Errors: ``{error, suggestion}`` plus the provider's verdict — ``retryable``
    (with ``retry_after_seconds`` when advertised) on a transient failure,
    ``not_found: true`` on a definitive miss, including the local non-DOI
    rejection, which costs no request: the graph tools are DOI-only.
    """
    doi, bad = await _resolve_doi(doi, force_refresh=force_refresh)
    if bad is not None:
        return bad

    data = await opencitations.get_citations(doi, force_refresh=force_refresh)
    if "error" in data:
        return enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
    return {"doi": doi, "count": data.get("count", 0)}


@mcp.tool
async def get_paper_citations(
    doi: DOI,
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Page through incoming citations (papers that cite this work) from OpenCitations.

    Returns ``{_source, doi, total, page, page_size, has_more, citations: [...]}``,
    the echoed ``doi`` canonical rather than the spelling you passed. Each entry
    carries whichever cross-referenced IDs OpenCitations listed — doi (citing
    paper), omid, openalex, pmid — plus creation (date string, may be null),
    journal_self_citation, author_self_citation. No bibliographic metadata: chain
    a citing DOI into get_paper_metadata for that. ``total: 0`` means "no edges in
    this index", not "nothing cites this work".

    Call get_paper_citations_count first for the total. OpenCitations is the only
    index of incoming citations, so there is no ``source`` parameter and no
    ``sources`` envelope (unlike get_paper_references); one would return if a
    second source ships. Omit ``force_refresh`` when paginating so pages 2..N
    reuse the cache page 1 warmed.

    Errors: ``{error, suggestion}`` plus the provider's verdict — ``retryable``
    (with ``retry_after_seconds`` when advertised) on a transient failure,
    ``not_found: true`` on a definitive miss, including the local non-DOI
    rejection, which costs no request.
    """
    doi, bad = await _resolve_doi(doi, force_refresh=force_refresh)
    if bad is not None:
        return bad

    data = await opencitations.get_citations(doi, force_refresh=force_refresh)
    if "error" in data:
        return enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")

    return _opencitations_page(data, doi, page, page_size, kind="citations")
