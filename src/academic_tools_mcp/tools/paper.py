"""Paper metadata tools: metadata / authors / abstract / bibtex / author lookup."""

import asyncio
from collections.abc import Callable
from typing import Annotated, Any

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
    PAPER_ID,
    as_dict,
    crossref_date,
    dict_list,
    enrich_error,
    mcp,
    page_bounds,
    resolve_doi_identifier,
    resolve_paper_identifier,
    unwrap_first,
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


def _canonical_for_source(source: manual.MetadataSource | None, identifier: str) -> str | None:
    """The provider's canonical form of ``identifier``, echoed as ``_canonical_id``."""
    if source == "arxiv":
        return arxiv.canonical_arxiv_id(identifier)
    if source == "biorxiv":
        return biorxiv.canonical_key(identifier)
    if source == "acl_anthology":
        return acl.canonical_key(identifier)
    if source == "openalex":
        return openalex.canonical_doi(identifier)
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


def _arxiv_pdf_url(paper: dict[str, Any]) -> str | None:
    """Extract the PDF link from an arXiv entry's links list."""
    for link in paper.get("links", []):
        if link.get("title") == "pdf":
            return link.get("href")
    return None


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
        obj = await openalex.get_work(identifier, force_refresh=force_refresh)
    else:
        return None, None, _unknown_identifier_error(identifier)
    return source, canonical_id, obj


def _format_arxiv_metadata(
    paper: dict[str, Any],
    canonical_id: str | None,
    *,
    followed_published: bool | None = None,
) -> dict[str, Any]:
    result = {
        "_source": "arxiv",
        "_canonical_id": canonical_id,
        "arxiv_id": arxiv.id_from_entry(paper),
        "title": paper.get("title"),
        "published": paper.get("published"),
        "updated": paper.get("updated"),
        "primary_category": paper.get("primary_category"),
        "categories": paper.get("categories"),
        "pdf_url": _arxiv_pdf_url(paper),
        "doi": paper.get("doi"),
        "journal_ref": paper.get("journal_ref"),
        "comment": paper.get("comment"),
    }
    # Absent unless a chain was attempted, so the default shape is unchanged.
    if followed_published is not None:
        result["followed_published"] = followed_published
    return result


def _format_biorxiv_metadata(
    paper: dict[str, Any],
    canonical_id: str | None,
    *,
    followed_published: bool | None = None,
) -> dict[str, Any]:
    result = {
        "_source": "biorxiv",
        "_canonical_id": canonical_id,
        "doi": paper.get("doi"),
        "title": paper.get("title"),
        "date": paper.get("date"),
        "version": paper.get("version"),
        "type": paper.get("type"),
        "category": paper.get("category"),
        "license": paper.get("license"),
        "server": paper.get("server"),
        "published_doi": paper.get("published_doi"),
        "published_journal": paper.get("published_journal"),
        "published_date": paper.get("published_date"),
        "funding": dict_list(paper.get("funding")),
        "pdf_url": paper.get("pdf_url"),
    }
    # Absent unless a chain was attempted, so the default shape is unchanged.
    if followed_published is not None:
        result["followed_published"] = followed_published
    return result


def _format_acl_metadata(paper: dict[str, Any], canonical_id: str | None) -> dict[str, Any]:
    return {
        "_source": "acl_anthology",
        "_canonical_id": canonical_id,
        "anthology_id": paper.get("anthology_id"),
        "title": paper.get("title"),
        "doi": paper.get("doi"),
        "year": paper.get("year"),
        "month": paper.get("month"),
        "booktitle": paper.get("booktitle"),
        "volume_type": paper.get("volume_type"),
        "journal_volume": paper.get("journal_volume"),
        "journal_issue": paper.get("journal_issue"),
        "venues": paper.get("venues"),
        "publisher": paper.get("publisher"),
        "address": paper.get("address"),
        "pages": paper.get("pages"),
        "bibkey": paper.get("bibkey"),
        "url": paper.get("url"),
        "pdf_url": paper.get("pdf_url"),
    }


