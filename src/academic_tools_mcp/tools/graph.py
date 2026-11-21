"""Reference & citation graph tools."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..app import (
    CITATION_SORT,
    CITATION_SOURCE,
    DOI,
    FORCE_REFRESH,
    PAGE,
    PAGE_SIZE,
    READ_ONLY_NETWORK_ANNOTATIONS,
    REF_SOURCE,
    enrich_error,
    mcp,
    openalex_arxiv_id,
    openalex_pmid,
    page_bounds,
    resolve_doi_identifier,
    resolve_paper_identifier,
)
from ..providers import crossref, openalex, opencitations
from ..util import doinorm
from ..util.jsonshape import as_dict, as_int, dict_list

# Every graph call goes through one DOI resolver; this names the tools in its refusal.
_SUBJECT = "Reference and citation graphs"

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


def _source_error(result: dict[str, Any]) -> dict[str, Any]:
    """Lean copy of a provider error dict for embedding in a multi-source response.

    Forwards the whole structured signal, not just ``error``: trimmed to the
    message, the agent cannot tell a transient failure from a definitive one or act
    on it. ``backpressure`` + ``max_concurrency`` are the least obvious pair — we
    refused locally, and this is how much parallelism is safe.
    """
    return {k: result[k] for k in _FORWARDED_ERROR_KEYS if k in result}


async def _oc_count(
    result: dict[str, Any],
    fallback: Callable[..., Awaitable[dict[str, Any]]],
    doi: str,
    *,
    force_refresh: bool,
) -> dict[str, Any]:
    """OpenCitations' tally for a survey row, from the edge list or from ``fallback``.

    The list is primary: it warms the page tool the survey exists to aim, so the
    second request is spent only where the first bought nothing. Gated on ``retryable
    is True`` — a definitive miss and an unclassified 4xx would only be asking again
    for what upstream refused. A tally reached that way carries ``pageable: False``,
    since ``_page`` slices the list it was handed.
    """
    if "error" not in result:
        return {"count": result.get("count", 0)}
    if result.get("retryable") is not True:
        return _source_error(result)

    counted = await fallback(doi, force_refresh=force_refresh)
    if "error" in counted:
        # The list's error, not the tally's: it is the one the agent asked for, and
        # both carry the same verdict when the fallback was reached at all.
        return _source_error(result)
    return {"count": counted.get("count", 0), "pageable": False}


def _crossref_refs(work: dict[str, Any]) -> list[dict[str, Any]]:
    """A Crossref work's reference rows, or ``[]``.

    ``crossref.get_work``'s shape ladder stops at ``message`` being a dict, so the
    list *and its rows* are checked here — a string would ``len()`` to a character
    count and slice character-wise; a non-dict row would reach
    ``_format_crossref_reference`` as an ``AttributeError``. The one list both the
    count tool and the page tool read, so filtering here keeps them agreeing about a
    work. Mirrors ``opencitations._edges_of``.
    """
    return dict_list(work.get("reference"))


_CROSSREF_REF_FIELDS = (
    ("doi", "DOI"),
    ("author", "author"),
    ("title", "article-title"),
    ("year", "year"),
    ("journal", "journal-title"),
    ("volume", "volume"),
    ("first_page", "first-page"),
    ("unstructured", "unstructured"),
)


def _format_crossref_reference(ref: dict[str, Any]) -> dict[str, Any]:
    """Extract lean fields from a raw Crossref reference object."""
    entry: dict[str, Any] = {dst: val for dst, src in _CROSSREF_REF_FIELDS if (val := ref.get(src))}
    if not entry and ref.get("key"):
        # A bookkeeping-only row is a real deposit with no usable metadata;
        # a bare {} would read as a formatter that lost the entry.
        entry["key"] = ref["key"]
    return entry


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_references_count(
    doi: DOI, force_refresh: FORCE_REFRESH = False
) -> dict[str, Any]:
    """Survey outgoing-reference counts across Crossref and OpenCitations in parallel.

    Returns ``{doi, sources: {crossref, opencitations}}``. A ``pageable: false``
    flag on OpenCitations means the count is real but rows cannot be paged.
    """
    doi, bad = await resolve_doi_identifier(doi, subject=_SUBJECT, force_refresh=force_refresh)
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

    sources["opencitations"] = await _oc_count(
        oc_result, opencitations.get_reference_count, doi, force_refresh=force_refresh
    )

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


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_references(
    doi: DOI,
    source: REF_SOURCE = "auto",
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Page through outgoing references (bibliography) from Crossref or OpenCitations.

    ``source='auto'`` surveys both on page 1 (biased toward Crossref for richer
    metadata) and echoes the winner in ``_source``; pin ``source`` to ``_source``
    when paging past page 1. Returns ``{_source, doi, total, page, page_size,
    has_more, references}`` (plus ``partial_failure`` if one provider errored).
    """
    doi, bad = await resolve_doi_identifier(doi, subject=_SUBJECT, force_refresh=force_refresh)
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


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_citations_count(
    doi: DOI, force_refresh: FORCE_REFRESH = False
) -> dict[str, Any]:
    """Survey incoming-citation counts across OpenCitations and OpenAlex in parallel.

    Returns ``{doi, count, pageable, sources: {opencitations, openalex}}``.
    ``count`` is OpenCitations' (null on error); ``get_paper_citations`` can page
    only when ``pageable`` is true. Cross-check OpenCitations 0 against OpenAlex.
    """
    doi, bad = await resolve_doi_identifier(doi, subject=_SUBJECT, force_refresh=force_refresh)
    if bad is not None:
        return bad

    oc_data, oa_work = await asyncio.gather(
        opencitations.get_citations(doi, force_refresh=force_refresh),
        openalex.get_work(doi, force_refresh=force_refresh),
    )

    sources: dict[str, dict[str, Any]] = {}
    oc_row = await _oc_count(
        oc_data, opencitations.get_citation_count, doi, force_refresh=force_refresh
    )
    sources["opencitations"] = oc_row
    count = oc_row.get("count")
    # Beside `count`, not only on the row: a caveat one level down goes unread.
    pageable = oc_row.get("pageable", True) if count is not None else False

    if "error" in oa_work:
        sources["openalex"] = _source_error(oa_work)
    else:
        sources["openalex"] = {"count": as_int(oa_work.get("cited_by_count"))}

    return {"doi": doi, "count": count, "pageable": pageable, "sources": sources}


