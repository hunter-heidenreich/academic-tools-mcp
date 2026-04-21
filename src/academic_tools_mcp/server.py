import asyncio
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import acl_anthology, arxiv, biorxiv, cache, crossref, manual, opencitations, openalex, papers, wikipedia
from .bibtex import generate_arxiv_bibtex, generate_bibtex, generate_biorxiv_bibtex

mcp = FastMCP(
    "academic-tools",
    instructions=(
        "Academic paper research tools. Wraps OpenAlex, arXiv, bioRxiv/medRxiv, "
        "Crossref, OpenCitations, ACL Anthology, and Wikipedia APIs. "
        "Use these tools to look up paper metadata, authors, abstracts, BibTeX citations, "
        "citation/reference graphs, and to download and read full paper content section-by-section. "
        "Unified paper tools (get_paper_metadata / get_paper_authors / get_paper_abstract / "
        "get_paper_bibtex) accept arXiv IDs or any DOI and auto-route to arXiv, bioRxiv, or "
        "OpenAlex — each response carries `_source` so you can interpret provider-specific "
        "fields. "
        "PDF pipeline: download_pdf → convert_paper → get_paper_sections → get_paper_section, "
        "auto-detects the provider. For PDFs not on arXiv/bioRxiv/ACL, fetch the file "
        "yourself and hand it to import_pdf (or import_markdown for pre-converted text). "
        "Reference/citation tools use a count-then-page pattern to avoid token blowouts."
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
        description="Paper identifier. Auto-detects the source: "
        "arXiv ID (2301.00001 or hep-th/9901001), "
        "bioRxiv/medRxiv DOI (10.1101/...), "
        "ACL Anthology DOI (10.18653/v1/...), "
        "any other DOI, or a freeform label. "
        "Accepts bare values, doi: prefix, or full URLs."
    ),
]

# Default truncation limit for section content (~4000 tokens)
_DEFAULT_MAX_CHARS = 16000

MAX_CHARS = Annotated[
    int | None,
    Field(
        description="Maximum characters of section content to return. "
        "Defaults to 16000 chars (~4000 tokens). "
        "Set to 0 for full content (no truncation). "
        "When truncated, the response includes remaining_chars and a hint.",
    ),
]


def _resolve_max_chars(max_chars: int | None) -> int | None:
    """Normalize the max_chars parameter: None uses default, 0 means no limit."""
    if max_chars is None:
        return _DEFAULT_MAX_CHARS
    if max_chars == 0:
        return None
    return max_chars


def _enrich_error(result: dict[str, Any], suggestion: str) -> dict[str, Any]:
    """Add a suggestion to an error dict if one isn't already present."""
    if "error" in result and "suggestion" not in result:
        result["suggestion"] = suggestion
    return result


async def _fetch_work(doi: str) -> dict[str, Any]:
    """Fetch an OpenAlex work and return it, or propagate an error dict."""
    return await openalex.get_work(doi)


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