def _openalex_pmid(work: dict[str, Any]) -> str | None:
    """The work's PMID as bare digits, or ``None``.

    OpenAlex spells it as a PubMed URL; normalized here so the server hands back
    a spelling it also accepts. ``isdecimal``, not ``is_pmid``: that predicate's
    floor on a bare run is a routing tier, and short PMIDs on early papers are
    real.
    """
    raw = as_dict(work.get("ids")).get("pmid")
    if not isinstance(raw, str) or not raw:
        return None
    normalized = openalex.normalize_pmid(raw)
    return normalized if normalized.isdecimal() else None


def _format_openalex_metadata(work: dict[str, Any], canonical_id: str | None) -> dict[str, Any]:
    primary_location = as_dict(work.get("primary_location"))
    source_obj = as_dict(primary_location.get("source"))
    oa = as_dict(work.get("open_access"))
    return {
        "_source": "openalex",
        "_canonical_id": canonical_id,
        "title": work.get("title"),
        "doi": work.get("doi"),
        "pmid": _openalex_pmid(work),
        "publication_year": work.get("publication_year"),
        "publication_date": work.get("publication_date"),
        "type": work.get("type"),
        "language": work.get("language"),
        "venue": source_obj.get("display_name"),
        "cited_by_count": work.get("cited_by_count"),
        "is_oa": oa.get("is_oa"),
        "oa_status": oa.get("oa_status"),
        "oa_url": oa.get("oa_url"),
        "pdf_url": openalex.best_pdf_url(work),
    }


def _format_openalex_via_biorxiv(
    work: dict[str, Any], preprint_doi: str | None, journal_canonical: str
) -> dict[str, Any]:
    base = _format_openalex_metadata(work, journal_canonical)
    base["_source"] = "openalex_via_biorxiv"
    base["preprint_doi"] = preprint_doi
    base["followed_published"] = True  # symmetric with the False on fall-through
    return base


def _format_openalex_via_arxiv(
    work: dict[str, Any], preprint_arxiv_id: str, journal_canonical: str
) -> dict[str, Any]:
    base = _format_openalex_metadata(work, journal_canonical)
    base["_source"] = "openalex_via_arxiv"
    base["preprint_arxiv_id"] = preprint_arxiv_id
    base["followed_published"] = True  # symmetric with the False on fall-through
    return base


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


def _format_crossref_metadata(work: dict[str, Any], canonical_id: str | None) -> dict[str, Any]:
    """Crossref work → the unified metadata shape.

    Mirrors ``_format_openalex_metadata``'s key set, with is_oa / oa_status / oa_url /
    pdf_url always null.
    """
    year, date = crossref_date(work)
    # Crossref's own name for the same number; int-guarded, the value is untyped JSON.
    cited_by = work.get("is-referenced-by-count")
    return {
        "_source": "crossref",
        "_canonical_id": canonical_id,
        "title": unwrap_first(work.get("title")),
        "doi": work.get("DOI"),
        "publication_year": year,
        "publication_date": date,
        "type": work.get("type"),
        "language": work.get("language"),
        "venue": unwrap_first(work.get("container-title")),
        "cited_by_count": cited_by if isinstance(cited_by, int) else None,
        "is_oa": None,
        "oa_status": None,
        "oa_url": None,
        "pdf_url": None,
    }


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
    if not (fallback_crossref and source == "openalex" and canonical_id and obj.get("not_found")):
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


