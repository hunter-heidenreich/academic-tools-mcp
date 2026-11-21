"""Paper tools: metadata / authors / abstract / bibtex / versions / updates / profiles."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, NamedTuple

from pydantic import Field

from .. import manual
from ..app import (
    AUTHOR_ID,
    AUTHORS_PAGE,
    AUTHORS_PAGE_SIZE,
    DOI,
    FALLBACK_CROSSREF,
    FOLLOW_PUBLISHED,
    FORCE_REFRESH,
    INSTITUTION_ID,
    PAPER_ID,
    PREPRINT_ID,
    READ_ONLY_NETWORK_ANNOTATIONS,
    crossref_date,
    enrich_error,
    mcp,
    page_bounds,
    resolve_doi_identifier,
    resolve_paper_identifier,
)
from ..bibtex import (
    generate_acl_bibtex,
    generate_arxiv_bibtex,
    generate_bibtex,
    generate_biorxiv_bibtex,
    generate_crossref_bibtex,
)
from ..net import http
from ..providers import acl, arxiv, biorxiv, crossref, openalex
from ..util import doinorm
from ..util.jsonshape import dict_list, unwrap_first
from .formatting import (
    authors_envelope,
    format_arxiv_metadata,
    format_author_profile,
    format_biorxiv_metadata,
    format_crossref_authors,
    format_crossref_metadata,
    format_institution_profile,
    format_metadata_by_source,
    format_openalex_authors,
    format_openalex_via_arxiv,
    format_openalex_via_biorxiv,
)


def _canonical_for_source(source: manual.MetadataSource | None, identifier: str) -> str | None:
    """The provider's canonical form of ``identifier``, echoed as ``_canonical_id``."""
    if source == "arxiv":
        return arxiv.canonical_arxiv_id(identifier)
    if source == "biorxiv":
        return biorxiv.canonical_key(identifier)
    if source == "acl_anthology":
        return acl.canonical_key(identifier)
    if source == "openalex":
        return (
            openalex.canonical_work_id(identifier)
            if openalex.is_work_id(identifier)
            else openalex.canonical_doi(identifier)
        )
    return None


def _unknown_identifier_error(identifier: str) -> dict[str, Any]:
    """Return an error dict for identifiers that don't resolve to any provider."""
    return {
        "error": (
            f"Cannot resolve paper provider for identifier: {identifier!r}. "
            "Use an arXiv ID (e.g. 2301.00001), an ACL Anthology ID (e.g. P16-1160), "
            "a DOI (e.g. 10.1038/...), or call search_arxiv / "
            "search_crossref_by_title to find one."
        ),
    }


_ARXIV_METADATA_HINT = "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv."

_BIORXIV_METADATA_HINT = "Check the DOI format (10.1101/...) or use search_crossref_by_title."

_ACL_METADATA_HINT = "Check the Anthology ID on aclanthology.org (e.g. P16-1160, 2023.acl-long.1)."

_OPENALEX_METADATA_HINT = (
    "Check the DOI format or use search_crossref_by_title to find the correct DOI."
)

# Must stay total over ``MetadataSource``: subscripted on the error path, where a
# KeyError would surface late.
_METADATA_HINT_BY_SOURCE: dict[manual.MetadataSource, str] = {
    "arxiv": _ARXIV_METADATA_HINT,
    "biorxiv": _BIORXIV_METADATA_HINT,
    "acl_anthology": _ACL_METADATA_HINT,
    "openalex": _OPENALEX_METADATA_HINT,
}


