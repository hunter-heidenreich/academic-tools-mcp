"""Reference & citation graph tools."""

import asyncio
from typing import Any

from .. import _app
from .._app import (
    CITATION_SOURCE,
    DOI,
    FORCE_REFRESH,
    PAGE,
    PAGE_SIZE,
    REF_SOURCE,
    _enrich_error,
    mcp,
)
from ..providers import opencitations


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
    return entry


@mcp.tool
async def get_paper_references_count(
    doi: DOI, force_refresh: FORCE_REFRESH = False
) -> dict[str, Any]:
    """Survey outgoing-reference coverage across Crossref and OpenCitations.

    Fires both providers in parallel via asyncio.gather. Counts often
    differ — call this first to pick the better-covered source before
    paginating with get_paper_references.

    ``force_refresh=True`` re-fetches both sources, bypassing the cache —
    useful when a reference list may have grown since it was last cached.

    Returns ``{doi, sources: {crossref: {count: N} | {error, suggestion?},
    opencitations: {count: M} | {error, suggestion?}}}``. Partial-failure
    tolerant: if one source errors the other's count is still reported.
    """
    cr_task = _app._fetch_crossref_work(doi, force_refresh=force_refresh)
    oc_task = opencitations.get_references(doi, force_refresh=force_refresh)
    cr_result, oc_result = await asyncio.gather(cr_task, oc_task)

    sources: dict[str, dict[str, Any]] = {}
    if "error" in cr_result:
        sources["crossref"] = {"error": cr_result["error"]}
    else:
        sources["crossref"] = {"count": len(cr_result.get("reference") or [])}

    if "error" in oc_result:
        sources["opencitations"] = {"error": oc_result["error"]}
    else:
        sources["opencitations"] = {"count": oc_result.get("count", 0)}

    return {"doi": doi, "sources": sources}


def _crossref_refs_page(
    work: dict[str, Any], doi: str, page: int, page_size: int
) -> dict[str, Any]:
    raw_refs = work.get("reference") or []
    total = len(raw_refs)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "_source": "crossref",
        "doi": doi,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        "references": [_format_crossref_reference(r) for r in raw_refs[start:end]],
    }


def _opencitations_refs_page(
    data: dict[str, Any], doi: str, page: int, page_size: int
) -> dict[str, Any]:
    refs = data.get("references", [])
    total = len(refs)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "_source": "opencitations",
        "doi": doi,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        "references": refs[start:end],
    }