def _format_crossref_authors(work: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    """One page of Crossref authors, in ``_format_openalex_authors``' shape.

    ``institutions`` is real here — Crossref carries a per-author ``affiliation``
    — but usually empty, since most publishers do not deposit it; the
    ``page_institutions`` keys are emitted either way, so a paginating agent
    never feature-detects. ``author_count`` counts the **filtered** list the page
    was sliced from, never the raw one.
    """
    all_authors = dict_list(work.get("author"))
    page_authors: list[dict[str, Any]] = []
    page_institutions: list[str] = []
    for a in all_authors[start:end]:
        inst_names = [
            name
            for inst in dict_list(a.get("affiliation"))
            if isinstance(name := inst.get("name"), str) and name
        ]
        for name in inst_names:
            if name not in page_institutions:
                page_institutions.append(name)
        page_authors.append(
            {
                "name": crossref.author_name(a),
                "openalex_id": None,
                "position": a.get("sequence"),
                "is_corresponding": None,
                "institutions": inst_names,
            }
        )
    return {
        "author_count": len(all_authors),
        "authors": page_authors,
        "page_institution_count": len(page_institutions),
        "page_institutions": page_institutions,
    }


def _format_metadata_by_source(
    source: manual.MetadataSource, obj: dict[str, Any], canonical_id: str | None
) -> dict[str, Any]:
    """Dispatch a raw provider object to its per-source formatter.

    openalex is the fall-through, so callers gate on a successful ``_fetch_source``.
    """
    if source == "arxiv":
        return _format_arxiv_metadata(obj, canonical_id)
    if source == "biorxiv":
        return _format_biorxiv_metadata(obj, canonical_id)
    if source == "acl_anthology":
        return _format_acl_metadata(obj, canonical_id)
    return _format_openalex_metadata(obj, canonical_id)


@mcp.tool
async def get_paper_metadata(
    identifier: PAPER_ID,
    follow_published: FOLLOW_PUBLISHED = False,
    force_refresh: FORCE_REFRESH = False,
    fallback_crossref: FALLBACK_CROSSREF = False,
) -> dict[str, Any]:
    """Get core metadata for a paper, dispatched by identifier shape.

    Returns ``{_source, _canonical_id, ...source-native fields}``, where
    ``_canonical_id`` is the provider's normalized identifier — reuse it on
    later calls instead of re-normalizing whatever the user typed.

      - arxiv: arxiv_id, title, published, updated, primary_category,
        categories, pdf_url, doi (the journal version's, when the authors recorded
        one), journal_ref, comment. License and revision history are
        get_paper_versions'.
      - biorxiv: doi, title, date, version, type, category, license, server,
        published_doi, published_journal, published_date, funding, pdf_url. The
        published_* fields are null while unpublished; funding is ``[{name, id,
        id_type, award}]``, recorded only for papers posted since April 2025.
      - For arxiv and biorxiv, a ``follow_published`` chain that didn't reach the
        journal version adds ``followed_published=False``, plus
        ``published_lookup_retryable=True`` if that lookup failed transiently
        (5xx/429/timeout); both absent when no chain was attempted.
      - acl_anthology: anthology_id, title, doi, year, month, booktitle,
        volume_type ("journal" for TACL/CL, else "proceedings"), journal_volume,
        journal_issue, venues, publisher, address, pages, bibkey, url, pdf_url. No
        citation count; doi is often null, and the journal fields are null outside
        a journal.
      - openalex: title, doi, pmid, publication_year, publication_date, type,
        language, venue, cited_by_count, is_oa, oa_status, oa_url, pdf_url.
        ``pmid`` is bare digits (null when OpenAlex has none) and is itself an
        accepted identifier; ``cited_by_count`` drifts with time.
      - openalex_via_biorxiv / openalex_via_arxiv (``follow_published`` reached the
        journal version): openalex's fields plus preprint_doi / preprint_arxiv_id
        and ``followed_published=True``, ``_canonical_id`` being the journal DOI.
      - crossref (``fallback_crossref`` after an OpenAlex 404): openalex's fields
        with is_oa / oa_status / oa_url / pdf_url null, and no abstract path.
        ``cited_by_count`` is Crossref's own tally, which differs from OpenAlex's.

    A bioRxiv DOI bioRxiv fails to serve transiently is answered by OpenAlex:
    ``_source: "openalex"`` plus ``biorxiv_unavailable: true``, with no
    ``follow_published`` chain.

    A PMID dispatches as its DOI, so ``_source`` is ``openalex`` and
    ``_canonical_id`` the DOI whichever of the two you passed. An Anthology URL or
    hosted DOI dispatches as the Anthology ID.

    Errors: an unresolvable identifier returns ``{error}``; a provider failure
    returns ``{error, suggestion}``, plus ``crossref_fallback_retryable: true``
    if the fallback's own Crossref call failed transiently, or
    ``openalex_fallback_retryable: true`` if OpenAlex, standing in for bioRxiv, did. Siblings
    get_paper_authors / _abstract / _bibtex share this dispatch, the cached
    object and ``fallback_crossref``. For many identifiers at once, use
    get_papers_metadata.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error

    if (
        follow_published
        and source == "biorxiv"
        and "error" not in obj
        and (published_doi := obj.get("published_doi"))
    ):
        return await _follow_published(
            published_doi,
            journal=lambda work, doi: _format_openalex_via_biorxiv(work, obj.get("doi"), doi),
            preprint=lambda: _format_biorxiv_metadata(obj, canonical_id, followed_published=False),
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
            journal=lambda work, doi: _format_openalex_via_arxiv(
                work, arxiv.id_from_entry(obj), doi
            ),
            preprint=lambda: _format_arxiv_metadata(obj, canonical_id, followed_published=False),
            force_refresh=force_refresh,
        )

    cr, cr_retryable = await _crossref_fallback(
        source, canonical_id, obj, fallback_crossref=fallback_crossref, force_refresh=force_refresh
    )
    if cr is not None:
        return _format_crossref_metadata(cr, canonical_id)

    if "error" in obj:
        return _provider_error(obj, source, crossref_retryable=cr_retryable)
    return _flag_fallback(_format_metadata_by_source(source, obj, canonical_id), obj)


@mcp.tool
async def get_paper_versions(
    identifier: PAPER_ID,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """A preprint's license and revision history, for an arXiv ID or bioRxiv/medRxiv DOI.

    Returns ``{_source, _canonical_id, submitter, license, versions, version_count}``,
    versions oldest first; ``_canonical_id`` is unversioned.
      - arxiv: plus ``arxiv_id``; versions ``{version, date, size}``, ``date`` ISO 8601
        UTC, ``size`` arXiv's string (``"1102kb"``). Its own request, cached a day.
      - biorxiv: plus ``doi`` and ``server``; versions ``{version, date, license}``;
        ``submitter`` null. Read from the cached metadata record, so free.

    ``license`` is the latest version's: a URL on arXiv (null on older papers), a code
    on bioRxiv (``cc_by``). It decides reuse: arXiv's non-exclusive distribution
    license (``…/licenses/nonexclusive-distrib/1.0/``) and bioRxiv's ``cc_no`` do not
    permit redistribution; a Creative Commons license may.

    Errors: ``{error, suggestion}`` plus ``not_found: true`` (a paper the provider
    lacks, or another identifier, refused without a request), ``retryable: true``
    (transport or parse failure) or ``retryable: false`` (any other provider error).
    """
    if biorxiv.is_biorxiv_doi(identifier):
        return await _biorxiv_versions(identifier, force_refresh=force_refresh)

    if not arxiv.is_arxiv_id(identifier):
        return {
            **http.not_found(f"Not an arXiv ID or bioRxiv/medRxiv DOI: {identifier!r}."),
            "suggestion": "Pass the preprint's arXiv ID or 10.1101 DOI; find it with "
            "get_paper_metadata or search_arxiv.",
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


@mcp.tool
async def get_papers_metadata(
    identifiers: Annotated[
        list[str],
        Field(
            description=(
                "List of paper identifiers (arXiv IDs, ACL Anthology IDs, DOIs, "
                "PMIDs). Mixed "
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
    """Batch metadata fetch — same payload as get_paper_metadata, in bulk.

    For reference-graph traversal: uncached OpenAlex DOIs are chunked into
    ``/works?filter=doi:...|...`` calls and uncached arXiv IDs into ``id_list``
    calls, bioRxiv and the ACL Anthology fetch concurrently,
    cached entries cost no HTTP call, and every fetched paper warms the singleton
    cache so a later get_paper_metadata / _authors is free.

    Does NOT support follow_published — chain per-paper instead.

    Returns ``{count, papers}``: each entry is get_paper_metadata's payload plus
    ``_input``, the original string, so an agent can correlate input to output.
    Order matches the input list; failures appear as ``{_input, error, suggestion?}``
    and don't affect others. OpenAlex fallback answers carry ``biorxiv_unavailable: true``.
    """
    n = len(identifiers)
    results: list[dict[str, Any] | None] = [None] * n

    singleton_tasks: list[asyncio.Task] = []
    openalex_indices: list[tuple[int, str, str]] = []  # (slot, routed id, caller's input)
    arxiv_indices: list[tuple[int, str, str]] = []

    async def _singleton_one(slot: int, routed: str, ident: str) -> None:
        source, canonical, obj = await _fetch_source(routed, force_refresh=force_refresh)
        if source is None:
            # Unreachable: the loop routes only arXiv/bioRxiv/ACL here. Guards the hint
            # lookup against a None key regardless.
            results[slot] = {"_input": ident, **obj}
            return
        if "error" in obj:
            results[slot] = {
                "_input": ident,
                **enrich_error(obj, _METADATA_HINT_BY_SOURCE[source]),
            }
            return
        formatted = _flag_fallback(_format_metadata_by_source(source, obj, canonical), obj)
        formatted["_input"] = ident
        results[slot] = formatted

    # First and concurrently: routing is by shape, and a PMID has none until it is
    # traded. `_input` stays the caller's spelling.
    resolved = await asyncio.gather(
        *(resolve_paper_identifier(ident, force_refresh=force_refresh) for ident in identifiers)
    )

    for i, (ident, (routed, pmid_error)) in enumerate(zip(identifiers, resolved, strict=True)):
        if pmid_error is not None:
            results[i] = {"_input": ident, **pmid_error}
            continue
        source = manual.resolve_metadata_source(routed)
        if source in ("biorxiv", "acl_anthology"):
            singleton_tasks.append(asyncio.create_task(_singleton_one(i, routed, ident)))
        elif source == "arxiv":
            arxiv_indices.append((i, routed, ident))
        elif source == "openalex":
            openalex_indices.append((i, routed, ident))
        else:
            results[i] = {"_input": ident, **_unknown_identifier_error(ident)}

    async def _openalex_batch() -> None:
        if not openalex_indices:
            return
        batch = await openalex.get_works_batch(
            [d for _, d, _ in openalex_indices], force_refresh=force_refresh
        )
        for slot, routed, ident in openalex_indices:
            canonical = openalex.canonical_doi(routed)
            work = batch.get(canonical)
            # get_works_batch is total, so `is None` means a test stub. dict(): batch
            # entries aren't deep-copied and two spellings of one DOI share the one entry.
            if work is None or "error" in work:
                err = work or {"error": f"No work found for DOI: {routed}"}
                results[slot] = {
                    "_input": ident,
                    **enrich_error(dict(err), _OPENALEX_METADATA_HINT),
                }
                continue
            formatted = _format_openalex_metadata(work, canonical)
            formatted["_input"] = ident
            results[slot] = formatted

    async def _arxiv_batch() -> None:
        if not arxiv_indices:
            return
        # One id_list call per chunk: singletons queue behind arXiv's single connection
        # and a fan-out past its burst cap is refused.
        batch = await arxiv.get_papers_batch(
            [r for _, r, _ in arxiv_indices], force_refresh=force_refresh
        )
        for slot, routed, ident in arxiv_indices:
            canonical = arxiv.canonical_arxiv_id(routed)
            paper = batch.get(canonical)
            # get_papers_batch is total, so `is None` means a test stub. dict(): batch
            # entries aren't deep-copied and two spellings of one id share the one entry.
            if paper is None or "error" in paper:
                err = paper or {"error": f"No paper found for arXiv ID: {routed}"}
                results[slot] = {
                    "_input": ident,
                    **enrich_error(dict(err), _ARXIV_METADATA_HINT),
                }
                continue
            formatted = _format_arxiv_metadata(paper, canonical)
            formatted["_input"] = ident
            results[slot] = formatted

    await asyncio.gather(*singleton_tasks, _openalex_batch(), _arxiv_batch())

    # Defensive: a slot still None is a bug — surface it, don't crash the caller.
    for i, r in enumerate(results):
        if r is None:
            results[i] = {
                "_input": identifiers[i],
                "error": "Internal: no result produced for this identifier.",
            }

    return {"count": n, "papers": results}


def _format_openalex_authors(work: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    """One page of OpenAlex authors, plus that page's deduped institution roll-up.

    Returns ``{author_count, authors, page_institutions, page_institution_count}``,
    ``author_count`` being the whole list; the caller adds the shared envelope.
    """
    all_authorships = dict_list(work.get("authorships"))
    page_authors: list[dict[str, Any]] = []
    page_institutions: list[str] = []
    for a in all_authorships[start:end]:
        author_info = as_dict(a.get("author"))
        inst_names = [
            name
            for inst in dict_list(a.get("institutions"))
            if isinstance(name := inst.get("display_name"), str) and name
        ]
        for name in inst_names:
            if name not in page_institutions:
                page_institutions.append(name)
        page_authors.append(
            {
                "name": author_info.get("display_name"),
                "openalex_id": author_info.get("id"),
                "position": a.get("author_position"),
                "is_corresponding": a.get("is_corresponding"),
                "institutions": inst_names,
            }
        )
    return {
        "author_count": len(all_authorships),
        "authors": page_authors,
        "page_institution_count": len(page_institutions),
        "page_institutions": page_institutions,
    }


@mcp.tool
async def get_paper_authors(
    identifier: PAPER_ID,
    page: AUTHORS_PAGE = 1,
    page_size: AUTHORS_PAGE_SIZE = 25,
    force_refresh: FORCE_REFRESH = False,
    fallback_crossref: FALLBACK_CROSSREF = False,
) -> dict[str, Any]:
    """Get a page of the author list, dispatched by identifier shape.

    Slicing is in-memory against the cached paper, so paging costs no extra API hits.

    Returns ``{_source, _canonical_id, author_count, page, page_size, has_more,
    authors, page_institutions, page_institution_count}``; ``author_count`` is the
    whole list, not the page.
      - arxiv: authors = [{name, affiliations}]; page_institutions [] and
        page_institution_count 0 — arXiv has no per-author roll-up.
      - acl_anthology: authors = [{name, first, last, orcid, affiliation}], orcid
        and affiliation nullable; page_institutions [] and page_institution_count 0.
      - biorxiv: authors = [{name}], plus author_corresponding /
        author_corresponding_institution on every page; page_institutions [] and
        page_institution_count 0 — bioRxiv exposes only the corresponding author's
        institution.
      - openalex: authors = [{name, openalex_id, position, is_corresponding,
        institutions}]; page_institutions / page_institution_count cover the current
        page only (dedupe across pages for a global view). openalex_id chains into
        get_author.
      - crossref (``fallback_crossref`` after an OpenAlex 404): the same keys,
        with openalex_id and is_corresponding null and position carrying
        Crossref's ``sequence`` ("first" / "additional"). institutions come from
        its per-author affiliation, which most publishers omit — usually empty.

    Errors: an unresolvable identifier returns ``{error}``; a provider failure
    returns ``{error, suggestion}``, plus ``crossref_fallback_retryable: true``
    if the fallback's own Crossref call failed transiently, or
    ``openalex_fallback_retryable: true`` if OpenAlex, standing in for bioRxiv, did.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error

    start, end = page_bounds(page, page_size)

    cr, cr_retryable = await _crossref_fallback(
        source, canonical_id, obj, fallback_crossref=fallback_crossref, force_refresh=force_refresh
    )
    if cr is not None:
        page_slice = _format_crossref_authors(cr, start, end)
        total = page_slice["author_count"]
        return {
            "_source": "crossref",
            "_canonical_id": canonical_id,
            "page": page,
            "page_size": page_size,
            "has_more": end < total,
            **page_slice,
        }

    if "error" in obj:
        return _provider_error(obj, source, crossref_retryable=cr_retryable)

    if source == "openalex":
        page_slice = _format_openalex_authors(obj, start, end)
    else:
        # arXiv/bioRxiv/ACL have no institution records; emit them empty so agents
        # never feature-detect.
        authors = obj.get("authors", [])
        page_slice = {
            "author_count": len(authors),
            "authors": authors[start:end],
            "page_institutions": [],
            "page_institution_count": 0,
        }

    total = page_slice["author_count"]
    result = {
        "_source": source,
        "_canonical_id": canonical_id,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        **page_slice,
    }
    if source == "biorxiv":
        result["author_corresponding"] = obj.get("author_corresponding")
        result["author_corresponding_institution"] = obj.get("author_corresponding_institution")
    return _flag_fallback(result, obj)


@mcp.tool
async def get_paper_abstract(
    identifier: PAPER_ID,
    force_refresh: FORCE_REFRESH = False,
    fallback_crossref: FALLBACK_CROSSREF = False,
) -> dict[str, Any]:
    """Get a paper's abstract as plain text, dispatched by identifier shape.

    Returns ``{_source, _canonical_id, title, abstract}``; ``abstract`` is null
    when the source has none. OpenAlex abstracts are reconstructed from an
    inverted index — not byte-identical to the publisher's. A crossref abstract
    (``fallback_crossref`` after an OpenAlex 404) is JATS rendered to plain text,
    section titles included; many Crossref records carry none at all.

    Errors: an unresolvable identifier returns ``{error}``; a provider failure
    returns ``{error, suggestion}``, plus ``crossref_fallback_retryable: true``
    if the fallback's own Crossref call failed transiently, or
    ``openalex_fallback_retryable: true`` if OpenAlex, standing in for bioRxiv, did.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error

    cr, cr_retryable = await _crossref_fallback(
        source, canonical_id, obj, fallback_crossref=fallback_crossref, force_refresh=force_refresh
    )
    if cr is not None:
        return {
            "_source": "crossref",
            "_canonical_id": canonical_id,
            "title": unwrap_first(cr.get("title")),
            "abstract": crossref.abstract_text(cr),
        }

    if "error" in obj:
        return _provider_error(obj, source, crossref_retryable=cr_retryable)

    if source == "arxiv":
        abstract = obj.get("summary")
    elif source in ("biorxiv", "acl_anthology"):
        abstract = obj.get("abstract")
    else:
        abstract = openalex.reconstruct_abstract(obj.get("abstract_inverted_index")) or None

    result = {
        "_source": source,
        "_canonical_id": canonical_id,
        "title": obj.get("title"),
        "abstract": abstract,
    }
    return _flag_fallback(result, obj)


@mcp.tool
async def get_paper_bibtex(
    identifier: PAPER_ID,
    force_refresh: FORCE_REFRESH = False,
    fallback_crossref: FALLBACK_CROSSREF = False,
) -> dict[str, Any]:
    """Generate a BibTeX entry, dispatched by identifier shape.

    Returns ``{_source, _canonical_id, bibtex}``. Entry type per source:
      - arxiv: @article if the paper has journal_ref, else @misc. Both carry
        eprint / archiveprefix, plus primaryclass when the paper has a
        primary_category.
      - biorxiv: @article when published_doi is present, with journal and year
        when known; else @misc with the preprint DOI and server.
      - acl_anthology: @article for a journal (TACL, CL), else @inproceedings
        with editor, booktitle and address.
      - openalex: inferred from the work type (@article, @inproceedings,
        @misc for preprints, @phdthesis, etc.).
      - crossref (``fallback_crossref`` after an OpenAlex 404): inferred from
        Crossref's own, different type vocabulary (@article for journal-article,
        @inproceedings for proceedings-article, @misc for posted-content, etc.).

    Errors: an unresolvable identifier returns ``{error}``; a provider failure
    returns ``{error, suggestion}``, plus ``crossref_fallback_retryable: true``
    if the fallback's own Crossref call failed transiently, or
    ``openalex_fallback_retryable: true`` if OpenAlex, standing in for bioRxiv, did.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error

    cr, cr_retryable = await _crossref_fallback(
        source, canonical_id, obj, fallback_crossref=fallback_crossref, force_refresh=force_refresh
    )
    if cr is not None:
        return {
            "_source": "crossref",
            "_canonical_id": canonical_id,
            "bibtex": generate_crossref_bibtex(cr, year=crossref_date(cr)[0]),
        }

    if "error" in obj:
        return _provider_error(obj, source, crossref_retryable=cr_retryable)

    if source == "arxiv":
        bibtex = generate_arxiv_bibtex(obj)
    elif source == "biorxiv":
        bibtex = generate_biorxiv_bibtex(obj)
    elif source == "acl_anthology":
        bibtex = generate_acl_bibtex(obj)
    else:
        bibtex = generate_bibtex(obj)

    result = {
        "_source": source,
        "_canonical_id": canonical_id,
        "bibtex": bibtex,
    }
    return _flag_fallback(result, obj)