def _format_openalex_citing_work(work: dict[str, Any]) -> dict[str, Any]:
    """Extract lean triage fields from one OpenAlex citing work."""
    authorships = dict_list(work.get("authorships"))
    first_author = (
        as_dict(authorships[0].get("author")).get("display_name") if authorships else None
    )
    raw_doi = work.get("doi")
    return {
        "doi": doinorm.normalize(raw_doi) if isinstance(raw_doi, str) else None,
        "openalex_id": work.get("id"),
        "pmid": openalex_pmid(work),
        "arxiv_id": openalex_arxiv_id(work),
        "title": work.get("title"),
        "first_author": first_author,
        "author_count": len(authorships),
        "publication_year": work.get("publication_year"),
        "cited_by_count": work.get("cited_by_count"),
        "is_oa": as_dict(work.get("open_access")).get("is_oa"),
    }


async def _openalex_citations_page(
    identifier: str,
    *,
    page: int,
    page_size: int,
    sort: openalex.CitationSort,
    force_refresh: bool,
) -> dict[str, Any]:
    """Resolve *identifier* to an OpenAlex work ID and page its citing works."""
    routed, trade_error = await resolve_paper_identifier(identifier, force_refresh=force_refresh)
    if trade_error is not None:
        return trade_error

    if openalex.is_work_id(routed):
        work_id = openalex.normalize_work_id(routed)
        echoed_id = openalex.canonical_work_id(routed)
    else:
        canonical_doi, bad = await resolve_doi_identifier(
            routed, subject=_SUBJECT, force_refresh=force_refresh
        )
        if bad is not None:
            return bad
        work = await openalex.get_work(canonical_doi, force_refresh=force_refresh)
        if "error" in work:
            return enrich_error(
                work,
                "Check the DOI format or try source='opencitations' if OpenAlex has not "
                "indexed this work.",
            )
        raw_id = work.get("id")
        if not isinstance(raw_id, str) or not openalex.is_work_id(raw_id):
            return {
                "error": f"OpenAlex work for {canonical_doi} has no usable work ID.",
                "suggestion": "Try source='opencitations' instead.",
            }
        work_id = openalex.normalize_work_id(raw_id)
        echoed_id = canonical_doi

    response = await openalex.get_citing_works(work_id, page=page, per_page=page_size, sort=sort)
    if "error" in response:
        return enrich_error(
            response,
            "Retry if OpenAlex is temporarily unavailable, or try source='opencitations'.",
        )

    items = [_format_openalex_citing_work(w) for w in response.get("items", [])]
    total = response.get("total_results") or 0
    _, end = page_bounds(page, page_size)
    return {
        "_source": "openalex",
        "doi": echoed_id,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total and bool(items),
        "citations": items,
    }


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_citations(
    doi: DOI,
    source: CITATION_SOURCE = "opencitations",
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
    sort: CITATION_SORT = "cited_by_count",
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Page through incoming citations from OpenCitations (default) or OpenAlex.

    ``opencitations`` returns DOI-to-DOI links + self-citation flags (``total: 0``
    means unindexed, not uncited). ``openalex`` returns sorted triage rows,
    warms the works cache, supports DOI-less ``W...`` IDs, and works on landmark
    papers where OpenCitations 504s. Call ``get_paper_citations_count`` first.
    """
    if source == "openalex":
        return await _openalex_citations_page(
            doi, page=page, page_size=page_size, sort=sort, force_refresh=force_refresh
        )

    doi, bad = await resolve_doi_identifier(doi, subject=_SUBJECT, force_refresh=force_refresh)
    if bad is not None:
        return bad

    data = await opencitations.get_citations(doi, force_refresh=force_refresh)
    if "error" in data:
        hint = (
            "OpenCitations failed transiently (common on heavily cited papers). "
            "Retry with source='openalex' for sorted, paginated citing works."
            if data.get("retryable") is True
            else "Check the DOI format. OpenCitations requires a valid DOI."
        )
        return enrich_error(data, hint)

    return _opencitations_page(data, doi, page, page_size, kind="citations")
