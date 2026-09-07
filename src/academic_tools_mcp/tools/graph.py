"""Reference & citation graph tools."""

import asyncio
from typing import Any

from .. import _doi, _http
from .._app import (
    DOI,
    FORCE_REFRESH,
    PAGE,
    PAGE_SIZE,
    REF_SOURCE,
    _enrich_error,
    mcp,
    page_bounds,
)
from ..providers import crossref, opencitations

# Auto source-selection bias. Crossref entries carry structured
# bibliographic metadata (author/title/year/journal/DOI); OpenCitations
# entries are bare DOI-to-DOI links. A near-tie on raw count would flip
# auto to the metadata-poor source for the sake of a row or two, so
# OpenCitations only wins when it has *materially* more references.
_CROSSREF_HYSTERESIS = 1.2


# Structured fields worth forwarding from a provider error into a
# multi-source response. Everything an agent might branch on.
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

    Both graph providers are DOI-only. Without this, an arXiv ID spends an
    upstream round-trip to earn a 404 and then plants a ``not_found`` negative
    cache entry keyed to an identifier that could never have resolved. Uses the
    same predicate as the metadata dispatcher, so the two agree on what a DOI is.
    """
    if _doi.looks_like_doi(doi):
        return None
    return {
        **_http.not_found(f"Not a DOI: {doi!r}. Reference and citation graphs are DOI-only."),
        "suggestion": (
            "Pass a DOI (e.g. 10.1038/nature12373), in bare, doi: or "
            "https://doi.org/ form. For an arXiv paper, call get_paper_metadata "
            "first and use the doi field, or search_crossref_by_title to find one."
        ),
    }


def _source_error(result: dict[str, Any]) -> dict[str, Any]:
    """Lean copy of a provider error dict for embedding in a multi-source response.

    Forwards the whole structured signal, not just the message string, so an
    agent can tell a transient failure from a definitive one *and* act on it.
    Each key in ``_FORWARDED_ERROR_KEYS`` earns its place: ``retryable`` (is it
    worth retrying), ``retry_after_seconds`` (how long to wait),
    ``backpressure`` + ``max_concurrency`` (we refused locally, and how much
    parallelism is safe), ``not_found`` (definitively absent vs. transiently
    unavailable), ``suggestion`` (what to do instead). Trimming the set to just
    the message strands the agent with a failure it cannot classify.
    """
    return {k: result[k] for k in _FORWARDED_ERROR_KEYS if k in result}


def _crossref_refs(work: dict[str, Any]) -> list[dict[str, Any]]:
    """A Crossref work's reference rows, or ``[]``.

    Both the list *and its rows* are type-checked: ``message`` being a dict is
    as far as ``crossref.get_work``'s shape ladder reaches, so a string here
    would ``len()`` to a character count and then slice character-wise, and a
    non-dict row would reach ``_format_crossref_reference`` as an
    ``AttributeError``. Filtering here — the one list both the count tool and
    the page tool read — is what keeps them from disagreeing about a work.
    Mirrors ``opencitations._edges_of``, which drops non-dict records upstream.
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
        # A row carrying only bookkeeping fields is a real deposit with no
        # usable metadata. Emitting Crossref's own `key` says that, where a
        # bare {} reads as a formatter that lost the entry.
        entry["key"] = ref["key"]
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
    tolerant: if one source errors the other's count is still reported. The
    echoed ``doi`` is the canonical form of whatever spelling you passed.

    A non-DOI identifier is rejected locally, without a request —
    both providers are DOI-only.
    """
    if (bad := _reject_non_doi(doi)) is not None:
        return bad
    doi = _doi.canonical(doi)

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
    page carries. ``total`` is derived from the sliced list rather than from a
    provider's own count field, because it is what ``has_more`` and the offsets
    have to agree with.
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

    ``kind`` is the provider's own ``references`` / ``citations`` key — the two
    directions differ by nothing else on this side either, which is what
    ``opencitations._fetch_direction`` already parameterises upstream.
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

    Default ``source="auto"`` fires Crossref and OpenCitations in parallel
    and pages from the better source — saves a turn vs. calling
    get_paper_references_count first. Selection is biased toward Crossref
    (its entries carry structured author/title/year metadata vs.
    OpenCitations' bare DOI links): OpenCitations only wins when it has
    materially more references, not on a one-or-two-entry margin. The
    chosen source is reported in ``_source``. If one provider errors, the
    other wins automatically and the response carries a ``partial_failure``
    field naming the failed source so an empty result isn't mistaken for a
    confident "no references"; if both error, the response carries both errors.

    ``source="auto"`` only resolves on the first page. Paginating past page 1
    must pin ``_source`` to the value returned on page 1 (auto on page>1
    returns an error) — re-surveying could pick a different source mid-walk
    and silently shift the offsets.

    ``force_refresh=True`` re-fetches the underlying source(s), bypassing the
    cache — pass it on the first page when you need fresh coverage; omit it
    when paginating so page 2..N reuse the warmed cache.

    Returns ``{_source, doi, total, page, page_size, has_more, references: [...]}``.
    The per-entry shape differs by source:
      - crossref: structured metadata, fields conditionally present based
        on publisher deposit quality. Possible keys: doi, author, title,
        year, journal, volume, first_page, unstructured (raw citation
        text fallback when structured fields are absent), key (last-resort
        fallback: the publisher deposited the row with no usable metadata).
      - opencitations: DOI-to-DOI links with cross-referenced IDs flattened
        at the top level. Possible keys: doi (cited paper), omid, openalex,
        pmid, creation (date string), journal_self_citation,
        author_self_citation. No bibliographic metadata.

    Defaults: page=1, page_size=20 (1-50). Call get_paper_references_count
    explicitly only if you want to compare coverage before committing.

    Errors: bad DOI / upstream failure → ``{error, suggestion}`` with retry
    hints for transient failures. A non-DOI identifier is rejected locally,
    without a request — both providers are DOI-only.

    The echoed ``doi`` is the canonical form of whatever spelling you passed,
    so every spelling of one paper correlates to one value across calls.
    """
    if (bad := _reject_non_doi(doi)) is not None:
        return bad
    doi = _doi.canonical(doi)

    if source == "crossref":
        work = await crossref.get_work(doi, force_refresh=force_refresh)
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
        return _opencitations_page(data, doi, page, page_size, kind="references")

    # source == "auto": resolve the source on page 1 only. Pages 2..N must
    # pin the source returned on page 1 — re-surveying here could pick a
    # different provider if the cached counts have drifted (a force_refresh
    # on a later page, or a TTL lapse mid-walk), silently shifting `total`
    # and the slice offsets out from under the agent.
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

    # Survey both. The fetches are cached so a follow-up page-1 call with
    # the same DOI doesn't re-fetch.
    cr_task = crossref.get_work(doi, force_refresh=force_refresh)
    oc_task = opencitations.get_references(doi, force_refresh=force_refresh)
    cr_work, oc_data = await asyncio.gather(cr_task, oc_task)

    cr_ok = "error" not in cr_work
    oc_ok = "error" not in oc_data
    cr_count = len(_crossref_refs(cr_work)) if cr_ok else -1
    oc_count = oc_data.get("count", 0) if oc_ok else -1

    if not cr_ok and not oc_ok:
        # Both upstreams failed. Surface both errors so the agent can
        # decide whether to retry or pick one explicitly.
        return {
            "error": "Both reference sources failed for this DOI.",
            # Retrying the pair is worth it if *either* source might answer on
            # a second call, so the top-level verdict is the disjunction of the
            # two. Without it an agent branching on the top level — the shape
            # every other tool-layer error carries — learns nothing.
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

    # Pick the source. With both available, bias toward Crossref's richer
    # per-entry metadata: OpenCitations wins only when it has materially
    # more references. When one source errored its count is -1, so the
    # surviving source wins automatically.
    if oc_count > cr_count * _CROSSREF_HYSTERESIS:
        page_result = _opencitations_page(oc_data, doi, page, page_size, kind="references")
    else:
        page_result = _crossref_refs_page(cr_work, doi, page, page_size)

    # If exactly one source failed, the chosen page came from the survivor.
    # Surface the failure so an empty/short result isn't read as a confident
    # "no references" when the other source merely had a transient error.
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

    Returns ``{doi, count}`` on success or ``{error, suggestion}`` on failure;
    the echoed ``doi`` is the canonical form of whatever spelling you passed.
    OpenCitations is the only source for incoming citations (no Crossref
    equivalent), so unlike get_paper_references_count there is no source
    survey — call this then page with get_paper_citations.

    ``force_refresh=True`` re-fetches from OpenCitations, bypassing the cache —
    incoming citations grow continuously, so use it for a fresher count.

    A non-DOI identifier is rejected locally, without a request —
    both providers are DOI-only.
    """
    if (bad := _reject_non_doi(doi)) is not None:
        return bad
    doi = _doi.canonical(doi)

    data = await opencitations.get_citations(doi, force_refresh=force_refresh)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
    return {"doi": doi, "count": data.get("count", 0)}


@mcp.tool
async def get_paper_citations(
    doi: DOI,
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

    Defaults: page=1, page_size=20 (1-50). Call get_paper_citations_count
    first to see the total. OpenCitations is the only source for incoming
    citations today, so there is no source parameter (unlike
    get_paper_references); one would be reintroduced if a second citation
    source ever ships.

    ``force_refresh=True`` re-fetches from OpenCitations, bypassing the cache —
    pass it on the first page for fresh coverage; omit it when paginating so
    page 2..N reuse the warmed cache.

    Errors: bad DOI / upstream failure → ``{error, suggestion}`` with retry
    hints for transient failures. A non-DOI identifier is rejected locally,
    without a request — both providers are DOI-only.

    The echoed ``doi`` is the canonical form of whatever spelling you passed.
    """
    if (bad := _reject_non_doi(doi)) is not None:
        return bad
    doi = _doi.canonical(doi)

    data = await opencitations.get_citations(doi, force_refresh=force_refresh)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")

    return _opencitations_page(data, doi, page, page_size, kind="citations")