async def _fetch_source(
    identifier: str, *, force_refresh: bool = False
) -> tuple[manual.MetadataSource | None, str | None, dict[str, Any]]:
    """Resolve an identifier and fetch its raw provider object.

    Returns ``(source, canonical_id, obj)`` — ``(None, None, unknown-identifier error)``
    when no provider claims it. ``obj`` keeps the provider's *un-enriched* error so a
    caller can branch on a provider flag first: get_paper_metadata reads ``not_found``
    for the Crossref fallback.

    All four paper tools reach a provider through here, so the PMID trade at the
    top is uniform across them, as is the bioRxiv → OpenAlex fallback: a hit returns
    ``source == "openalex"``, its work marked ``biorxiv_unavailable``.
    """
    was_non_doi = (
        openalex.is_pmid(identifier)
        or openalex.is_pmcid(identifier)
        or openalex.is_work_id(identifier)
    )
    identifier, pmid_error = await resolve_paper_identifier(identifier, force_refresh=force_refresh)
    if pmid_error is not None:
        return None, None, pmid_error

    source = manual.resolve_metadata_source(identifier)
    canonical_id = _canonical_for_source(source, identifier)
    if source == "arxiv":
        obj = await arxiv.get_paper(identifier, force_refresh=force_refresh)
    elif source == "biorxiv":
        obj = await biorxiv.get_paper(identifier, force_refresh=force_refresh)
        work, oa_retryable = await _biorxiv_fallback(canonical_id, obj, force_refresh=force_refresh)
        if work is not None:
            return "openalex", canonical_id, {**work, "biorxiv_unavailable": True}
        if oa_retryable:
            obj = {**obj, "openalex_fallback_retryable": True}
    elif source == "acl_anthology":
        obj = await acl.get_paper(identifier, force_refresh=force_refresh)
    elif source == "openalex":
        oa_refresh = force_refresh and not (was_non_doi and openalex.is_work_id(identifier))
        obj = await openalex.get_work(identifier, force_refresh=oa_refresh)
    else:
        return None, None, _unknown_identifier_error(identifier)
    return source, canonical_id, obj


def _arxiv_published_doi(paper: dict[str, Any]) -> str | None:
    """The journal DOI an arXiv author recorded; the first, when they listed several."""
    raw = paper.get("doi")
    tokens = raw.split() if isinstance(raw, str) else []
    return tokens[0] if tokens else None


async def _follow_published(
    published_doi: str,
    *,
    journal: Callable[[dict[str, Any], str], dict[str, Any]],
    preprint: Callable[[], dict[str, Any]],
    force_refresh: bool,
) -> dict[str, Any]:
    """OpenAlex's record for a preprint's journal version, else the preprint's own.

    ``journal`` formats the OpenAlex work under its canonical DOI; ``preprint`` builds
    the fall-through record, already carrying ``followed_published=False``.
    """
    work = await openalex.get_work(published_doi, force_refresh=force_refresh)
    if "error" not in work:
        return journal(work, openalex.canonical_doi(published_doi))
    # Not indexed yet: fall back to the preprint — the agent asked for the best
    # version, not a failure.
    result = preprint()
    if work.get("retryable") is True:
        result["published_lookup_retryable"] = True
    return result


async def _crossref_fallback(
    source: manual.MetadataSource,
    canonical_id: str | None,
    obj: dict[str, Any],
    *,
    fallback_crossref: bool,
    force_refresh: bool,
) -> tuple[dict[str, Any] | None, bool]:
    """The Crossref work to answer with, and whether the attempt failed transiently.

    ``(work, False)`` on a hit, ``(None, retryable)`` otherwise. One home for the
    whole precondition — opt-in flag, definitive 404, OpenAlex-routed only — so
    the four paper tools cannot disagree about when Crossref is consulted.

    ``source`` is load-bearing: arXiv and bioRxiv flag their own misses
    ``not_found`` too, and Crossref indexes ``10.1101`` DOIs. Keyed off
    ``canonical_id``, a PMID having already been traded for its DOI.
    """
    if not (
        fallback_crossref
        and source == "openalex"
        and canonical_id
        and doinorm.looks_like_doi(canonical_id)
        and obj.get("not_found")
    ):
        return None, False
    cr = await crossref.get_work(canonical_id, force_refresh=force_refresh)
    if "error" not in cr:
        return cr, False
    # The agent asked for the fallback; a transient failure there is a different
    # verdict from OpenAlex's definitive miss, and silence reads as neither.
    return None, cr.get("retryable") is True


async def _biorxiv_fallback(
    canonical_id: str | None, obj: dict[str, Any], *, force_refresh: bool
) -> tuple[dict[str, Any] | None, bool]:
    """OpenAlex's work for a DOI bioRxiv failed to serve, and whether that lookup failed transiently.

    ``_crossref_fallback``'s contract. Only an upstream-transient bioRxiv failure
    qualifies: a definitive miss stays bioRxiv's, and a local refusal (``backpressure``,
    ``quota_exhausted``) says nothing about bioRxiv.
    """
    if not (
        canonical_id
        and obj.get("retryable") is True
        and not obj.get("backpressure")
        and not obj.get("quota_exhausted")
    ):
        return None, False
    work = await openalex.get_work(canonical_id, force_refresh=force_refresh)
    if "error" not in work:
        return work, False
    return None, work.get("retryable") is True