@mcp.tool
async def get_paper_metadata(identifier: PAPER_ID) -> dict[str, Any]:
    """Get core metadata for a paper, auto-detecting the source from the identifier.

    Every response carries `_source` = "arxiv" | "biorxiv" | "openalex"
    alongside source-native fields:
      - arxiv: arxiv_id, title, published, updated, primary_category,
        categories, pdf_url, doi, journal_ref, comment.
      - biorxiv: doi, title, date, version, type, category, license, server,
        published_doi, pdf_url. Chain `published_doi` into OpenAlex for
        the journal version.
      - openalex: title, doi, publication_year, publication_date, type,
        language, venue, is_oa, oa_status, oa_url.

    Related: get_paper_authors / get_paper_abstract / get_paper_bibtex use
    the same dispatch.
    """
    source = manual._resolve_metadata_source(identifier)

    if source == "arxiv":
        paper = await arxiv.get_paper(identifier)
        if "error" in paper:
            return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv.")
        return {
            "_source": "arxiv",
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
        paper = await biorxiv.get_paper(identifier)
        if "error" in paper:
            return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title.")
        return {
            "_source": "biorxiv",
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

    if source == "openalex":
        work = await _fetch_work(identifier)
        if "error" in work:
            return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")
        primary_location = work.get("primary_location") or {}
        source_obj = primary_location.get("source") or {}
        oa = work.get("open_access") or {}
        return {
            "_source": "openalex",
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


@mcp.tool
async def get_paper_authors(identifier: PAPER_ID) -> dict[str, Any]:
    """Get the author list for a paper, auto-detecting the source.

    Response shape per `_source`:
      - arxiv: author_count, authors (name + optional affiliations).
      - biorxiv: author_count, authors, author_corresponding,
        author_corresponding_institution.
      - openalex: author_count, authors (each with openalex_id, position,
        is_corresponding, institutions), institution_count, all_institutions.
        OpenAlex author entries carry openalex_id for chaining into
        get_author_profile / get_author_affiliations.
    """
    source = manual._resolve_metadata_source(identifier)

    if source == "arxiv":
        paper = await arxiv.get_paper(identifier)
        if "error" in paper:
            return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv.")
        authors = paper.get("authors", [])
        return {"_source": "arxiv", "author_count": len(authors), "authors": authors}

    if source == "biorxiv":
        paper = await biorxiv.get_paper(identifier)
        if "error" in paper:
            return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title.")
        authors = paper.get("authors", [])
        return {
            "_source": "biorxiv",
            "author_count": len(authors),
            "authors": authors,
            "author_corresponding": paper.get("author_corresponding"),
            "author_corresponding_institution": paper.get("author_corresponding_institution"),
        }

    if source == "openalex":
        work = await _fetch_work(identifier)
        if "error" in work:
            return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")
        authors: list[dict[str, Any]] = []
        all_institutions: list[str] = []
        for a in work.get("authorships", []):
            author_info = a.get("author", {})
            inst_names = [
                inst.get("display_name")
                for inst in a.get("institutions", [])
                if inst.get("display_name")
            ]
            for name in inst_names:
                if name not in all_institutions:
                    all_institutions.append(name)
            authors.append({
                "name": author_info.get("display_name"),
                "openalex_id": author_info.get("id"),
                "position": a.get("author_position"),
                "is_corresponding": a.get("is_corresponding"),
                "institutions": inst_names,
            })
        return {
            "_source": "openalex",
            "author_count": len(authors),
            "authors": authors,
            "institution_count": len(all_institutions),
            "all_institutions": all_institutions,
        }

    return _unknown_identifier_error(identifier)


@mcp.tool
async def get_paper_abstract(identifier: PAPER_ID) -> dict[str, Any]:
    """Get the abstract of a paper as plain text, auto-detecting the source."""
    source = manual._resolve_metadata_source(identifier)

    if source == "arxiv":
        paper = await arxiv.get_paper(identifier)
        if "error" in paper:
            return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv.")
        return {
            "_source": "arxiv",
            "title": paper.get("title"),
            "abstract": paper.get("summary"),
        }

    if source == "biorxiv":
        paper = await biorxiv.get_paper(identifier)
        if "error" in paper:
            return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title.")
        return {
            "_source": "biorxiv",
            "title": paper.get("title"),
            "abstract": paper.get("abstract"),
        }

    if source == "openalex":
        work = await _fetch_work(identifier)
        if "error" in work:
            return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")
        return {
            "_source": "openalex",
            "title": work.get("title"),
            "abstract": openalex.reconstruct_abstract(work.get("abstract_inverted_index")) or None,
        }

    return _unknown_identifier_error(identifier)


@mcp.tool
async def get_paper_bibtex(identifier: PAPER_ID) -> dict[str, Any]:
    """Generate a BibTeX entry for a paper, auto-detecting the source.

    - arxiv: @article if the paper has journal_ref, else @misc with
      eprint / archivePrefix / primaryClass.
    - biorxiv: @article when a published_doi is present, else @misc with
      the preprint DOI and server.
    - openalex: entry type inferred from the work type (@article,
      @inproceedings, @misc for preprints, @phdthesis, etc.).
    """
    source = manual._resolve_metadata_source(identifier)

    if source == "arxiv":
        paper = await arxiv.get_paper(identifier)
        if "error" in paper:
            return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv.")
        return {"_source": "arxiv", "bibtex": generate_arxiv_bibtex(paper)}

    if source == "biorxiv":
        paper = await biorxiv.get_paper(identifier)
        if "error" in paper:
            return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title.")
        return {"_source": "biorxiv", "bibtex": generate_biorxiv_bibtex(paper)}

    if source == "openalex":
        work = await _fetch_work(identifier)
        if "error" in work:
            return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")
        return {"_source": "openalex", "bibtex": generate_bibtex(work)}

    return _unknown_identifier_error(identifier)


# Temporarily disabled — re-enable by restoring the @mcp.tool decorator.
# @mcp.tool
async def get_paper_citations_summary(doi: DOI) -> dict[str, Any]:
    """Get citation statistics for a paper (OpenAlex only, requires a DOI).

    Returns cited_by_count, referenced_works_count, and is_retracted. For
    arXiv-only preprints without a DOI this data is not available.
    """
    work = await _fetch_work(doi)
    if "error" in work:
        return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")

    return {
        "title": work.get("title"),
        "cited_by_count": work.get("cited_by_count"),
        "referenced_works_count": work.get("referenced_works_count"),
        "is_retracted": work.get("is_retracted"),
    }


# Temporarily disabled — re-enable by restoring the @mcp.tool decorator.
# @mcp.tool
async def get_paper_topics(doi: DOI) -> dict[str, Any]:
    """Get topic classifications and keywords for a paper (OpenAlex only, requires a DOI).

    For arXiv-only preprints without a DOI this data is not available.
    """
    work = await _fetch_work(doi)
    if "error" in work:
        return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")

    topics = [
        {
            "name": t.get("display_name"),
            "score": round(t.get("score", 0), 4),
            "subfield": (t.get("subfield") or {}).get("display_name"),
            "field": (t.get("field") or {}).get("display_name"),
            "domain": (t.get("domain") or {}).get("display_name"),
        }
        for t in work.get("topics", [])
    ]
    keywords = [
        {
            "keyword": k.get("display_name"),
            "score": round(k.get("score", 0), 4),
        }
        for k in work.get("keywords", [])
    ]

    return {
        "title": work.get("title"),
        "topic_count": len(topics),
        "topics": topics,
        "keyword_count": len(keywords),
        "keywords": keywords,
    }


@mcp.tool
async def get_author_profile(author_id: AUTHOR_ID) -> dict[str, Any]:
    """Get an author's profile: name, ORCID, current institutions, publication/citation counts, h-index, and top topics."""
    author = await openalex.get_author(author_id)
    if "error" in author:
        return _enrich_error(author, "Use an OpenAlex author ID (from get_paper_authors) or an ORCID URL.")

    stats = author.get("summary_stats") or {}
    last_institutions = [
        inst.get("display_name")
        for inst in (author.get("last_known_institutions") or [])
        if inst.get("display_name")
    ]
    topics = [
        {"name": t.get("display_name"), "count": t.get("count")}
        for t in (author.get("topics") or [])[:5]
    ]

    return {
        "name": author.get("display_name"),
        "openalex_id": author.get("id"),
        "orcid": author.get("orcid"),
        "works_count": author.get("works_count"),
        "cited_by_count": author.get("cited_by_count"),
        "h_index": stats.get("h_index"),
        "i10_index": stats.get("i10_index"),
        "current_institutions": last_institutions,
        "top_topics": topics,
    }


@mcp.tool
async def get_author_affiliations(author_id: AUTHOR_ID) -> dict[str, Any]:
    """Get an author's affiliation history: institutions with the years they were affiliated."""
    author = await openalex.get_author(author_id)
    if "error" in author:
        return _enrich_error(author, "Use an OpenAlex author ID (from get_paper_authors) or an ORCID URL.")

    affiliations = []
    for aff in author.get("affiliations") or []:
        inst = aff.get("institution") or {}
        years = sorted(aff.get("years") or [])
        affiliations.append({
            "institution": inst.get("display_name"),
            "country_code": inst.get("country_code"),
            "years": years,
        })

    return {
        "name": author.get("display_name"),
        "affiliations": affiliations,
    }


# ---------------------------------------------------------------------------
# arXiv search
# ---------------------------------------------------------------------------


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
    """Search arXiv papers. Returns titles, IDs, authors, and categories for matching papers.

    Use the returned arxiv_id with get_paper_metadata, get_paper_abstract,
    get_paper_bibtex, or download_pdf to access full paper content.
    """
    result = await arxiv.search_papers(query, max_results=max_results)
    if "error" in result:
        return result

    return {
        "total_results": result["total_results"],
        "papers": [
            {
                "arxiv_id": _arxiv_id_from_entry(p),
                "title": p.get("title"),
                "authors": [a.get("name") for a in p.get("authors", [])],
                "primary_category": p.get("primary_category"),
                "published": p.get("published"),
            }
            for p in result.get("entries", [])
        ],
    }


# ---------------------------------------------------------------------------
# Unified PDF pipeline tools
# ---------------------------------------------------------------------------


async def _download_pdf_by_provider(identifier: str) -> dict[str, Any]:
    """Dispatch PDF download to the correct provider based on identifier type."""
    target = manual._resolve_target(identifier)
    ns = target["namespace"]

    if ns == "arxiv":
        return await arxiv.download_pdf(identifier)
    elif ns == "acl_anthology":
        return await acl_anthology.download_pdf(identifier)
    elif ns == "biorxiv":
        return await biorxiv.download_pdf(identifier)
    else:
        return {
            "error": f"Cannot auto-download for identifier: {identifier}. "
            "Fetch the PDF yourself and hand it to import_pdf (local file) or "
            "import_markdown (pre-converted), then convert_paper → "
            "get_paper_sections → get_paper_section."
        }


@mcp.tool
async def download_pdf(identifier: PAPER_ID) -> dict[str, Any]:
    """Download and cache the PDF for a paper, auto-detecting the source.

    Supports arXiv IDs, ACL Anthology DOIs (10.18653/v1/...), and
    bioRxiv/medRxiv DOIs (10.1101/...). Skips download if already cached.
    For other sources, fetch the PDF yourself and hand it to import_pdf
    (or import_markdown for pre-converted text).

    Next step: convert_paper → get_paper_sections → get_paper_section.
    """
    return await _download_pdf_by_provider(identifier)


@mcp.tool
async def convert_paper(identifier: PAPER_ID) -> dict[str, Any]:
    """Convert a downloaded PDF to markdown, then parse into sections.

    Auto-detects the provider from the identifier and routes to the correct
    cache namespace. This is a slow operation (5-10 minutes). Returns the
    section index on completion. Skips conversion if markdown is already cached.

    The PDF must be downloaded first via download_pdf (or import_pdf for
    other sources).

    Next step: get_paper_sections → get_paper_section.
    """
    target = manual._resolve_target(identifier)
    pdf = target["pdf_path"]

    if not pdf.exists():
        return {
            "error": f"PDF not cached for: {identifier}. "
            "Pipeline: download_pdf → convert_paper → get_paper_sections → get_paper_section. "
            "For PDFs outside arXiv/bioRxiv/ACL, fetch the file yourself and "
            "hand it to import_pdf (or import_markdown for pre-converted text)."
        }

    result = await papers.convert_pdf(pdf, target["namespace"], target["canonical"])
    if "error" in result:
        return _enrich_error(
            result,
            "Conversion failed permanently — do not retry. "
            "The PDF may be too large, corrupted, or in an unsupported format. "
            "Try importing a different version or pre-converted markdown via import_markdown.",
        )
    return result


@mcp.tool
async def get_paper_sections(identifier: PAPER_ID) -> dict[str, Any]:
    """Get the section index for a converted paper.

    Auto-detects the provider from the identifier. Returns section titles
    with sub-heading previews and approximate token counts.

    The paper must be converted first via convert_paper.

    Automatically re-parses if the markdown file has changed since the last
    call (detected via SHA-256 checksum).

    Next step: get_paper_section(identifier, section_index_or_title).
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

    # Check cache with checksum validation
    cached = cache.get(namespace, "sections", papers._sections_key(canonical))
    if cached is not None:
        stored_checksum = cached.get("markdown_checksum", None)
        current_checksum = papers._markdown_checksum(md_path)
        if stored_checksum is None or stored_checksum == current_checksum:
            # Cache valid — return stored sections
            sections_data = cached
        else:
            # Checksum mismatch — re-parse and update cache
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


@mcp.tool(meta={"anthropic/maxResultSizeChars": 200000})
async def get_paper_section(
    identifier: PAPER_ID,
    section: Annotated[
        str,
        Field(
            description="Section to retrieve: an integer index (e.g. '0') "
            "or a title substring (e.g. 'Introduction', 'Methods'). "
            "Use get_paper_sections to see available sections."
        ),
    ],
    max_chars: MAX_CHARS = None,
) -> dict[str, Any]:
    """Get the markdown content of a specific section from a converted paper.

    Auto-detects the provider from the identifier. Accepts a section index
    number or a title substring (case-insensitive).
    Content is truncated by default (16000 chars). Set max_chars=0 for full content.
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

    return papers.get_section_content(markdown, section_key, max_chars=_resolve_max_chars(max_chars))


# ---------------------------------------------------------------------------
# Manual PDF import tools
# ---------------------------------------------------------------------------


@mcp.tool
async def import_pdf(
    file_path: Annotated[
        str,
        Field(
            description="Absolute path to a local PDF file "
            "(e.g. from Zotero storage)."
        ),
    ],
    identifier: PAPER_ID,
) -> dict[str, Any]:
    """Import a local PDF file into the cache for conversion and section access.

    Use this for papers you already have on disk (e.g. from Zotero, email,
    or publisher downloads). The identifier is used as the cache key — use
    the DOI when available so you can chain into Crossref/OpenAlex tools.

    Next step: convert_paper → get_paper_sections → get_paper_section.
    """
    return manual.import_local_pdf(file_path, identifier)


@mcp.tool
async def import_markdown(
    file_path: Annotated[
        str,
        Field(
            description="Absolute path to a local markdown file."
        ),
    ],
    identifier: PAPER_ID,
) -> dict[str, Any]:
    """Import a pre-converted markdown file directly into the cache.

    Skips the PDF download and conversion steps entirely — the section
    pipeline (get_paper_sections / get_paper_section) works immediately
    after this call.

    Use this when you already have markdown from any PDF-to-markdown tool.
    """
    return manual.import_markdown(file_path, identifier)


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
    """Search Crossref for papers by title (bibliographic query).

    Returns matching DOIs with titles, authors, and publication info.
    Useful for finding the published DOI when you only have a title or arXiv ID.
    Also serves as de facto search for bioRxiv papers (Crossref indexes all bioRxiv DOIs).

    Use the returned DOI with get_paper_metadata, get_paper_bibtex, or
    get_paper_references.
    """
    items = await crossref.search_works(title, year=year, rows=5)

    results = []
    for item in items:
        authors = []
        for a in item.get("author", []):
            name_parts = []
            if a.get("given"):
                name_parts.append(a["given"])
            if a.get("family"):
                name_parts.append(a["family"])
            if name_parts:
                authors.append(" ".join(name_parts))

        pub_date = item.get("published-print") or item.get("published-online") or {}
        date_parts = pub_date.get("date-parts", [[]])[0]

        results.append({
            "doi": item.get("DOI"),
            "title": (item.get("title") or [None])[0],
            "authors": authors,
            "year": date_parts[0] if date_parts else None,
            "venue": (item.get("container-title") or [None])[0],
            "type": item.get("type"),
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
    Literal["crossref", "opencitations"],
    Field(
        description="Which reference source to page through. "
        "'crossref' gives structured metadata (author, title, year, journal, DOI) "
        "but quality varies by publisher. "
        "'opencitations' gives DOI-to-DOI links with cross-referenced IDs "
        "(OMID, OpenAlex, PMID) and self-citation flags, aggregated from "
        "Crossref/PubMed/DataCite/OpenAIRE/JaLC — may have entries Crossref lacks. "
        "Call get_paper_references_count first to compare coverage."
    ),
]


# ---------------------------------------------------------------------------
# Reference / citation graph tools
# ---------------------------------------------------------------------------


@mcp.tool
async def get_paper_references_count(doi: DOI) -> dict[str, Any]:
    """Survey outgoing-reference coverage across Crossref and OpenCitations in one call.

    Queries both providers in parallel and returns a per-source count so you
    can pick the better-covered source before paginating with
    get_paper_references. On partial failure the failing source returns an
    error dict while the other source's count is still reported.

    Shape: {doi, sources: {crossref: {count: N} | {error: ...},
                           opencitations: {count: M} | {error: ...}}}
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


@mcp.tool
async def get_paper_references(
    doi: DOI,
    source: REF_SOURCE,
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
) -> dict[str, Any]:
    """Get a page of outgoing references (bibliography) from the chosen source.

    Response shape per `_source`:
      - crossref: structured metadata per entry (doi, author, title, year,
        journal, volume, first_page, unstructured). Fields present depend on
        publisher deposit quality.
      - opencitations: DOI-to-DOI links with publication dates, self-citation
        flags, and cross-referenced IDs (OMID, OpenAlex, PMID). No full
        bibliographic metadata.

    Call get_paper_references_count first to compare coverage across sources
    and see the total.
    """
    if source == "crossref":
        work = await _fetch_crossref_work(doi)
        if "error" in work:
            return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")
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

    data = await opencitations.get_references(doi)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
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
async def get_paper_citations_count(doi: DOI) -> dict[str, Any]:
    """Get the number of incoming citations (papers that cite this work) from OpenCitations."""
    data = await opencitations.get_citations(doi)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
    return {"doi": doi, "count": data["count"]}


@mcp.tool
async def get_paper_citations(
    doi: DOI,
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
) -> dict[str, Any]:
    """Get a page of incoming citations (papers that cite this work) from OpenCitations.

    Returns citing DOIs with publication dates, self-citation flags, and
    cross-referenced IDs. Call get_paper_citations_count first to see the total.
    """
    data = await opencitations.get_citations(doi)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")

    cites = data.get("citations", [])
    total = len(cites)

    start = (page - 1) * page_size
    end = start + page_size

    return {
        "doi": doi,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        "citations": cites[start:end],
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
    """Search Wikipedia for articles matching a query.

    Returns matching article titles and URLs. Useful for finding the correct
    Wikipedia article title before verifying with get_wikipedia_summary.
    """
    results = await wikipedia.search(query, limit=limit)
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
    """Get a summary of a Wikipedia article: title, description, extract, and URL.

    Also reports the page type — 'standard' for normal articles,
    'disambiguation' for disambiguation pages. Returns an error if the
    page does not exist.
    """
    return await wikipedia.get_summary(title)


@mcp.tool
async def check_wikipedia_page(
    title: Annotated[
        str,
        Field(
            description="Wikipedia article title to verify "
            "(e.g. 'Cytochrome P450')."
        ),
    ],
) -> dict[str, Any]:
    """Check if a Wikipedia page exists and is a standard article (not a disambiguation page).

    Returns exists, is_disambiguation, canonical title, and URL.
    Use this to verify Wikipedia URLs before suggesting them as cross-reference links.
    """
    return await wikipedia.page_exists(title)


if __name__ == "__main__":
    mcp.run()
