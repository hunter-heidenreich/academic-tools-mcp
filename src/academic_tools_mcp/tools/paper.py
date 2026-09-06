"""Paper metadata tools: metadata / authors / abstract / bibtex / author lookup."""

import asyncio
from typing import Annotated, Any

from pydantic import Field

from .. import manual
from .._app import (
    AUTHOR_ID,
    AUTHORS_PAGE,
    AUTHORS_PAGE_SIZE,
    FALLBACK_CROSSREF,
    FOLLOW_PUBLISHED,
    FORCE_REFRESH,
    PAPER_ID,
    _arxiv_id_from_entry,
    _crossref_date,
    _enrich_error,
    _first,
    mcp,
)
from ..bibtex import generate_arxiv_bibtex, generate_bibtex, generate_biorxiv_bibtex
from ..providers import arxiv, biorxiv, crossref, openalex


def _canonical_for_source(source: manual.MetadataSource | None, identifier: str) -> str | None:
    """Return the provider's canonical form of ``identifier``.

    Echoed back to agents as ``_canonical_id`` on every metadata
    response so callers can reuse the normalized form across subsequent
    tool calls instead of re-normalizing input each time.
    """
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

# Total over ``MetadataSource``: ``_enrich_error`` indexes it with whatever
# ``resolve_metadata_source`` returned, on the error path, where a KeyError
# would be least likely to be caught in review.
_METADATA_HINT_BY_SOURCE: dict[manual.MetadataSource, str] = {
    "arxiv": _ARXIV_METADATA_HINT,
    "biorxiv": _BIORXIV_METADATA_HINT,
    "openalex": _OPENALEX_METADATA_HINT,
}


async def _fetch_source(
    identifier: str, *, force_refresh: bool = False
) -> tuple[manual.MetadataSource | None, str | None, dict[str, Any]]:
    """Resolve an identifier and fetch its raw provider object.

    Centralizes the dispatch ritual the unified paper tools share: resolve the
    source, compute the canonical id, fetch the raw upstream object. Returns
    ``(source, canonical_id, obj)`` where ``obj`` is the provider's raw object
    on success or its raw (un-enriched) error dict on failure. When no provider
    matches, ``source`` is ``None`` and ``obj`` is the unknown-identifier error.

    The error is left un-enriched so a caller that must branch on a
    provider-specific error flag before attaching a suggestion can do so —
    get_paper_metadata inspects OpenAlex's ``not_found`` for the Crossref
    fallback. Simple callers enrich via ``_METADATA_HINT_BY_SOURCE[source]``.
    """
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
        "arxiv_id": _arxiv_id_from_entry(paper),
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
    # Only surfaced when a follow_published chain was actually attempted; left
    # absent otherwise so the default response shape is unchanged.
    if followed_published is not None:
        result["followed_published"] = followed_published
    return result