def _flag_fallback(result: dict[str, Any], obj: dict[str, Any]) -> dict[str, Any]:
    """Copy ``biorxiv_unavailable`` onto a tool response."""
    if obj.get("biorxiv_unavailable") is True:
        result["biorxiv_unavailable"] = True
    return result


def _provider_error(
    obj: dict[str, Any], source: manual.MetadataSource, *, crossref_retryable: bool
) -> dict[str, Any]:
    """A provider error with its per-source hint, plus the fallback's own verdict."""
    result = enrich_error(obj, _METADATA_HINT_BY_SOURCE[source])
    if crossref_retryable:
        result["crossref_fallback_retryable"] = True
    return result


async def _dispatch_paper_tool(
    identifier: str,
    *,
    force_refresh: bool,
    fallback_crossref: bool,
    on_crossref: Callable[[dict[str, Any], str | None], dict[str, Any]],
    on_source: Callable[[manual.MetadataSource, str | None, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Shared fetch, Crossref-fallback, and error-enrichment pipeline for paper tools."""
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj
    cr, cr_retryable = await _crossref_fallback(
        source, canonical_id, obj, fallback_crossref=fallback_crossref, force_refresh=force_refresh
    )
    if cr is not None:
        return on_crossref(cr, canonical_id)
    if "error" in obj:
        return _provider_error(obj, source, crossref_retryable=cr_retryable)
    return _flag_fallback(on_source(source, canonical_id, obj), obj)


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_metadata(
    identifier: PAPER_ID,
    follow_published: FOLLOW_PUBLISHED = False,
    force_refresh: FORCE_REFRESH = False,
    fallback_crossref: FALLBACK_CROSSREF = False,
) -> dict[str, Any]:
    """Get core metadata for a paper, auto-routed by identifier shape.

    Returns ``{_source, _canonical_id, ...provider-native fields}`` across arXiv,
    bioRxiv/medRxiv, ACL Anthology, OpenAlex, and (with ``fallback_crossref`` on
    OpenAlex 404) Crossref. ``follow_published=True`` chains arXiv/bioRxiv
    preprints to OpenAlex's journal version (``_source: openalex_via_*``).
    Transient bioRxiv outages automatically fall back to OpenAlex with
    ``biorxiv_unavailable: true``. Use ``get_papers_metadata`` for batches.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error

    if (
        follow_published
        and source == "biorxiv"
        and "error" not in obj
        and isinstance(published_doi := obj.get("published_doi"), str)
        and published_doi
    ):
        return await _follow_published(
            published_doi,
            journal=lambda work, doi: format_openalex_via_biorxiv(work, obj.get("doi"), doi),
            preprint=lambda: format_biorxiv_metadata(obj, canonical_id, followed_published=False),
            force_refresh=force_refresh,
        )

    if (
        follow_published
        and source == "arxiv"
        and "error" not in obj
        and (published_doi := _arxiv_published_doi(obj))
    ):
        return await _follow_published(
            published_doi,
            journal=lambda work, doi: format_openalex_via_arxiv(
                work, arxiv.id_from_entry(obj), doi
            ),
            preprint=lambda: format_arxiv_metadata(obj, canonical_id, followed_published=False),
            force_refresh=force_refresh,
        )

    cr, cr_retryable = await _crossref_fallback(
        source, canonical_id, obj, fallback_crossref=fallback_crossref, force_refresh=force_refresh
    )
    if cr is not None:
        return format_crossref_metadata(cr, canonical_id)

    if "error" in obj:
        return _provider_error(obj, source, crossref_retryable=cr_retryable)
    return _flag_fallback(format_metadata_by_source(source, obj, canonical_id), obj)


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_versions(
    identifier: PREPRINT_ID,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Get a preprint's license and revision history (arXiv ID or bioRxiv/medRxiv DOI only).

    Returns ``{_source, _canonical_id, submitter, license, versions, version_count}``
    (oldest first). Non-preprint shapes (including PMIDs/OpenAlex work IDs) are
    refused locally without a request — resolve via ``get_paper_metadata`` first.
    """
    if biorxiv.is_biorxiv_doi(identifier):
        return await _biorxiv_versions(identifier, force_refresh=force_refresh)

    if not arxiv.is_arxiv_id(identifier):
        return {
            **http.not_found(f"Not an arXiv ID or bioRxiv/medRxiv DOI: {identifier!r}."),
            "suggestion": "Pass the preprint's arXiv ID or 10.1101 DOI; find it with "
            "get_paper_metadata or search_arxiv. A PMID or OpenAlex work ID is not "
            "traded here — resolve it with get_paper_metadata first.",
        }

    record = await arxiv.get_versions(identifier, force_refresh=force_refresh)
    if "error" in record:
        return enrich_error(record, _ARXIV_METADATA_HINT)

    versions = dict_list(record.get("versions"))
    return {
        "_source": "arxiv",
        "_canonical_id": arxiv.base_arxiv_id(identifier),
        "arxiv_id": record.get("arxiv_id"),
        "submitter": record.get("submitter"),
        "license": record.get("license"),
        "versions": versions,
        "version_count": len(versions),
    }


async def _biorxiv_versions(identifier: str, *, force_refresh: bool) -> dict[str, Any]:
    """get_paper_versions' bioRxiv branch, read off the details record."""
    paper = await biorxiv.get_paper(identifier, force_refresh=force_refresh)
    if "error" in paper:
        return enrich_error(paper, _BIORXIV_METADATA_HINT)

    versions = dict_list(paper.get("versions"))
    return {
        "_source": "biorxiv",
        "_canonical_id": biorxiv.canonical_key(identifier),
        "doi": paper.get("doi"),
        "server": paper.get("server"),
        "submitter": None,
        "license": paper.get("license"),
        "versions": versions,
        "version_count": len(versions),
    }


class _BatchPlan(NamedTuple):
    """One provider's batch leg of get_papers_metadata, and the slots waiting on it.

    ``source`` is a dispatch tag, not a formatter: the leg formats through
    ``format_metadata_by_source``, the helper the singleton leg and
    ``get_paper_metadata`` already share, so a field added there cannot reach some
    paths and miss the batched ones.

    ``fetch`` and ``pending`` are bound per call — the first so a patched provider
    module is seen and ``force_refresh`` is captured, the second because a list at
    module scope would carry slots between calls.
    """

    source: manual.MetadataSource
    fetch: Callable[[list[str]], Awaitable[dict[str, dict[str, Any]]]]
    canonical: Callable[[str], str]
    missing: Callable[[str], str]
    pending: list[tuple[int, str, str]]  # (slot, routed id, caller's input)


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_papers_metadata(
    identifiers: Annotated[
        list[str],
        Field(
            description=(
                "List of paper identifiers (arXiv IDs, ACL Anthology IDs, DOIs, "
                "PMIDs, OpenAlex work IDs). Mixed "
                "sources are fine; each is dispatched to the right "
                "provider. Cap 100 per call to keep responses bounded — "
                "for larger sets, page through in batches."
            ),
            min_length=1,
            max_length=100,
        ),
    ],
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Batch metadata fetch (cap 100) — same payload as ``get_paper_metadata`` plus ``_input``.

    Batches uncached OpenAlex DOIs/PMIDs/work IDs and arXiv IDs, warms singleton
    caches, and preserves input order with per-item errors. Does not support
    ``follow_published``.
    """
    n = len(identifiers)
    results: list[dict[str, Any] | None] = [None] * n

    singleton_tasks: list[asyncio.Task[None]] = []
    plans = (
        _BatchPlan(
            "openalex",
            lambda ids: openalex.get_works_batch(ids, force_refresh=force_refresh),
            openalex.canonical_doi,
            lambda routed: f"No work found for DOI: {routed}",
            [],
        ),
        _BatchPlan(
            "arxiv",
            # One id_list call per chunk: singletons queue behind arXiv's single
            # connection and a fan-out past its burst cap is refused.
            lambda ids: arxiv.get_papers_batch(ids, force_refresh=force_refresh),
            arxiv.canonical_arxiv_id,
            lambda routed: f"No paper found for arXiv ID: {routed}",
            [],
        ),
    )
    by_source = {plan.source: plan for plan in plans}

    singleton_sem = asyncio.Semaphore(2)

    async def _singleton_one(slot: int, routed: str, ident: str) -> None:
        async with singleton_sem:
            sub_refresh = force_refresh and not openalex.is_work_id(routed)
            source, canonical, obj = await _fetch_source(routed, force_refresh=sub_refresh)
        if source is None:
            # Unreachable: the loop routes only bioRxiv/ACL/DOI-less OpenAlex here.
            # Guards the hint lookup against a None key regardless.
            results[slot] = {"_input": ident, **obj}
            return
        if "error" in obj:
            results[slot] = {
                "_input": ident,
                **enrich_error(obj, _METADATA_HINT_BY_SOURCE[source]),
            }
            return
        formatted = _flag_fallback(format_metadata_by_source(source, obj, canonical), obj)
        formatted["_input"] = ident
        results[slot] = formatted

    pmid_idents = [ident for ident in identifiers if openalex.is_pmid(ident)]
    pmcid_idents = [
        ident for ident in identifiers if not openalex.is_pmid(ident) and openalex.is_pmcid(ident)
    ]
    work_id_idents = [
        ident
        for ident in identifiers
        if not openalex.is_pmid(ident)
        and not openalex.is_pmcid(ident)
        and openalex.is_work_id(ident)
    ]
    prebatched_pmids = (
        len(pmid_idents) > 1
        and getattr(openalex.resolve_pmid, "__module__", "") == openalex.__name__
    )
    prebatched_pmcids = (
        len(pmcid_idents) > 1
        and getattr(openalex.resolve_pmcid, "__module__", "") == openalex.__name__
    )
    prebatched_work_ids = (
        len(work_id_idents) > 1
        and getattr(openalex.resolve_work_id, "__module__", "") == openalex.__name__
    )
    prebatch_coros = []
    if prebatched_pmids:
        prebatch_coros.append(
            openalex.resolve_pmids_batch(pmid_idents, force_refresh=force_refresh)
        )
    if prebatched_pmcids:
        prebatch_coros.append(
            openalex.resolve_pmcids_batch(pmcid_idents, force_refresh=force_refresh)
        )
    if prebatched_work_ids:
        prebatch_coros.append(
            openalex.resolve_work_ids_batch(work_id_idents, force_refresh=force_refresh)
        )
    if prebatch_coros:
        await asyncio.gather(*prebatch_coros)

    resolve_sem = asyncio.Semaphore(4)

    async def _resolve_one(ident: str) -> tuple[str, dict[str, Any] | None]:
        is_prebatched = (
            (prebatched_pmids and openalex.is_pmid(ident))
            or (prebatched_pmcids and not openalex.is_pmid(ident) and openalex.is_pmcid(ident))
            or (
                prebatched_work_ids
                and not openalex.is_pmid(ident)
                and not openalex.is_pmcid(ident)
                and openalex.is_work_id(ident)
            )
        )
        async with resolve_sem:
            return await resolve_paper_identifier(
                ident, force_refresh=force_refresh and not is_prebatched
            )

    # First and concurrently: routing is by shape, and a PMID has none until it is
    # traded. `_input` stays the caller's spelling.
    resolved = await asyncio.gather(*(_resolve_one(ident) for ident in identifiers))

    for i, (ident, (routed, pmid_error)) in enumerate(zip(identifiers, resolved, strict=True)):
        if pmid_error is not None:
            results[i] = {"_input": ident, **pmid_error}
            continue
        source = manual.resolve_metadata_source(routed)
        plan = (
            by_source.get(source)
            if source is not None and not openalex.is_work_id(routed)
            else None
        )
        if plan is not None:
            plan.pending.append((i, routed, ident))
        elif source in ("biorxiv", "acl_anthology", "openalex"):
            singleton_tasks.append(asyncio.create_task(_singleton_one(i, routed, ident)))
        else:
            results[i] = {"_input": ident, **_unknown_identifier_error(ident)}

    async def _run_batch(plan: _BatchPlan) -> None:
        if not plan.pending:
            return
        batch = await plan.fetch([routed for _, routed, _ in plan.pending])
        for slot, routed, ident in plan.pending:
            canonical = plan.canonical(routed)
            obj = batch.get(canonical)
            # The batch getters are total, so `is None` means a test stub. dict(): batch
            # entries aren't deep-copied and two spellings of one id share the one entry.
            if obj is None or "error" in obj:
                err = obj or {"error": plan.missing(routed)}
                results[slot] = {
                    "_input": ident,
                    **enrich_error(dict(err), _METADATA_HINT_BY_SOURCE[plan.source]),
                }
                continue
            formatted = _flag_fallback(format_metadata_by_source(plan.source, obj, canonical), obj)
            formatted["_input"] = ident
            results[slot] = formatted

    await asyncio.gather(*singleton_tasks, *(_run_batch(plan) for plan in plans))

    # Defensive: a slot still None is a bug — surface it, don't crash the caller.
    for i, r in enumerate(results):
        if r is None:
            results[i] = {
                "_input": identifiers[i],
                "error": "Internal: no result produced for this identifier.",
            }

    return {"count": n, "papers": results}


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_authors(
    identifier: PAPER_ID,
    page: AUTHORS_PAGE = 1,
    page_size: AUTHORS_PAGE_SIZE = 25,
    force_refresh: FORCE_REFRESH = False,
    fallback_crossref: FALLBACK_CROSSREF = False,
) -> dict[str, Any]:
    """Get a page of the author list, auto-routed by identifier shape (in-memory paging).

    Returns ``{_source, _canonical_id, author_count, page, page_size, has_more,
    authors, page_institutions, page_institution_count}``. OpenAlex rows carry
    ``openalex_id`` (chain into ``get_author``), ``position``, ``is_corresponding``,
    and ``institutions``; bioRxiv carries ``author_corresponding`` fields.
    """
    start, end = page_bounds(page, page_size)

    def _on_source(
        source: manual.MetadataSource, canonical_id: str | None, obj: dict[str, Any]
    ) -> dict[str, Any]:
        if source == "openalex":
            page_slice = format_openalex_authors(obj, start, end)
        else:
            # arXiv/bioRxiv/ACL have no institution records; emit them empty so agents
            # never feature-detect.
            authors = dict_list(obj.get("authors"))
            page_slice = {
                "author_count": len(authors),
                "authors": authors[start:end],
                "page_institutions": [],
                "page_institution_count": 0,
            }
        result = authors_envelope(source, canonical_id, page, page_size, end, page_slice)
        if source == "biorxiv":
            result["author_corresponding"] = obj.get("author_corresponding")
            result["author_corresponding_institution"] = obj.get("author_corresponding_institution")
        return result

    return await _dispatch_paper_tool(
        identifier,
        force_refresh=force_refresh,
        fallback_crossref=fallback_crossref,
        on_crossref=lambda cr, cid: authors_envelope(
            "crossref", cid, page, page_size, end, format_crossref_authors(cr, start, end)
        ),
        on_source=_on_source,
    )


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_abstract(
    identifier: PAPER_ID,
    force_refresh: FORCE_REFRESH = False,
    fallback_crossref: FALLBACK_CROSSREF = False,
) -> dict[str, Any]:
    """Get a paper's plain-text abstract, auto-routed by identifier shape.

    Returns ``{_source, _canonical_id, title, abstract}`` (``abstract`` is null
    when unavailable).
    """

    def _on_source(
        source: manual.MetadataSource, canonical_id: str | None, obj: dict[str, Any]
    ) -> dict[str, Any]:
        if source == "arxiv":
            abstract = obj.get("summary")
        elif source in ("biorxiv", "acl_anthology"):
            abstract = obj.get("abstract")
        else:
            abstract = openalex.reconstruct_abstract(obj.get("abstract_inverted_index")) or None
        return {
            "_source": source,
            "_canonical_id": canonical_id,
            "title": obj.get("title"),
            "abstract": abstract,
        }

    return await _dispatch_paper_tool(
        identifier,
        force_refresh=force_refresh,
        fallback_crossref=fallback_crossref,
        on_crossref=lambda cr, cid: {
            "_source": "crossref",
            "_canonical_id": cid,
            "title": unwrap_first(cr.get("title")),
            "abstract": crossref.abstract_text(cr),
        },
        on_source=_on_source,
    )


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_bibtex(
    identifier: PAPER_ID,
    force_refresh: FORCE_REFRESH = False,
    fallback_crossref: FALLBACK_CROSSREF = False,
) -> dict[str, Any]:
    """Generate a ready-to-paste BibTeX entry, auto-routed by identifier shape.

    Returns ``{_source, _canonical_id, bibtex}``.
    """
    generators: dict[manual.MetadataSource, Callable[[dict[str, Any]], str]] = {
        "arxiv": generate_arxiv_bibtex,
        "biorxiv": generate_biorxiv_bibtex,
        "acl_anthology": generate_acl_bibtex,
        "openalex": generate_bibtex,
    }
    return await _dispatch_paper_tool(
        identifier,
        force_refresh=force_refresh,
        fallback_crossref=fallback_crossref,
        on_crossref=lambda cr, cid: {
            "_source": "crossref",
            "_canonical_id": cid,
            "bibtex": generate_crossref_bibtex(cr, year=crossref_date(cr)[0]),
        },
        on_source=lambda source, cid, obj: {
            "_source": source,
            "_canonical_id": cid,
            "bibtex": generators[source](obj),
        },
    )


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_author(
    author_id: AUTHOR_ID,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Fetch an author's OpenAlex profile (chain from get_paper_authors).

    Returns ``{name, openalex_id, orcid, works_count, cited_by_count, h_index,
    i10_index, current_institutions, top_topics, affiliations}``. ``top_topics``
    is [{name, count}], capped at 5; ``affiliations`` is the full history
    ([{institution, country_code, years}], years sorted). ``works_count`` /
    ``cited_by_count`` / ``h_index`` drift with time.

    Errors: not found / bad ID → ``{error, suggestion}`` pointing at
    get_paper_authors or an ORCID.
    """
    author = await openalex.get_author(author_id, force_refresh=force_refresh)
    if "error" in author:
        return enrich_error(
            author,
            "Use an OpenAlex author ID (from get_paper_authors or search_authors), "
            "or an ORCID in any spelling.",
        )

    return format_author_profile(author)


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_institution(
    institution_id: INSTITUTION_ID,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Fetch an institution's OpenAlex profile (chain from autocomplete_openalex).

    Returns ``{name, openalex_id, ror, country_code, type, homepage_url,
    works_count, cited_by_count, h_index, i10_index, city, region,
    alternative_names, parent_institutions}``. ``alternative_names`` merges
    OpenAlex's acronyms and alternative spellings, capped at 10;
    ``parent_institutions`` is the lineage above this one, so a hospital or campus
    names its university. ``works_count`` / ``cited_by_count`` / ``h_index`` drift
    with time.

    Resolves what an affiliation string names, not where an author worked when a
    given paper was written — affiliations elsewhere are current, not paper-time.

    Errors: not found / bad ID → ``{error, suggestion}`` pointing at
    autocomplete_openalex or a ROR.
    """
    institution = await openalex.get_institution(institution_id, force_refresh=force_refresh)
    if "error" in institution:
        return enrich_error(
            institution,
            "Use an OpenAlex institution ID (from autocomplete_openalex or "
            "get_paper_authors), or a ROR in any spelling.",
        )

    return format_institution_profile(institution)


# Names this tool in the DOI-only refusal `resolve_doi_identifier` builds.
_UPDATES_SUBJECT = "Crossref update notices"


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_updates(doi: DOI, force_refresh: FORCE_REFRESH = False) -> dict[str, Any]:
    """Check a Crossref DOI for retraction/correction notices and preprint relations.

    Returns ``{doi, retracted, updates, relations}``. ``retracted`` covers
    retraction/withdrawal/removal; scan ``updates`` for corrections/expressions
    of concern. ``retracted: false`` means Crossref lists no notice, not a clean
    bill of health.
    """
    doi, bad = await resolve_doi_identifier(
        doi, subject=_UPDATES_SUBJECT, force_refresh=force_refresh
    )
    if bad is not None:
        return bad

    work = await crossref.get_work(doi, force_refresh=force_refresh)
    if "error" in work:
        return enrich_error(
            work,
            "Check the DOI, or use search_crossref_by_title to find the right one. "
            "Only Crossref-registered DOIs carry update notices.",
        )

    updates = []
    for update in crossref.updates(work):
        # `crossref_date` walks a work, so the notice's date-parts ride in under a
        # key it knows rather than growing a second walker.
        year, date = crossref_date({"issued": update.pop("updated")})
        updates.append({**update, "year": year, "date": date})

    return {
        "doi": doi,
        "retracted": any(crossref.retracts(u["type"]) for u in updates),
        "updates": updates,
        "relations": crossref.relations(work),
    }