@mcp.tool
async def get_paper_references(
    doi: DOI,
    source: REF_SOURCE = "auto",
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Page through outgoing references (bibliography) from the chosen source.

    Default ``source="auto"`` fires Crossref and OpenCitations in parallel,
    picks whichever has more references, and pages from that one — saves
    a turn vs. calling get_paper_references_count first. The chosen
    source is reported in ``_source`` so subsequent pagination calls can
    pin it explicitly. If one provider errors, the other wins
    automatically; if both error, the response carries both errors.

    ``force_refresh=True`` re-fetches the underlying source(s), bypassing the
    cache — pass it on the first page when you need fresh coverage; omit it
    when paginating so page 2..N reuse the warmed cache.

    Returns ``{_source, doi, total, page, page_size, has_more, references: [...]}``.
    The per-entry shape differs by source:
      - crossref: structured metadata, fields conditionally present based
        on publisher deposit quality. Possible keys: doi, author, title,
        year, journal, volume, first_page, unstructured (raw citation
        text fallback when structured fields are absent).
      - opencitations: DOI-to-DOI links with cross-referenced IDs flattened
        at the top level. Possible keys: doi (cited paper), omid, openalex,
        pmid, creation (date string), journal_self_citation,
        author_self_citation. No bibliographic metadata.

    Defaults: page=1, page_size=20 (1-50). Call get_paper_references_count
    explicitly only if you want to compare coverage before committing.

    Errors: bad DOI / upstream failure → ``{error, suggestion}`` with retry
    hints for transient failures.
    """
    if source == "crossref":
        work = await _app._fetch_crossref_work(doi, force_refresh=force_refresh)
        if "error" in work:
            return _enrich_error(
                work,
                "Check the DOI format or use search_crossref_by_title to find the correct DOI.",
            )
        return _crossref_refs_page(work, doi, page, page_size)

    if source == "opencitations":
        data = await opencitations.get_references(doi, force_refresh=force_refresh)
        if "error" in data:
            return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
        return _opencitations_refs_page(data, doi, page, page_size)

    # source == "auto": survey both, pick the bigger. The fetches are
    # cached so a follow-up page=2 call with the same source doesn't
    # re-survey or re-fetch.
    cr_task = _app._fetch_crossref_work(doi, force_refresh=force_refresh)
    oc_task = opencitations.get_references(doi, force_refresh=force_refresh)
    cr_work, oc_data = await asyncio.gather(cr_task, oc_task)

    cr_count = len(cr_work.get("reference") or []) if "error" not in cr_work else -1
    oc_count = oc_data.get("count", 0) if "error" not in oc_data else -1

    if cr_count < 0 and oc_count < 0:
        # Both upstreams failed. Surface both errors so the agent can
        # decide whether to retry or pick one explicitly.
        return {
            "error": "Both reference sources failed for this DOI.",
            "sources": {
                "crossref": {"error": cr_work.get("error")},
                "opencitations": {"error": oc_data.get("error")},
            },
            "suggestion": (
                "Both Crossref and OpenCitations are unreachable or have "
                "no record for this DOI. Check the DOI format with "
                "search_crossref_by_title, or retry — these were transient "
                "failures if either error message says 'Transient'."
            ),
        }

    # Pick the bigger count; tie goes to Crossref (richer metadata).
    if cr_count >= oc_count:
        return _crossref_refs_page(cr_work, doi, page, page_size)
    return _opencitations_refs_page(oc_data, doi, page, page_size)


@mcp.tool
async def get_paper_citations_count(
    doi: DOI, force_refresh: FORCE_REFRESH = False
) -> dict[str, Any]:
    """Count incoming citations (papers that cite this work) via OpenCitations.

    Returns ``{doi, count}`` on success or ``{error, suggestion}`` on failure.
    OpenCitations is the only source for incoming citations (no Crossref
    equivalent), so unlike get_paper_references_count there is no source
    survey — call this then page with get_paper_citations.

    ``force_refresh=True`` re-fetches from OpenCitations, bypassing the cache —
    incoming citations grow continuously, so use it for a fresher count.
    """
    data = await opencitations.get_citations(doi, force_refresh=force_refresh)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
    return {"doi": doi, "count": data["count"]}


@mcp.tool
async def get_paper_citations(
    doi: DOI,
    source: CITATION_SOURCE = "auto",
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Page through incoming citations (papers that cite this work) from OpenCitations.

    Returns ``{_source, doi, total, page, page_size, has_more, citations: [...]}``.
    Each citation entry has cross-referenced IDs flattened at the top
    level: doi (citing paper), omid, openalex, pmid, creation (date
    string), journal_self_citation, author_self_citation. No bibliographic
    metadata — chain a citing DOI into get_paper_metadata for that.

    Defaults: source="auto" (currently always OpenCitations), page=1,
    page_size=20 (1-50). Call get_paper_citations_count first to see
    the total.

    ``force_refresh=True`` re-fetches from OpenCitations, bypassing the cache —
    pass it on the first page for fresh coverage; omit it when paginating so
    page 2..N reuse the warmed cache.

    Errors: bad DOI / upstream failure → ``{error, suggestion}`` with
    retry hints for transient failures.
    """
    # source is reserved for forward compatibility; both values dispatch
    # to OpenCitations today. Keeping the parameter in the signature now
    # means agent code path "page through citations" can pin source=
    # "opencitations" without breaking when a second source ships.
    data = await opencitations.get_citations(doi, force_refresh=force_refresh)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")

    cites = data.get("citations", [])
    total = len(cites)

    start = (page - 1) * page_size
    end = start + page_size

    return {
        "_source": "opencitations",
        "doi": doi,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        "citations": cites[start:end],
    }
