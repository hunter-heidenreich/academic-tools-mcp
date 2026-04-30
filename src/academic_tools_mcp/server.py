import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import _clients, _stats, acl_anthology, arxiv, biorxiv, cache, cache_search, config, crossref, manual, opencitations, openalex, papers, wikipedia
from .bibtex import generate_arxiv_bibtex, generate_bibtex, generate_biorxiv_bibtex


@asynccontextmanager
async def _lifespan(app: FastMCP):
    """Manage process-wide resources tied to the server's life.

    On startup: sweep ``.cache/`` for stale ``*.tmp`` files left behind
    by killed writers from previous runs. Cheap (one rglob, no I/O on
    files that don't match) and idempotent. New clients are pooled
    lazily on first use, so we don't pre-build them here.

    On shutdown: close every pooled httpx.AsyncClient so we don't leak
    sockets if the server is stopped while clients are idle.
    """
    cache.gc_orphan_tmp_files()
    try:
        yield
    finally:
        await _clients.aclose_all()


mcp = FastMCP(
    "academic-tools",
    lifespan=_lifespan,
    instructions=(
        "Academic paper research. Wraps OpenAlex, arXiv, bioRxiv/medRxiv, "
        "Crossref, OpenCitations, ACL Anthology, and Wikipedia for paper "
        "metadata, authors, abstracts, BibTeX, reference/citation graphs, "
        "and full-text section reading.\n\n"
        "Unified paper tools (get_paper_metadata / get_paper_authors / "
        "get_paper_abstract / get_paper_bibtex) take an arXiv ID or any DOI "
        "and route to the right provider; every response tags `_source` for "
        "provider-specific fields. get_paper_authors paginates (cap "
        "page_size=25) for huge collaboration lists.\n\n"
        "PDF pipeline: download_pdf → convert_paper → get_paper_sections → "
        "get_paper_section. All auto-detect the provider. For PDFs outside "
        "arXiv/bioRxiv/ACL, fetch the file yourself and hand it to "
        "import_paper. import_paper also accepts pre-converted .md/.markdown "
        "files — these skip convert_paper entirely (useful when the converter "
        "is unavailable or when you have a higher-quality manual conversion). "
        "get_paper_section pages by character offset (re-call with "
        "offset=next_offset) for long sections.\n\n"
        "References/citations use count-then-page (`_count` first, then "
        "paginate). Search tools (search_arxiv, search_crossref_by_title) "
        "return slim triage hits — chain to get_paper_metadata for the full "
        "record (free cache hit).\n\n"
        "All tools return {error, suggestion?} on failure; transient errors "
        "(5xx, 429, timeouts) include retry hints."
    ),
)

# Common parameter type for DOI
DOI = Annotated[
    str,
    Field(
        description="The DOI of the paper. "
        "Accepts full URL (https://doi.org/10.1234/example), "
        "prefixed (doi:10.1234/example), or bare (10.1234/example)."
    ),
]

AUTHOR_ID = Annotated[
    str,
    Field(
        description="OpenAlex author ID (e.g., A5023888391) or ORCID "
        "(e.g., https://orcid.org/0000-0001-6187-6610)."
    ),
]


PAPER_ID = Annotated[
    str,
    Field(
        description="Paper identifier — bare value, doi: prefix, or full URL. "
        "Auto-detects the source: arXiv IDs (2301.00001 or hep-th/9901001), "
        "bioRxiv/medRxiv DOIs (10.1101/...), ACL Anthology DOIs "
        "(10.18653/v1/...), or any other DOI. "
        "Metadata tools (get_paper_metadata / get_paper_authors / "
        "get_paper_abstract / get_paper_bibtex) require one of those shapes; "
        "the PDF pipeline tools (download_pdf / convert_paper / import_paper / "
        "get_paper_sections / get_paper_section) additionally accept freeform "
        "labels for manually imported files."
    ),
]

_SECTION_HARNESS_CAP = 200000

SECTION_OFFSET = Annotated[
    int,
    Field(
        description="Character offset within the section to start reading. "
        "Use the next_offset returned by a previous call to page through.",
        ge=0,
    ),
]

SECTION_MAX_CHARS = Annotated[
    int,
    Field(
        description="Slice size in characters (~4 chars per token). "
        f"Default 16000 (~4000 tokens). Hard cap {_SECTION_HARNESS_CAP} chars "
        "(enforced by the harness regardless of this setting).",
        ge=1,
        le=_SECTION_HARNESS_CAP,
    ),
]


def _enrich_error(result: dict[str, Any], suggestion: str) -> dict[str, Any]:
    """Add a suggestion to an error dict if one isn't already present."""
    if "error" in result and "suggestion" not in result:
        result["suggestion"] = suggestion
    return result


_INTERNAL_PATH_KEYS = ("path", "markdown_path")


def _strip_internal_paths(result: dict[str, Any]) -> dict[str, Any]:
    """Drop cache filesystem paths before returning to the agent.

    The agent should drive the pipeline by identifier; exposing on-disk
    paths tempts it to read files directly instead of using the tools.
    """
    if not isinstance(result, dict):
        return result
    return {k: v for k, v in result.items() if k not in _INTERNAL_PATH_KEYS}