def _format_openalex_metadata(work: dict[str, Any], canonical_id: str | None) -> dict[str, Any]:
    primary_location = work.get("primary_location") or {}
    source_obj = primary_location.get("source") or {}
    oa = work.get("open_access") or {}
    return {
        "_source": "openalex",
        "_canonical_id": canonical_id,
        "title": work.get("title"),
        "doi": work.get("doi"),
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
    # The chain succeeded — symmetric with the fall-through case, which sets
    # followed_published=False on the preprint record.
    base["followed_published"] = True
    return base


def _format_crossref_metadata(work: dict[str, Any], canonical_id: str | None) -> dict[str, Any]:
    """Format a raw Crossref work object into the unified metadata shape.

    Mirrors ``_format_openalex_metadata`` so callers branching on
    ``_source`` see a near-identical key set, but Crossref carries no
    open-access information — the OA fields are always null here.
    """
    year, date = _crossref_date(work)
    return {
        "_source": "crossref",
        "_canonical_id": canonical_id,
        "title": _first(work.get("title")),
        "doi": work.get("DOI"),
        "publication_year": year,
        "publication_date": date,
        "type": work.get("type"),
        "language": work.get("language"),
        "venue": _first(work.get("container-title")),
        "is_oa": None,
        "oa_status": None,
        "oa_url": None,
        "pdf_url": None,
    }


def _format_metadata_by_source(
    source: manual.MetadataSource, obj: dict[str, Any], canonical_id: str | None
) -> dict[str, Any]:
    """Dispatch a raw provider object to its per-source metadata formatter.

    The plain (non-special-case) success path for both get_paper_metadata and
    the get_papers_metadata batch closures, so the field mapping lives in one
    place. ``source`` must be one of the three known providers — callers gate on
    a successful ``_fetch_source`` before calling.
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

    Every successful response carries ``_canonical_id`` — the provider's
    normalized form of the identifier (lowercased DOI, version-stripped
    arXiv ID, etc.) — so subsequent tool calls can reuse it instead of
    re-normalizing whatever the user typed.

    Returns ``{_source, _canonical_id, ...source-native fields}``:
      - arxiv: arxiv_id, title, published, updated, primary_category,
        categories, pdf_url, doi, journal_ref, comment.
      - biorxiv: doi, title, date, version, type, category, license, server,
        published_doi (chain to OpenAlex for the journal version), pdf_url.
        When ``follow_published=True`` was requested but the OpenAlex lookup
        on the journal version didn't return it, also carries
        ``followed_published=False`` (preprint-era metadata for a paper that
        *is* published). If that lookup failed *transiently* (5xx/timeout,
        not a definitive 404), the fallback additionally carries
        ``published_lookup_retryable=True`` so the agent can retry the chain
        rather than assume the journal version is unindexed. The
        ``followed_published`` field is absent when no chain was attempted
        (``follow_published=False`` or no ``published_doi``).
      - openalex: title, doi, publication_year, publication_date, type,
        language, venue, is_oa, oa_status, oa_url.
      - openalex_via_biorxiv: identical to openalex, plus preprint_doi and
        ``followed_published=True``. Only produced when ``follow_published=True``
        for a bioRxiv DOI whose journal version is in OpenAlex.
      - crossref: title, doi, publication_year, publication_date, type,
        language, venue, plus null OA fields. Only produced when
        ``fallback_crossref=True`` and OpenAlex 404s the DOI. Crossref
        often indexes new DOIs before OpenAlex but carries no open-access
        info (and get_paper_abstract has no Crossref path).

    Errors: unknown identifier or paper not found returns ``{error, suggestion}``.
    Sibling tools (get_paper_authors / get_paper_abstract / get_paper_bibtex)
    share the same dispatch and cached upstream object.

    For many identifiers at once, use get_papers_metadata — it batches
    OpenAlex DOIs into one HTTP call and fans out arXiv / bioRxiv
    fetches concurrently.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error

    # bioRxiv → journal chaining (special case on a successful preprint record).
    if source == "biorxiv" and "error" not in obj:
        published_doi = obj.get("published_doi")
        if follow_published and published_doi:
            # Chain to OpenAlex for the journal version. If OpenAlex
            # doesn't have it (paper too new to index, etc.) we fall
            # back to the preprint metadata rather than erroring — the
            # agent asked for "the best version", not "fail if no
            # journal record".
            work = await openalex.get_work(published_doi, force_refresh=force_refresh)
            if "error" not in work:
                return _format_openalex_via_biorxiv(
                    work,
                    obj.get("doi"),
                    openalex.canonical_doi(published_doi),
                )
            # OpenAlex didn't return the journal version — fall back to the
            # preprint record but signal followed_published=False so the agent
            # knows it's looking at preprint-era metadata for a paper that *is*
            # published (vs. one that simply isn't published yet). A definitive
            # 404 (not_found) means "not indexed yet"; a transient failure
            # (5xx / timeout, no not_found) means a retry might surface the
            # record, so tag it so the agent can distinguish the two rather
            # than treating a flaky lookup as a permanent miss.
            result = _format_biorxiv_metadata(obj, canonical_id, followed_published=False)
            if not work.get("not_found"):
                result["published_lookup_retryable"] = True
            return result

    # OpenAlex definitive 404 + opt-in Crossref fallback (Crossref often indexes
    # new/niche DOIs ahead of OpenAlex). Inspect the raw, un-enriched error here
    # — that's why _fetch_source doesn't attach the suggestion itself. Crossref
    # force_refresh is threaded into the fallback too: this path exists for
    # brand-new DOIs, which is exactly where a stale cached Crossref record is
    # most likely and least useful. If Crossref also misses, fall through to
    # the OpenAlex error below.
    if source == "openalex" and obj.get("not_found") and fallback_crossref:
        cr = await crossref.get_work(identifier, force_refresh=force_refresh)
        if "error" not in cr:
            return _format_crossref_metadata(cr, crossref.canonical_doi(identifier))

    if "error" in obj:
        return _enrich_error(obj, _METADATA_HINT_BY_SOURCE[source])
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

    Optimised for reference-graph traversal where an agent has 30–200
    DOIs from get_paper_references and wants metadata for all of them.
    OpenAlex DOIs are fanned into one ``/works?filter=doi:...|...`` call
    per 50 (vs. one HTTP call per DOI); arXiv and bioRxiv identifiers
    are fetched concurrently up to each provider's concurrency cap.
    Cached entries (positive or negative) are served without an HTTP
    call. Each successfully-fetched paper warms the singleton cache so
    a follow-up get_paper_metadata / get_paper_authors call is free.

    Does NOT support follow_published — chain bioRxiv-to-journal
    explicitly via per-paper get_paper_metadata calls.

    Returns ``{count, papers: [...]}`` where each entry is the same
    shape ``get_paper_metadata`` would return for that identifier (with
    an added ``_input`` field carrying the original input string so an
    agent can correlate input → output without re-resolving). Order
    matches the input list. Per-paper failures appear as
    ``{_input, error, suggestion?}`` entries; one failure does not
    affect the others.
    """
    n = len(identifiers)
    results: list[dict[str, Any] | None] = [None] * n

    singleton_tasks: list[asyncio.Task] = []
    openalex_indices: list[tuple[int, str]] = []  # (slot, original input)

    async def _singleton_one(slot: int, ident: str) -> None:
        # arXiv / bioRxiv fetch as concurrent singletons. The dispatch loop
        # routes OpenAlex to the batch and unknown ids never reach here, so
        # _fetch_source always resolves to a real provider.
        source, canonical, obj = await _fetch_source(ident, force_refresh=force_refresh)
        if source is None:
            # Unreachable through the loop below; if it ever happens, obj is
            # already the unknown-identifier error, so pass it straight out
            # instead of indexing _METADATA_HINT_BY_SOURCE with None.
            results[slot] = {"_input": ident, **obj}
            return
        if "error" in obj:
            results[slot] = {
                "_input": ident,
                **_enrich_error(obj, _METADATA_HINT_BY_SOURCE[source]),
            }
            return
        formatted = _format_metadata_by_source(source, obj, canonical)
        formatted["_input"] = ident
        results[slot] = formatted

    for i, ident in enumerate(identifiers):
        source = manual.resolve_metadata_source(ident)
        if source in ("arxiv", "biorxiv"):
            singleton_tasks.append(asyncio.create_task(_singleton_one(i, ident)))
        elif source == "openalex":
            openalex_indices.append((i, ident))
        else:
            results[i] = {"_input": ident, **_unknown_identifier_error(ident)}

    async def _openalex_batch() -> None:
        if not openalex_indices:
            return
        batch = await openalex.get_works_batch(
            [d for _, d in openalex_indices], force_refresh=force_refresh
        )
        for slot, ident in openalex_indices:
            canonical = openalex.canonical_doi(ident)
            work = batch.get(canonical)
            if work is None or "error" in work:
                err = work or {"error": f"No work found for DOI: {ident}"}
                results[slot] = {
                    "_input": ident,
                    **_enrich_error(dict(err), _OPENALEX_METADATA_HINT),
                }
                continue
            formatted = _format_openalex_metadata(work, canonical)
            formatted["_input"] = ident
            results[slot] = formatted

    await asyncio.gather(*singleton_tasks, _openalex_batch())

    # Defensive: every slot should be filled. Anything still None is a
    # bug — surface as an error rather than crashing the caller.
    for i, r in enumerate(results):
        if r is None:
            results[i] = {
                "_input": identifiers[i],
                "error": "Internal: no result produced for this identifier.",
            }

    return {"count": n, "papers": results}


def _format_openalex_authors(work: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    """Build the OpenAlex-specific author/institution slice for one page.

    Returns ``{author_count, authors, page_institutions,
    page_institution_count}``; the caller wraps it with the shared envelope
    (_source, _canonical_id, page, page_size, has_more). ``page_institutions``
    is the deduped roll-up across the current page only — agents needing a
    global list dedupe across pages. Mirrors the ``_format_*_metadata``
    factoring so the tool body stays thin and per-source shaping lives in one
    place.
    """
    all_authorships = work.get("authorships", [])
    page_authors: list[dict[str, Any]] = []
    page_institutions: list[str] = []
    for a in all_authorships[start:end]:
        author_info = a.get("author", {})
        inst_names = [
            inst.get("display_name")
            for inst in a.get("institutions", [])
            if inst.get("display_name")
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

    Default page_size 25 covers typical papers in one call; large-collaboration
    papers can have thousands of authors — page through with page / page_size
    (cap 25). Slicing is in-memory against the cached paper, no extra API hits.

    Returns ``{_source, author_count, page, page_size, has_more, authors,
    page_institutions, page_institution_count, ...}``:
      - arxiv: authors = [{name, affiliations?}]. ``page_institutions``
        is always [] (arXiv has no per-author institution roll-up).
      - biorxiv: authors = [{name}] plus author_corresponding /
        author_corresponding_institution on every page.
        ``page_institutions`` is always [] (bioRxiv only exposes the
        corresponding-author institution, not a per-author roll-up).
      - openalex: authors = [{name, openalex_id, position, is_corresponding,
        institutions}]. ``page_institutions`` / ``page_institution_count``
        are derived from the current page only (dedupe across pages for
        a global view). openalex_id chains into get_author.

    Errors: unknown identifier or paper not found returns ``{error, suggestion}``.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error
    if "error" in obj:
        return _enrich_error(obj, _METADATA_HINT_BY_SOURCE[source])

    start = (page - 1) * page_size
    end = start + page_size

    if source == "openalex":
        page_slice = _format_openalex_authors(obj, start, end)
    else:
        # arXiv / bioRxiv carry a flat author list with no per-author
        # institution roll-up — emit empty institution fields so the response
        # shape stays symmetric with the OpenAlex branch and paginating agents
        # don't have to feature-detect.
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
        # bioRxiv additionally exposes the corresponding-author fields.
        result["author_corresponding"] = obj.get("author_corresponding")
        result["author_corresponding_institution"] = obj.get("author_corresponding_institution")
    return result


@mcp.tool
async def get_paper_abstract(
    identifier: PAPER_ID,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Get a paper's abstract as plain text, dispatched by identifier shape.

    Returns ``{_source, title, abstract}``. OpenAlex abstracts are
    reconstructed from an inverted index — good enough for an LLM but not
    byte-identical to the publisher's original.

    Errors: unknown identifier or paper not found returns ``{error, suggestion}``.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error
    if "error" in obj:
        return _enrich_error(obj, _METADATA_HINT_BY_SOURCE[source])

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

    Returns ``{_source, bibtex}``. Entry type per source:
      - arxiv: @article if the paper has journal_ref, else @misc with
        eprint / archivePrefix / primaryClass.
      - biorxiv: @article when published_doi is present, else @misc with
        the preprint DOI and server.
      - openalex: inferred from the work type (@article, @inproceedings,
        @misc for preprints, @phdthesis, etc.).

    Errors: unknown identifier or paper not found returns ``{error, suggestion}``.
    """
    source, canonical_id, obj = await _fetch_source(identifier, force_refresh=force_refresh)
    if source is None:
        return obj  # unknown-identifier error
    if "error" in obj:
        return _enrich_error(obj, _METADATA_HINT_BY_SOURCE[source])

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

    Returns ``{name, openalex_id, orcid, works_count, cited_by_count,
    h_index, i10_index, current_institutions, top_topics, affiliations}``.
    ``top_topics`` is capped at 5; ``affiliations`` is the full history
    (each entry: institution, country_code, sorted years).

    ``force_refresh=True`` drops the cached profile and re-fetches — use it
    when the drifting stats (h_index, cited_by_count, works_count) may be
    stale, the same way the unified paper tools refresh works.

    Errors: not found / bad ID → ``{error, suggestion}`` pointing at
    get_paper_authors (for OpenAlex IDs) or ORCID URLs.
    """
    author = await openalex.get_author(author_id, force_refresh=force_refresh)
    if "error" in author:
        return _enrich_error(
            author, "Use an OpenAlex author ID (from get_paper_authors) or an ORCID URL."
        )

    stats = author.get("summary_stats") or {}
    current_institutions = [
        inst.get("display_name")
        for inst in (author.get("last_known_institutions") or [])
        if inst.get("display_name")
    ]
    top_topics = [
        {"name": t.get("display_name"), "count": t.get("count")}
        for t in (author.get("topics") or [])[:5]
    ]
    affiliations = []
    for aff in author.get("affiliations") or []:
        inst = aff.get("institution") or {}
        affiliations.append(
            {
                "institution": inst.get("display_name"),
                "country_code": inst.get("country_code"),
                "years": sorted(aff.get("years") or []),
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
