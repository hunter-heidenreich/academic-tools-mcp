"""Paper metadata tools: metadata / authors / abstract / bibtex / author lookup."""

import asyncio
from typing import Annotated, Any

from pydantic import Field

from .. import manual
from ..app import (
    AUTHOR_ID,
    AUTHORS_PAGE,
    AUTHORS_PAGE_SIZE,
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
    resolve_paper_identifier,
    unwrap_first,
)
from ..bibtex import generate_arxiv_bibtex, generate_bibtex, generate_biorxiv_bibtex
from ..providers import arxiv, biorxiv, crossref, openalex


def _canonical_for_source(source: manual.MetadataSource | None, identifier: str) -> str | None:
    """The provider's canonical form of ``identifier``, echoed as ``_canonical_id``."""
    if source == "arxiv":
        return arxiv.canonical_arxiv_id(identifier)
    if source == "biorxiv":
        return biorxiv.canonical_key(identifier)
    if source == "openalex":
        return openalex.canonical_doi(identifier)
    return None


def _unknown_identifier_error(identifier: str) -> dict[str, Any]:
    """Return an error dict for identifiers that don't resolve to any provider."""
    return {
        "error": (
            f"Cannot resolve paper provider for identifier: {identifier!r}. "
            "Use an arXiv ID (e.g. 2301.00001), a DOI (e.g. 10.1038/...), "
            "or call search_arxiv / search_crossref_by_title to find one."
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

_OPENALEX_METADATA_HINT = (
    "Check the DOI format or use search_crossref_by_title to find the correct DOI."
)

# Must stay total over ``MetadataSource``: subscripted on the error path, where a
# KeyError would surface late.
_METADATA_HINT_BY_SOURCE: dict[manual.MetadataSource, str] = {
    "arxiv": _ARXIV_METADATA_HINT,
    "biorxiv": _BIORXIV_METADATA_HINT,
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
    top is uniform across them.
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
    elif source == "openalex":
        obj = await openalex.get_work(identifier, force_refresh=force_refresh)
    else:
        return None, None, _unknown_identifier_error(identifier)
    return source, canonical_id, obj


def _format_arxiv_metadata(paper: dict[str, Any], canonical_id: str | None) -> dict[str, Any]:
    return {
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
        "pdf_url": paper.get("pdf_url"),
    }
    # Absent unless a chain was attempted, so the default shape is unchanged.
    if followed_published is not None:
        result["followed_published"] = followed_published
    return result


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


def _format_crossref_metadata(work: dict[str, Any], canonical_id: str | None) -> dict[str, Any]:
    """Crossref work → the unified metadata shape.

    Mirrors ``_format_openalex_metadata``'s key set, with is_oa / oa_status / oa_url /
    pdf_url always null.
    """
    year, date = crossref_date(work)
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
        "is_oa": None,
        "oa_status": None,
        "oa_url": None,
        "pdf_url": None,
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
        categories, pdf_url, doi, journal_ref, comment.
      - biorxiv: doi, title, date, version, type, category, license, server,
        published_doi, pdf_url. A ``follow_published`` chain that didn't reach the
        journal version adds ``followed_published=False``, plus
        ``published_lookup_retryable=True`` if that lookup failed transiently
        (5xx/429/timeout); both absent when no chain was attempted.
      - openalex: title, doi, pmid, publication_year, publication_date, type,
        language, venue, is_oa, oa_status, oa_url, pdf_url. ``pmid`` is bare
        digits (null when OpenAlex has none) and is itself an accepted identifier.
      - openalex_via_biorxiv (``follow_published`` reached the journal version):
        openalex's fields plus preprint_doi and ``followed_published=True``,
        ``_canonical_id`` being the journal DOI.
      - crossref (``fallback_crossref`` after an OpenAlex 404): openalex's fields
        with is_oa / oa_status / oa_url / pdf_url null, and no abstract path.

    A PMID dispatches as its DOI, so ``_source`` is ``openalex`` and
    ``_canonical_id`` the DOI whichever of the two you passed.

    Errors: an unresolvable identifier returns ``{error}``; a provider failure
    returns ``{error, suggestion}``. Siblings get_paper_authors / _abstract /
    _bibtex share this dispatch and the cached object. For many identifiers at
    once, use get_papers_metadata.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error

    if source == "biorxiv" and "error" not in obj:
        published_doi = obj.get("published_doi")
        if follow_published and published_doi:
            work = await openalex.get_work(published_doi, force_refresh=force_refresh)
            if "error" not in work:
                return _format_openalex_via_biorxiv(
                    work, obj.get("doi"), openalex.canonical_doi(published_doi)
                )
            # Not indexed yet: fall back to the preprint — the agent asked for the best
            # version, not a failure.
            result = _format_biorxiv_metadata(obj, canonical_id, followed_published=False)
            if work.get("retryable") is True:
                result["published_lookup_retryable"] = True
            return result

    # force_refresh threads through: this path is for brand-new DOIs, where staleness
    # is likeliest.
    if source == "openalex" and obj.get("not_found") and fallback_crossref:
        cr = await crossref.get_work(identifier, force_refresh=force_refresh)
        if "error" not in cr:
            return _format_crossref_metadata(cr, crossref.canonical_doi(identifier))

    if "error" in obj:
        return enrich_error(obj, _METADATA_HINT_BY_SOURCE[source])
    return _format_metadata_by_source(source, obj, canonical_id)


@mcp.tool
async def get_papers_metadata(
    identifiers: Annotated[
        list[str],
        Field(
            description=(
                "List of paper identifiers (arXiv IDs and/or DOIs). Mixed "
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
    ``/works?filter=doi:...|...`` calls, arXiv and bioRxiv fetch concurrently,
    cached entries cost no HTTP call, and every fetched paper warms the singleton
    cache so a later get_paper_metadata / _authors is free.

    Does NOT support follow_published — chain per-paper instead.

    Returns ``{count, papers}``: each entry is get_paper_metadata's payload plus
    ``_input``, the original string, so an agent can correlate input to output.
    Order matches the input list; failures appear as ``{_input, error, suggestion?}``
    and don't affect others.
    """
    n = len(identifiers)
    results: list[dict[str, Any] | None] = [None] * n

    singleton_tasks: list[asyncio.Task] = []
    openalex_indices: list[tuple[int, str, str]] = []  # (slot, routed id, caller's input)

    async def _singleton_one(slot: int, routed: str, ident: str) -> None:
        source, canonical, obj = await _fetch_source(routed, force_refresh=force_refresh)
        if source is None:
            # Unreachable: the loop routes only arXiv/bioRxiv here. Guards the hint
            # lookup against a None key regardless.
            results[slot] = {"_input": ident, **obj}
            return
        if "error" in obj:
            results[slot] = {
                "_input": ident,
                **enrich_error(obj, _METADATA_HINT_BY_SOURCE[source]),
            }
            return
        formatted = _format_metadata_by_source(source, obj, canonical)
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
        if source in ("arxiv", "biorxiv"):
            singleton_tasks.append(asyncio.create_task(_singleton_one(i, routed, ident)))
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

    await asyncio.gather(*singleton_tasks, _openalex_batch())

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
) -> dict[str, Any]:
    """Get a page of the author list, dispatched by identifier shape.

    Slicing is in-memory against the cached paper, so paging costs no extra API hits.

    Returns ``{_source, _canonical_id, author_count, page, page_size, has_more,
    authors, page_institutions, page_institution_count}``; ``author_count`` is the
    whole list, not the page.
      - arxiv: authors = [{name, affiliations}]; page_institutions [] and
        page_institution_count 0 — arXiv has no per-author roll-up.
      - biorxiv: authors = [{name}], plus author_corresponding /
        author_corresponding_institution on every page; page_institutions [] and
        page_institution_count 0 — bioRxiv exposes only the corresponding author's
        institution.
      - openalex: authors = [{name, openalex_id, position, is_corresponding,
        institutions}]; page_institutions / page_institution_count cover the current
        page only (dedupe across pages for a global view). openalex_id chains into
        get_author.

    Errors: an unresolvable identifier returns ``{error}``; a provider failure
    returns ``{error, suggestion}``.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error
    if "error" in obj:
        return enrich_error(obj, _METADATA_HINT_BY_SOURCE[source])

    start, end = page_bounds(page, page_size)

    if source == "openalex":
        page_slice = _format_openalex_authors(obj, start, end)
    else:
        # arXiv/bioRxiv have no per-author institutions; emit them empty so agents
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
    return result


@mcp.tool
async def get_paper_abstract(
    identifier: PAPER_ID,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Get a paper's abstract as plain text, dispatched by identifier shape.

    Returns ``{_source, _canonical_id, title, abstract}``. OpenAlex abstracts are
    reconstructed from an inverted index — not byte-identical to the publisher's.

    Errors: an unresolvable identifier returns ``{error}``; a provider failure
    returns ``{error, suggestion}``.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error
    if "error" in obj:
        return enrich_error(obj, _METADATA_HINT_BY_SOURCE[source])

    if source == "arxiv":
        abstract = obj.get("summary")
    elif source == "biorxiv":
        abstract = obj.get("abstract")
    else:
        abstract = openalex.reconstruct_abstract(obj.get("abstract_inverted_index")) or None

    return {
        "_source": source,
        "_canonical_id": canonical_id,
        "title": obj.get("title"),
        "abstract": abstract,
    }


@mcp.tool
async def get_paper_bibtex(
    identifier: PAPER_ID,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Generate a BibTeX entry, dispatched by identifier shape.

    Returns ``{_source, _canonical_id, bibtex}``. Entry type per source:
      - arxiv: @article if the paper has journal_ref, else @misc. Both carry
        eprint / archiveprefix, plus primaryclass when the paper has a
        primary_category.
      - biorxiv: @article when published_doi is present, else @misc with
        the preprint DOI and server.
      - openalex: inferred from the work type (@article, @inproceedings,
        @misc for preprints, @phdthesis, etc.).

    Errors: an unresolvable identifier returns ``{error}``; a provider failure
    returns ``{error, suggestion}``.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error
    if "error" in obj:
        return enrich_error(obj, _METADATA_HINT_BY_SOURCE[source])

    if source == "arxiv":
        bibtex = generate_arxiv_bibtex(obj)
    elif source == "biorxiv":
        bibtex = generate_biorxiv_bibtex(obj)
    else:
        bibtex = generate_bibtex(obj)

    return {
        "_source": source,
        "_canonical_id": canonical_id,
        "bibtex": bibtex,
    }


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
    get_paper_authors or ORCID URLs.
    """
    author = await openalex.get_author(author_id, force_refresh=force_refresh)
    if "error" in author:
        return enrich_error(
            author, "Use an OpenAlex author ID (from get_paper_authors) or an ORCID URL."
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