async def _fetch_work(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch an OpenAlex work and return it, or propagate an error dict."""
    return await openalex.get_work(doi, force_refresh=force_refresh)


def _canonical_for_source(source: str | None, identifier: str) -> str | None:
    """Return the provider's canonical form of ``identifier``.

    Echoed back to agents as ``_canonical_id`` on every metadata
    response so callers can reuse the normalized form across subsequent
    tool calls instead of re-normalizing input each time.
    """
    if source == "arxiv":
        return arxiv._canonical_arxiv_id(identifier)
    if source == "biorxiv":
        return biorxiv._canonical_key(identifier)
    if source == "openalex":
        return openalex._canonical_doi(identifier)
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


def _arxiv_id_from_entry(paper: dict[str, Any]) -> str:
    """Extract the bare arXiv ID from an arXiv entry's id URL."""
    raw_id = paper.get("id", "")
    if "/abs/" in raw_id:
        return raw_id.split("/abs/")[-1]
    return raw_id


def _arxiv_pdf_url(paper: dict[str, Any]) -> str | None:
    """Extract the PDF link from an arXiv entry's links list."""
    for link in paper.get("links", []):
        if link.get("title") == "pdf":
            return link.get("href")
    return None


# ---------------------------------------------------------------------------
# Unified paper tools — auto-detect the provider from the identifier
# ---------------------------------------------------------------------------


FOLLOW_PUBLISHED = Annotated[
    bool,
    Field(
        description=(
            "If True and this is a bioRxiv/medRxiv preprint that has a "
            "journal version (published_doi field), automatically chain "
            "to OpenAlex and return the published record instead. "
            "Response carries _source='openalex_via_biorxiv' and a "
            "preprint_doi field so the chain stays visible. Has no "
            "effect for other identifier shapes or unpublished preprints."
        ),
    ),
]

FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "If True, drop the cached entry for this paper and re-fetch "
            "from the upstream provider. Use this when the cached record "
            "may be stale — a bioRxiv preprint that just got published, "
            "an OpenAlex citation count that should have grown, or an "
            "identifier that previously 404'd but should now resolve. "
            "Default False reads from cache (per-provider TTL applies)."
        ),
    ),
]

PDF_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "If True, drop the cached PDF for this paper and re-download "
            "from the upstream provider. Use this when the cached PDF is "
            "corrupt or the provider quietly replaced the file (e.g. a "
            "v2 arXiv upload under the same canonical key). Has no "
            "effect on identifiers that import_paper handled — manage "
            "those by re-running import_paper. Default False reads from "
            "the cached PDF if present."
        ),
    ),
]

CONVERT_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "If True, drop any cached markdown and section index for "
            "this paper so the converter subprocess re-runs. Use after "
            "replacing the source PDF (download_pdf with force_refresh, "
            "or import_paper with a new file) or after upgrading the "
            "converter. Conversion is slow (minutes) — only set this "
            "when you actually need a fresh markdown. Default False "
            "reuses the cached markdown."
        ),
    ),
]

SECTIONS_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "If True, drop the cached section index for this paper and "
            "re-parse from the (already-converted) markdown. Cheap — "
            "no subprocess, no network. Useful if you suspect the "
            "section index is stale and the checksum-based auto-refresh "
            "didn't catch it. Default False uses the cached index."
        ),
    ),
]