@mcp.tool
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

    stats = as_dict(author.get("summary_stats"))
    current_institutions = [
        name
        for inst in dict_list(author.get("last_known_institutions"))
        if isinstance(name := inst.get("display_name"), str) and name
    ]
    top_topics = [
        {"name": t.get("display_name"), "count": t.get("count")}
        for t in dict_list(author.get("topics"))[:5]
    ]
    affiliations = []
    for aff in dict_list(author.get("affiliations")):
        inst = as_dict(aff.get("institution"))
        raw_years = aff.get("years")
        years = raw_years if isinstance(raw_years, list) else []
        affiliations.append(
            {
                "institution": inst.get("display_name"),
                "country_code": inst.get("country_code"),
                # Ints or nothing, so a mixed list can't make ``sorted`` raise.
                "years": sorted(y for y in years if isinstance(y, int)),
            }
        )

    return {
        "name": author.get("display_name"),
        "openalex_id": author.get("id"),
        "orcid": author.get("orcid"),
        "works_count": author.get("works_count"),
        "cited_by_count": author.get("cited_by_count"),
        "h_index": stats.get("h_index"),
        "i10_index": stats.get("i10_index"),
        "current_institutions": current_institutions,
        "top_topics": top_topics,
        "affiliations": affiliations,
    }