@mcp.tool
async def get_paper_metadata(
    identifier: PAPER_ID,
    follow_published: FOLLOW_PUBLISHED = False,
    force_refresh: FORCE_REFRESH = False,
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
      - openalex: title, doi, publication_year, publication_date, type,
        language, venue, is_oa, oa_status, oa_url.
      - openalex_via_biorxiv: identical to openalex, plus preprint_doi.
        Only produced when ``follow_published=True`` for a bioRxiv DOI
        that has a journal version.

    Errors: unknown identifier or paper not found returns ``{error, suggestion}``.
    Sibling tools (get_paper_authors / get_paper_abstract / get_paper_bibtex)
    share the same dispatch and cached upstream object.
    """
    source = manual._resolve_metadata_source(identifier)
    canonical_id = _canonical_for_source(source, identifier)

    if source == "arxiv":
        paper = await arxiv.get_paper(identifier, force_refresh=force_refresh)
        if "error" in paper:
            return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv.")
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

    if source == "biorxiv":
        paper = await biorxiv.get_paper(identifier, force_refresh=force_refresh)
        if "error" in paper:
            return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title.")
        published_doi = paper.get("published_doi")
        if follow_published and published_doi:
            # Chain to OpenAlex for the journal version. If OpenAlex
            # doesn't have it (paper too new to index, etc.) we fall
            # back to the preprint metadata rather than erroring — the
            # agent asked for "the best version", not "fail if no
            # journal record".
            work = await openalex.get_work(published_doi, force_refresh=force_refresh)
            if "error" not in work:
                primary_location = work.get("primary_location") or {}
                source_obj = primary_location.get("source") or {}
                oa = work.get("open_access") or {}
                return {
                    "_source": "openalex_via_biorxiv",
                    # _canonical_id is the journal DOI (the paper the
                    # response now describes); preprint_doi keeps the
                    # original chain visible.
                    "_canonical_id": openalex._canonical_doi(published_doi),
                    "preprint_doi": paper.get("doi"),
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
                }
            # Fall through to preprint metadata; the agent can still
            # see published_doi in the response and decide what to do.
        return {
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
            "published_doi": published_doi,
            "pdf_url": paper.get("pdf_url"),
        }

    if source == "openalex":
        work = await _fetch_work(identifier, force_refresh=force_refresh)
        if "error" in work:
            return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")
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
        }

    return _unknown_identifier_error(identifier)


AUTHORS_PAGE = Annotated[
    int,
    Field(description="Page number for the author list, starting at 1.", ge=1),
]

AUTHORS_PAGE_SIZE = Annotated[
    int,
    Field(description="Authors per page (1-25, default 25).", ge=1, le=25),
]


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
    source = manual._resolve_metadata_source(identifier)
    canonical_id = _canonical_for_source(source, identifier)
    start = (page - 1) * page_size
    end = start + page_size

    if source == "arxiv":
        paper = await arxiv.get_paper(identifier, force_refresh=force_refresh)
        if "error" in paper:
            return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv.")
        authors = paper.get("authors", [])
        total = len(authors)
        return {
            "_source": "arxiv",
            "_canonical_id": canonical_id,
            "author_count": total,
            "page": page,
            "page_size": page_size,
            "has_more": end < total,
            "authors": authors[start:end],
            # arXiv author entries don't carry institution data — emit
            # empty so the response shape matches the OpenAlex branch.
            # Agents that branch on _source still get a stable schema.
            "page_institutions": [],
            "page_institution_count": 0,
        }

    if source == "biorxiv":
        paper = await biorxiv.get_paper(identifier, force_refresh=force_refresh)
        if "error" in paper:
            return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title.")
        authors = paper.get("authors", [])
        total = len(authors)
        return {
            "_source": "biorxiv",
            "_canonical_id": canonical_id,
            "author_count": total,
            "page": page,
            "page_size": page_size,
            "has_more": end < total,
            "authors": authors[start:end],
            "author_corresponding": paper.get("author_corresponding"),
            "author_corresponding_institution": paper.get("author_corresponding_institution"),
            # bioRxiv only exposes the corresponding-author institution
            # (already returned above), not a per-author roll-up. Empty
            # here keeps the shape symmetric with arxiv / openalex.
            "page_institutions": [],
            "page_institution_count": 0,
        }

    if source == "openalex":
        work = await _fetch_work(identifier, force_refresh=force_refresh)
        if "error" in work:
            return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")
        all_authorships = work.get("authorships", [])
        total = len(all_authorships)
        page_authorships = all_authorships[start:end]
        authors: list[dict[str, Any]] = []
        page_institutions: list[str] = []
        for a in page_authorships:
            author_info = a.get("author", {})
            inst_names = [
                inst.get("display_name")
                for inst in a.get("institutions", [])
                if inst.get("display_name")
            ]
            for name in inst_names:
                if name not in page_institutions:
                    page_institutions.append(name)
            authors.append({
                "name": author_info.get("display_name"),
                "openalex_id": author_info.get("id"),
                "position": a.get("author_position"),
                "is_corresponding": a.get("is_corresponding"),
                "institutions": inst_names,
            })
        return {
            "_source": "openalex",
            "_canonical_id": canonical_id,
            "author_count": total,
            "page": page,
            "page_size": page_size,
            "has_more": end < total,
            "authors": authors,
            "page_institution_count": len(page_institutions),
            "page_institutions": page_institutions,
        }

    return _unknown_identifier_error(identifier)


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
    source = manual._resolve_metadata_source(identifier)
    canonical_id = _canonical_for_source(source, identifier)

    if source == "arxiv":
        paper = await arxiv.get_paper(identifier, force_refresh=force_refresh)
        if "error" in paper:
            return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv.")
        return {
            "_source": "arxiv",
            "_canonical_id": canonical_id,
            "title": paper.get("title"),
            "abstract": paper.get("summary"),
        }

    if source == "biorxiv":
        paper = await biorxiv.get_paper(identifier, force_refresh=force_refresh)
        if "error" in paper:
            return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title.")
        return {
            "_source": "biorxiv",
            "_canonical_id": canonical_id,
            "title": paper.get("title"),
            "abstract": paper.get("abstract"),
        }

    if source == "openalex":
        work = await _fetch_work(identifier, force_refresh=force_refresh)
        if "error" in work:
            return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")
        return {
            "_source": "openalex",
            "_canonical_id": canonical_id,
            "title": work.get("title"),
            "abstract": openalex.reconstruct_abstract(work.get("abstract_inverted_index")) or None,
        }

    return _unknown_identifier_error(identifier)


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
    source = manual._resolve_metadata_source(identifier)
    canonical_id = _canonical_for_source(source, identifier)

    if source == "arxiv":
        paper = await arxiv.get_paper(identifier, force_refresh=force_refresh)
        if "error" in paper:
            return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv.")
        return {
            "_source": "arxiv",
            "_canonical_id": canonical_id,
            "bibtex": generate_arxiv_bibtex(paper),
        }

    if source == "biorxiv":
        paper = await biorxiv.get_paper(identifier, force_refresh=force_refresh)
        if "error" in paper:
            return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title.")
        return {
            "_source": "biorxiv",
            "_canonical_id": canonical_id,
            "bibtex": generate_biorxiv_bibtex(paper),
        }

    if source == "openalex":
        work = await _fetch_work(identifier, force_refresh=force_refresh)
        if "error" in work:
            return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")
        return {
            "_source": "openalex",
            "_canonical_id": canonical_id,
            "bibtex": generate_bibtex(work),
        }

    return _unknown_identifier_error(identifier)


@mcp.tool
async def get_author(author_id: AUTHOR_ID) -> dict[str, Any]:
    """Fetch an author's OpenAlex profile (chain from get_paper_authors).

    Returns ``{name, openalex_id, orcid, works_count, cited_by_count,
    h_index, i10_index, current_institutions, top_topics, affiliations}``.
    ``top_topics`` is capped at 5; ``affiliations`` is the full history
    (each entry: institution, country_code, sorted years).

    Errors: not found / bad ID → ``{error, suggestion}`` pointing at
    get_paper_authors (for OpenAlex IDs) or ORCID URLs.
    """
    author = await openalex.get_author(author_id)
    if "error" in author:
        return _enrich_error(author, "Use an OpenAlex author ID (from get_paper_authors) or an ORCID URL.")

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
        affiliations.append({
            "institution": inst.get("display_name"),
            "country_code": inst.get("country_code"),
            "years": sorted(aff.get("years") or []),
        })

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


# ---------------------------------------------------------------------------
# arXiv search
# ---------------------------------------------------------------------------


def _first_author_name(paper: dict[str, Any]) -> str | None:
    authors = paper.get("authors") or []
    if not authors:
        return None
    return authors[0].get("name")


def _published_year(paper: dict[str, Any]) -> int | None:
    published = paper.get("published") or ""
    if len(published) >= 4 and published[:4].isdigit():
        return int(published[:4])
    return None


@mcp.tool
async def search_arxiv(
    query: Annotated[
        str,
        Field(
            description="arXiv search query. Supports field prefixes: "
            "ti: (title), au: (author), abs: (abstract), cat: (category). "
            "Boolean operators: AND, OR, ANDNOT. "
            "Example: 'ti:attention AND au:vaswani'"
        ),
    ],
    max_results: Annotated[
        int,
        Field(description="Maximum results to return (1-50).", ge=1, le=50),
    ] = 10,
) -> dict[str, Any]:
    """Search arXiv papers. Returns a slim triage list.

    Each hit carries ``{arxiv_id, title, first_author, author_count,
    published_year}`` — enough to recognize the paper without the full
    author list (which can balloon to tens of KB on HEP/biology
    consortium papers). ``author_count`` lets the agent decide whether
    to call get_paper_authors directly or paginate. Call
    get_paper_metadata(arxiv_id) for the full record (free cache hit —
    each search entry is opportunistically cached).

    Returns ``{total_results, results: [...]}`` — same shape as
    search_crossref_by_title so an agent can branch on the source
    without learning per-tool field names.
    """
    result = await arxiv.search_papers(query, max_results=max_results)
    if "error" in result:
        return _enrich_error(
            result,
            "Refine the query or retry if arXiv is temporarily unavailable.",
        )

    return {
        "total_results": result["total_results"],
        "results": [
            {
                "arxiv_id": _arxiv_id_from_entry(p),
                "title": p.get("title"),
                "first_author": _first_author_name(p),
                "author_count": len(p.get("authors") or []),
                "published_year": _published_year(p),
            }
            for p in result.get("entries", [])
        ],
    }


# ---------------------------------------------------------------------------
# Unified PDF pipeline tools
# ---------------------------------------------------------------------------


async def _download_pdf_by_provider(
    identifier: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Dispatch PDF download to the correct provider based on identifier type."""
    target = manual._resolve_target(identifier)
    ns = target["namespace"]

    if ns == "arxiv":
        return await arxiv.download_pdf(identifier, force_refresh=force_refresh)
    elif ns == "acl_anthology":
        return await acl_anthology.download_pdf(identifier, force_refresh=force_refresh)
    elif ns == "biorxiv":
        return await biorxiv.download_pdf(identifier, force_refresh=force_refresh)
    else:
        return {
            "error": (
                f"Cannot auto-download PDF for identifier: {identifier!r}. "
                "Direct download is only supported for arXiv IDs, "
                "bioRxiv/medRxiv DOIs (10.1101/...), and ACL Anthology DOIs "
                "(10.18653/v1/...)."
            ),
            "suggestion": (
                "Obtain the PDF yourself (publisher site, institutional access, "
                "browser, curl, etc.), then call import_paper(file_path, identifier) "
                "with the SAME identifier — it will be cached in the correct "
                "namespace so convert_paper → get_paper_sections → get_paper_section "
                "find it. import_paper also accepts pre-converted .md/.markdown "
                "files, which skip the convert_paper step entirely."
            ),
        }


@mcp.tool
async def download_pdf(
    identifier: PAPER_ID,
    force_refresh: PDF_FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Download and cache the PDF for a paper, auto-detecting the source.

    Direct download is only supported for three providers:
      - arXiv IDs (e.g. 2301.00001)
      - bioRxiv/medRxiv DOIs (10.1101/...)
      - ACL Anthology DOIs (10.18653/v1/...)

    Any other identifier (generic publisher DOI, freeform label, etc.)
    returns an error — this tool will NOT attempt to fetch arbitrary PDFs.
    For those papers, obtain the file yourself (publisher site, institutional
    access, browser, curl) and pass it to import_paper(file_path, identifier);
    using the same identifier deduplicates with the rest of the pipeline.

    Skips download if already cached unless ``force_refresh=True``. Note
    that re-downloading the PDF does NOT invalidate any already-converted
    markdown — pass ``force_refresh=True`` to convert_paper too if you
    want the next conversion to pick up the new file.

    Next step: convert_paper → get_paper_sections → get_paper_section.
    """
    return _strip_internal_paths(
        await _download_pdf_by_provider(identifier, force_refresh=force_refresh)
    )


@mcp.tool
async def convert_paper(
    identifier: PAPER_ID,
    force_refresh: CONVERT_FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Convert a downloaded PDF to markdown and parse into sections.

    Step 2 of the PDF pipeline (download_pdf → convert_paper →
    get_paper_sections → get_paper_section). Slow: up to 10 minutes per
    paper (hard timeout). Skips the subprocess if the markdown is already
    cached — re-parses from the cached markdown if the sections index
    is missing or stale. ``force_refresh=True`` drops both the cached
    markdown and the section index so the converter re-runs.

    Returns ``{sections, cached}`` on success. ``cached`` is true when the
    expensive conversion was skipped (re-parses also count as cached).
    Each section entry has ``{index, title, h3s, approx_tokens}``.

    Errors: ``{error, retryable, pdf_size_mb?, suggestion}``.
      - PDF not cached → suggestion points at download_pdf / import_paper.
      - Server already running another conversion →
        ``{busy: True, retryable: True, in_progress: {...}}``. Only one
        conversion runs at a time; retry shortly.
      - Conversion failure (subprocess error, timeout, no output) →
        non-retryable; agent should try a different version or a
        pre-converted markdown via import_paper.
    """
    target = manual._resolve_target(identifier)
    pdf = target["pdf_path"]

    if not pdf.exists():
        return {
            "error": f"PDF not cached for: {identifier}. "
            "Pipeline: download_pdf → convert_paper → get_paper_sections → get_paper_section. "
            "For PDFs outside arXiv/bioRxiv/ACL, fetch the file yourself and "
            "hand it to import_paper (accepts .pdf or .md/.markdown)."
        }

    result = await papers.convert_pdf(
        pdf,
        target["namespace"],
        target["canonical"],
        force_refresh=force_refresh,
    )
    if "error" in result:
        if result.get("busy"):
            return _enrich_error(
                result,
                "Another PDF is being converted right now. Wait and retry; "
                "in the meantime you can still read sections of papers that "
                "are already converted, or work on non-PDF tools.",
            )
        return _enrich_error(
            result,
            "Conversion failed permanently — do not retry. "
            "The PDF may be too large, corrupted, or in an unsupported format. "
            "Try importing a different version or pre-converted markdown via import_paper.",
        )
    return _strip_internal_paths(result)


@mcp.tool
async def get_paper_sections(
    identifier: PAPER_ID,
    force_refresh: SECTIONS_FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Get the section index for a converted paper.

    Step 3 of the PDF pipeline. Cheap to call (no network, no conversion).
    Auto re-parses if the cached markdown's checksum changed;
    ``force_refresh=True`` drops the section index unconditionally so
    the next read re-parses the markdown.

    Returns ``{total_sections, total_approx_tokens, sections}`` where each
    section entry has ``{index, title, preview, approx_tokens}``.

    Errors: not yet converted → guidance to run convert_paper.
    Next step: get_paper_section(identifier, index_or_title).
    """
    target = manual._resolve_target(identifier)
    namespace = target["namespace"]
    canonical = target["canonical"]

    md_path = papers._markdown_path(namespace, canonical)
    if not md_path.exists():
        return {
            "error": f"Paper not converted yet for: {identifier}. "
            "Pipeline: download_pdf → convert_paper → get_paper_sections → get_paper_section."
        }

    # Check cache with checksum validation. Lock serialises concurrent
    # readers of the same paper so they don't both re-parse and race
    # to write the sections cache.
    async with papers._sections_lock(namespace, canonical):
        if force_refresh:
            cache.invalidate(namespace, "sections", papers._sections_key(canonical))
        cached = cache.get(namespace, "sections", papers._sections_key(canonical))
        if cached is not None:
            stored_checksum = cached.get("markdown_checksum", None)
            current_checksum = papers._markdown_checksum(md_path)
            if stored_checksum is not None and stored_checksum == current_checksum:
                # Cache valid — return stored sections
                sections_data = cached
            else:
                # Missing or mismatched checksum — re-parse and update cache
                markdown = md_path.read_text()
                sections = papers.parse_sections(markdown)
                sections_data = {
                    "sections": sections,
                    "markdown_checksum": current_checksum,
                }
                cache.put(namespace, "sections", papers._sections_key(canonical), sections_data)
        else:
            # No cache — parse and create
            markdown = md_path.read_text()
            sections = papers.parse_sections(markdown)
            sections_data = {
                "sections": sections,
                "markdown_checksum": papers._markdown_checksum(md_path),
            }
            cache.put(namespace, "sections", papers._sections_key(canonical), sections_data)

    sections_list = sections_data.get("sections", [])
    return {
        "total_sections": len(sections_list),
        "total_approx_tokens": sum(s.get("approx_tokens", 0) for s in sections_list),
        "sections": sections_list,
    }


@mcp.tool(meta={"anthropic/maxResultSizeChars": _SECTION_HARNESS_CAP})
async def get_paper_section(
    identifier: PAPER_ID,
    section: Annotated[
        str,
        Field(
            description="Integer index (e.g. '0') or case-insensitive title "
            "substring (e.g. 'Introduction'). "
            "Call get_paper_sections to see the available sections."
        ),
    ],
    offset: SECTION_OFFSET = 0,
    max_chars: SECTION_MAX_CHARS = 16000,
) -> dict[str, Any]:
    """Read a slice of a section's body. Final step of the PDF pipeline.

    Returns: ``{index, title, content, offset, chars_returned, total_chars,
    approx_tokens, has_more, next_offset}``. ``total_chars`` and
    ``approx_tokens`` describe the full section, not the slice. When
    ``has_more`` is true, call again with ``offset=next_offset`` to continue.

    Errors: not yet converted → guidance to run convert_paper. Unknown or
    ambiguous section title → error listing the available titles.
    """
    target = manual._resolve_target(identifier)
    md_path = papers._markdown_path(target["namespace"], target["canonical"])

    if not md_path.exists():
        return {
            "error": f"Paper not converted yet for: {identifier}. "
            "Pipeline: download_pdf → convert_paper → get_paper_sections → get_paper_section."
        }

    markdown = md_path.read_text()

    try:
        section_key: int | str = int(section)
    except ValueError:
        section_key = section

    return papers.get_section_content(markdown, section_key, offset=offset, max_chars=max_chars)


# ---------------------------------------------------------------------------
# Manual PDF import tools
# ---------------------------------------------------------------------------


_MARKDOWN_EXTS = {".md", ".markdown"}


@mcp.tool
async def import_paper(
    file_path: Annotated[
        str,
        Field(
            description="Path to a local .pdf or .md/.markdown file. "
            "Absolute or ~/-prefixed paths recommended. "
            "PDF is routed through the conversion pipeline; markdown is "
            "imported directly and skips conversion."
        ),
    ],
    identifier: PAPER_ID,
) -> dict[str, Any]:
    """Import a local PDF or pre-converted markdown into the cache.

    For papers outside arXiv/bioRxiv/ACL: fetch the file yourself, then
    call this with the paper's DOI / arXiv ID as the identifier. The same
    identifier deduplicates with the rest of the pipeline so a later
    download_pdf or convert_paper finds it without re-fetching. Unrecognised
    identifiers still work — the file lands in a ``manual`` namespace and
    the rest of the pipeline keys off the same identifier.

    File type is detected by extension:
      - .pdf → validated via %PDF- header, then cached for convert_paper →
        get_paper_sections → get_paper_section.
      - .md / .markdown → read as UTF-8, cached, and parsed into sections
        immediately; skip convert_paper.

    Returns ``{identifier, namespace, size_bytes, cached}`` for PDFs, or
    ``{identifier, namespace, section_count, cached}`` for markdown — call
    get_paper_sections for the full section index with previews.

    Errors: file not found, not a valid PDF, non-UTF-8 markdown, or
    unsupported extension → ``{error}``.
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        return _strip_internal_paths(manual.import_local_pdf(file_path, identifier))
    if ext in _MARKDOWN_EXTS:
        result = _strip_internal_paths(manual.import_markdown(file_path, identifier))
        if "sections" in result:
            sections = result.pop("sections")
            result["section_count"] = len(sections)
        return result
    return {
        "error": (
            f"Unsupported file extension {ext!r}. "
            "Expected .pdf (for the PDF pipeline) or .md/.markdown (for "
            "pre-converted text)."
        ),
    }


# ---------------------------------------------------------------------------
# Crossref tools
# ---------------------------------------------------------------------------

PAGE_SIZE = Annotated[
    int,
    Field(description="Number of results per page (1-50).", ge=1, le=50),
]

PAGE = Annotated[
    int,
    Field(description="Page number, starting at 1.", ge=1),
]


@mcp.tool
async def search_crossref_by_title(
    title: Annotated[
        str,
        Field(description="Paper title or bibliographic query string."),
    ],
    year: Annotated[
        int | None,
        Field(description="Publication year to filter results. Optional but recommended."),
    ] = None,
) -> dict[str, Any]:
    """Search Crossref by title (bibliographic query). Returns a slim triage list.

    Each hit carries ``{doi, title, first_author, author_count, year}`` —
    enough to recognize the paper without the full author list (which
    can balloon on HEP/biology consortium papers). ``author_count`` lets
    the agent decide whether to call get_paper_authors directly or
    paginate. Call get_paper_metadata(doi) for the full record.

    Useful for finding the published DOI when you only have a title or
    arXiv ID. Also serves as the de facto search for bioRxiv papers,
    since Crossref indexes all bioRxiv DOIs.

    Returns ``{total_results, results: [...]}``. Capped at 5 hits per
    call. Year filtering is optional but recommended; note that Crossref
    publication dates may differ from arXiv preprint dates.
    """
    response = await crossref.search_works(title, year=year, rows=5)
    if "error" in response:
        return _enrich_error(response, "Try a more specific title or use search_arxiv if it's a preprint.")
    items = response.get("items", [])

    results = []
    for item in items:
        authors = item.get("author") or []
        first_author = None
        for a in authors:
            name_parts = [p for p in (a.get("given"), a.get("family")) if p]
            if name_parts:
                first_author = " ".join(name_parts)
                break

        pub_date = item.get("published-print") or item.get("published-online") or {}
        date_parts = pub_date.get("date-parts", [[]])[0]

        results.append({
            "doi": item.get("DOI"),
            "title": (item.get("title") or [None])[0],
            "first_author": first_author,
            "author_count": len(authors),
            "year": date_parts[0] if date_parts else None,
        })

    return {"total_results": len(results), "results": results}


async def _fetch_crossref_work(doi: str) -> dict[str, Any]:
    """Fetch a work from Crossref and return it, or propagate an error dict."""
    return await crossref.get_work(doi)


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


REF_SOURCE = Annotated[
    Literal["auto", "crossref", "opencitations"],
    Field(
        description="Which reference source to page through. "
        "'auto' (default) surveys both providers in parallel and picks "
        "the one with more references — saves a turn versus calling "
        "get_paper_references_count first. "
        "'crossref' gives structured metadata (author, title, year, journal, DOI) "
        "but quality varies by publisher. "
        "'opencitations' gives DOI-to-DOI links with cross-referenced IDs "
        "(OMID, OpenAlex, PMID) and self-citation flags, aggregated from "
        "Crossref/PubMed/DataCite/OpenAIRE/JaLC — may have entries Crossref lacks. "
        "Call get_paper_references_count first only if you want to compare "
        "coverage explicitly before paginating."
    ),
]


# ---------------------------------------------------------------------------
# Reference / citation graph tools
# ---------------------------------------------------------------------------


@mcp.tool
async def get_paper_references_count(doi: DOI) -> dict[str, Any]:
    """Survey outgoing-reference coverage across Crossref and OpenCitations.

    Fires both providers in parallel via asyncio.gather. Counts often
    differ — call this first to pick the better-covered source before
    paginating with get_paper_references.

    Returns ``{doi, sources: {crossref: {count: N} | {error, suggestion?},
    opencitations: {count: M} | {error, suggestion?}}}``. Partial-failure
    tolerant: if one source errors the other's count is still reported.
    """
    cr_task = crossref.get_work(doi)
    oc_task = opencitations.get_references(doi)
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


def _crossref_refs_page(work: dict[str, Any], doi: str, page: int, page_size: int) -> dict[str, Any]:
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


def _opencitations_refs_page(data: dict[str, Any], doi: str, page: int, page_size: int) -> dict[str, Any]:
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
) -> dict[str, Any]:
    """Page through outgoing references (bibliography) from the chosen source.

    Default ``source="auto"`` fires Crossref and OpenCitations in parallel,
    picks whichever has more references, and pages from that one — saves
    a turn vs. calling get_paper_references_count first. The chosen
    source is reported in ``_source`` so subsequent pagination calls can
    pin it explicitly. If one provider errors, the other wins
    automatically; if both error, the response carries both errors.

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
        work = await _fetch_crossref_work(doi)
        if "error" in work:
            return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")
        return _crossref_refs_page(work, doi, page, page_size)

    if source == "opencitations":
        data = await opencitations.get_references(doi)
        if "error" in data:
            return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
        return _opencitations_refs_page(data, doi, page, page_size)

    # source == "auto": survey both, pick the bigger. The fetches are
    # cached so a follow-up page=2 call with the same source doesn't
    # re-survey or re-fetch.
    cr_task = _fetch_crossref_work(doi)
    oc_task = opencitations.get_references(doi)
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
async def get_paper_citations_count(doi: DOI) -> dict[str, Any]:
    """Count incoming citations (papers that cite this work) via OpenCitations.

    Returns ``{doi, count}`` on success or ``{error, suggestion}`` on failure.
    OpenCitations is the only source for incoming citations (no Crossref
    equivalent), so unlike get_paper_references_count there is no source
    survey — call this then page with get_paper_citations.
    """
    data = await opencitations.get_citations(doi)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
    return {"doi": doi, "count": data["count"]}


CITATION_SOURCE = Annotated[
    Literal["auto", "opencitations"],
    Field(
        description="Which citation source to page through. "
        "OpenCitations is currently the only provider for incoming "
        "citations (no Crossref equivalent), so 'auto' and "
        "'opencitations' behave identically. The parameter is "
        "reserved so a future second source can be added without a "
        "breaking change — pin source='opencitations' explicitly if "
        "your code path must always use it."
    ),
]


@mcp.tool
async def get_paper_citations(
    doi: DOI,
    source: CITATION_SOURCE = "auto",
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
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

    Errors: bad DOI / upstream failure → ``{error, suggestion}`` with
    retry hints for transient failures.
    """
    # source is reserved for forward compatibility; both values dispatch
    # to OpenCitations today. Keeping the parameter in the signature now
    # means agent code path "page through citations" can pin source=
    # "opencitations" without breaking when a second source ships.
    data = await opencitations.get_citations(doi)
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


# ---------------------------------------------------------------------------
# Local-cache full-text search
# ---------------------------------------------------------------------------


_CACHE_SEARCH_NAMESPACE = Annotated[
    str | None,
    Field(
        description=(
            "Optional cache namespace to restrict the search to "
            "(arxiv, biorxiv, acl_anthology, manual). Default None "
            "searches every cached namespace."
        ),
    ),
]

_CACHE_SEARCH_TOP_K = Annotated[
    int,
    Field(
        description=(
            "Maximum number of hits to return (1-50, default 10). "
            "Hits are ranked by BM25; ties go to the first-seen file "
            "in alphabetical order."
        ),
        ge=1,
        le=50,
    ),
]


@mcp.tool
async def search_cached_papers(
    query: Annotated[
        str,
        Field(
            description=(
                "Free-text query against the converted-markdown cache. "
                "Tokenised on words; stopwords dropped. Phrasal queries "
                "work as a bag-of-words (no positional matching), so "
                "'variational dropout' ranks docs by how often each "
                "term appears, not strictly the bigram."
            ),
        ),
    ],
    top_k: _CACHE_SEARCH_TOP_K = 10,
    namespace: _CACHE_SEARCH_NAMESPACE = None,
) -> dict[str, Any]:
    """BM25 full-text search across every paper you've already converted.

    Walks ``.cache/<namespace>/markdown/*.md`` for every namespace (or
    just the one passed via ``namespace=``) and ranks each document
    against the query using standard BM25. Useful for:

      - Recovering a paper by content when you don't remember the
        identifier ("which paper mentioned variational dropout?")
      - Finding all cached papers that discuss a concept
      - Triage on a manual-import collection where the identifier is a
        freeform label and search_arxiv / search_crossref_by_title
        can't help

    Returns ``{query, result_count, results: [{namespace, canonical_id,
    score, title, snippet, section, char_count}, ...]}``. ``snippet``
    is a ~200-char window centred on the most-distinct cluster of
    matching terms; ``section`` is the H2 the snippet falls under so
    you can chain into get_paper_section(canonical_id, section).

    Hits with score 0 (no query term appears) are dropped — empty
    results means the cache contains no relevant paper, not that the
    search failed. Searches the cache live on every call; for a
    personal-MCP corpus this runs in well under 100ms.

    Limits: pure keyword match (BM25 doesn't know synonyms — "self-
    attention" won't surface a paper that only says "scaled dot-
    product attention"). Only converted papers are searchable; PDFs
    that haven't been through convert_paper / import_paper are not in
    the index.
    """
    # Wrap the synchronous BM25 pass in to_thread so it doesn't pin the
    # event loop on a large corpus. Even at hundreds of papers this is
    # tens of milliseconds, but agents may run searches concurrently
    # with HTTP fetches and we shouldn't starve those.
    results = await asyncio.to_thread(
        cache_search.search, query, top_k=top_k, namespace=namespace
    )
    return {
        "query": query,
        "result_count": len(results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Wikipedia tools
# ---------------------------------------------------------------------------


@mcp.tool
async def search_wikipedia(
    query: Annotated[
        str,
        Field(description="Search term or phrase to find Wikipedia articles for."),
    ],
    limit: Annotated[
        int,
        Field(description="Maximum results to return (1-10).", ge=1, le=10),
    ] = 5,
) -> dict[str, Any]:
    """Search Wikipedia for articles matching a query (titles + URLs only).

    Returns ``{query, result_count, results: [{title, url}, ...]}``. Capped
    at 10 hits. Use the title from a hit with get_wikipedia_summary to
    fetch the article extract.

    Errors: Wikipedia outage / rate limit → ``{error, suggestion}`` with a
    retry hint.
    """
    response = await wikipedia.search(query, limit=limit)
    if "error" in response:
        return _enrich_error(response, "Wikipedia is temporarily unavailable; retry in a few seconds.")
    results = response.get("results", [])
    return {"query": query, "result_count": len(results), "results": results}


@mcp.tool
async def get_wikipedia_summary(
    title: Annotated[
        str,
        Field(
            description="Wikipedia article title (e.g. 'Cytochrome P450'). "
            "Spaces and underscores both work."
        ),
    ],
) -> dict[str, Any]:
    """Fetch the structured summary (extract) of a Wikipedia article.

    Returns ``{title, description, extract, url, type, pageid}``. ``type``
    is ``"standard"`` for normal articles or ``"disambiguation"`` for
    disambiguation pages (where ``extract`` is typically a list of
    candidate meanings). Cached per article — one network hit ever.

    Errors: page not found / Wikipedia outage → ``{error, suggestion}``.
    Use search_wikipedia first if you don't already know the canonical
    title.
    """
    result = await wikipedia.get_summary(title)
    if "error" in result:
        return _enrich_error(
            result,
            "Try search_wikipedia to find the correct title, or retry if "
            "Wikipedia is temporarily unavailable.",
        )
    return result




# ---------------------------------------------------------------------------
# Operator-only debug tools (gated behind ENABLE_DEBUG_TOOLS env var)
# ---------------------------------------------------------------------------
#
# These are NOT registered when the env var is absent so agents never
# see them in normal operation. The env var flips them on for an
# operator who wants to inspect cumulative cache/HTTP counters from
# within Claude Code itself, without dropping into a Python REPL.

_DEBUG_TOOLS_ENABLED = (config.get("ENABLE_DEBUG_TOOLS") or "").lower() in (
    "1", "true", "yes", "on"
)

if _DEBUG_TOOLS_ENABLED:
    @mcp.tool
    async def get_server_stats() -> dict[str, Any]:
        """Operator-only: snapshot cumulative cache + HTTP counters.

        Only registered when ``ENABLE_DEBUG_TOOLS=1`` in the environment;
        agents never see it in normal operation. Returns the per-provider
        counters tracked by ``_stats.snapshot()``: cache_hits / cache_misses
        / negative_hits, http_calls / http_retries, backpressure_refusals,
        and live in_flight counts. Cumulative since process start.

        Use this when something feels slow or rate-limit-pressured to see
        which provider is hitting the network vs. serving from cache.
        """
        return _stats.snapshot()


if __name__ == "__main__":
    mcp.run()