# Names this tool in the DOI-only refusal `resolve_doi_identifier` builds.
_UPDATES_SUBJECT = "Crossref update notices"


@mcp.tool
async def get_paper_updates(doi: DOI, force_refresh: FORCE_REFRESH = False) -> dict[str, Any]:
    """Check a DOI for retraction and correction notices, and for related preprint DOIs.

    Returns ``{doi, retracted, updates, relations}``, the echoed ``doi`` canonical
    rather than the spelling you passed. ``updates`` is
    [{doi, type, label, source, year, date}] — ``type`` is Crossref's (``retraction``,
    ``withdrawal``, ``correction``, ``erratum``, ``expression_of_concern``, …),
    ``source`` is ``publisher`` or ``retraction-watch``, and ``doi`` is the notice,
    which get_paper_metadata reads like any other paper. ``relations`` maps Crossref's
    relation names to DOIs (``is-preprint-of``, ``has-preprint``, ``is-version-of``).

    ``retracted`` covers the whole withdrawing class — ``retraction``,
    ``partial_retraction``, ``withdrawal``, ``removal`` — since none of those leave a
    paper citable; read ``type`` for which, and note that an amending notice (a
    correction, an expression of concern) is reported in ``updates`` without setting
    the flag, so scan the list rather than the boolean alone.

    **``retracted: false`` means Crossref lists no such notice, not that the paper
    stands** — it answers an undeposited DOI and a sound paper identically, so it
    clears a citation rather than certifying one. ``relations`` is ``{}`` for most
    papers, publisher deposit being thin, so an absent ``is-preprint-of`` is no claim
    that no preprint exists; get_paper_metadata's ``follow_published`` answers that
    more fully.

    Crossref-only, and free on a paper already cached. Errors: a non-DOI identifier is
    rejected locally without a request, and a DOI Crossref has no record for returns
    ``{error, not_found, suggestion}``, naming the registering agency when it is not
    Crossref.
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
