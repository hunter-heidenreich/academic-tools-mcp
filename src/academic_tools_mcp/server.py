from typing import Annotated, Any

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
        "Supports DOIs, arXiv IDs, and bioRxiv DOIs as identifiers. "
        "PDF pipeline: download_pdf → convert_paper → get_paper_sections → get_paper_section. "
        "These pipeline tools auto-detect the provider from the identifier. "
        "For local files or arbitrary URLs, use import_pdf / download_pdf_url / import_markdown first. "
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


ARXIV_ID = Annotated[
    str,
    Field(
        description="arXiv paper ID. Accepts bare ID (2301.00001), "
        "versioned (2301.00001v2), or URL "
        "(https://arxiv.org/abs/2301.00001)."
    ),
]


BIORXIV_DOI = Annotated[
    str,
    Field(
        description="bioRxiv or medRxiv paper DOI (prefix 10.1101/). "
        "Accepts bare DOI (10.1101/2024.01.01.573838), "
        "URL (https://doi.org/10.1101/...), or "
        "site URL (https://www.biorxiv.org/content/10.1101/...v1)."
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
    """Fetch a work and return it, or raise if not found."""
    return await openalex.get_work(doi)


@mcp.tool
async def get_paper_metadata(doi: DOI) -> dict[str, Any]:
    """Get core metadata for a paper: title, year, type, venue, DOI, and open access info.

    Related: get_paper_authors for authors, get_paper_bibtex for citations,
    get_crossref_references_count for reference list, get_paper_citations_summary
    for citation counts.
    """
    work = await _fetch_work(doi)
    if "error" in work:
        return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")

    primary_location = work.get("primary_location") or {}
    source = primary_location.get("source") or {}
    oa = work.get("open_access") or {}

    return {
        "title": work.get("title"),
        "doi": work.get("doi"),
        "publication_year": work.get("publication_year"),
        "publication_date": work.get("publication_date"),
        "type": work.get("type"),
        "language": work.get("language"),
        "venue": source.get("display_name"),
        "is_oa": oa.get("is_oa"),
        "oa_status": oa.get("oa_status"),
        "oa_url": oa.get("oa_url"),
    }


@mcp.tool
async def get_paper_authors(doi: DOI) -> dict[str, Any]:
    """Get the author list for a paper: names, positions, corresponding status, and institution names.

    Each author includes an openalex_id for chaining into get_author_profile
    or get_author_affiliations.
    """
    work = await _fetch_work(doi)
    if "error" in work:
        return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")

    authors = []
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
        "author_count": len(authors),
        "authors": authors,
        "institution_count": len(all_institutions),
        "all_institutions": all_institutions,
    }


@mcp.tool
async def get_paper_abstract(doi: DOI) -> dict[str, Any]:
    """Get the abstract of a paper as plain text."""
    work = await _fetch_work(doi)
    if "error" in work:
        return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")

    abstract = openalex.reconstruct_abstract(
        work.get("abstract_inverted_index")
    )

    return {
        "title": work.get("title"),
        "abstract": abstract or None,
    }


@mcp.tool
async def get_paper_citations_summary(doi: DOI) -> dict[str, Any]:
    """Get citation statistics for a paper: citation count, reference count, and retraction status."""
    work = await _fetch_work(doi)
    if "error" in work:
        return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")

    return {
        "title": work.get("title"),
        "cited_by_count": work.get("cited_by_count"),
        "referenced_works_count": work.get("referenced_works_count"),
        "is_retracted": work.get("is_retracted"),
    }


@mcp.tool
async def get_paper_topics(doi: DOI) -> dict[str, Any]:
    """Get topic classifications and keywords for a paper."""
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
async def get_paper_bibtex(doi: DOI) -> dict[str, Any]:
    """Generate a BibTeX citation entry for a paper.

    Automatically selects the correct entry type (@article, @inproceedings,
    @misc for preprints, @incollection for book chapters, @phdthesis, etc.)
    based on the work type.
    """
    work = await _fetch_work(doi)
    if "error" in work:
        return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")

    return {
        "bibtex": generate_bibtex(work),
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
# arXiv tools
# ---------------------------------------------------------------------------


async def _fetch_arxiv_paper(arxiv_id: str) -> dict[str, Any]:
    """Fetch an arXiv paper and return it, or propagate error dict."""
    return await arxiv.get_paper(arxiv_id)


def _arxiv_id_from_entry(paper: dict[str, Any]) -> str:
    """Extract bare arXiv ID from the entry's id URL."""
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


@mcp.tool
async def get_arxiv_paper_metadata(arxiv_id: ARXIV_ID) -> dict[str, Any]:
    """Get core metadata for an arXiv paper: title, dates, categories, links, and publication info.

    If doi is present, chain into get_paper_metadata (OpenAlex) for citation
    counts and venue info. Use download_pdf to get the full paper content.
    """
    paper = await _fetch_arxiv_paper(arxiv_id)
    if "error" in paper:
        return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv to find papers.")

    return {
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


@mcp.tool
async def get_arxiv_paper_authors(arxiv_id: ARXIV_ID) -> dict[str, Any]:
    """Get the author list for an arXiv paper, with affiliations when available."""
    paper = await _fetch_arxiv_paper(arxiv_id)
    if "error" in paper:
        return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv to find papers.")

    authors = paper.get("authors", [])
    return {
        "author_count": len(authors),
        "authors": authors,
    }


@mcp.tool
async def get_arxiv_paper_abstract(arxiv_id: ARXIV_ID) -> dict[str, Any]:
    """Get the abstract of an arXiv paper."""
    paper = await _fetch_arxiv_paper(arxiv_id)
    if "error" in paper:
        return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv to find papers.")

    return {
        "title": paper.get("title"),
        "abstract": paper.get("summary"),
    }


@mcp.tool
async def get_arxiv_paper_bibtex(arxiv_id: ARXIV_ID) -> dict[str, Any]:
    """Generate a BibTeX citation entry for an arXiv paper.

    Uses @article if the paper has a journal reference, otherwise @misc with
    eprint, archivePrefix, and primaryClass fields.
    """
    paper = await _fetch_arxiv_paper(arxiv_id)
    if "error" in paper:
        return _enrich_error(paper, "Check the arXiv ID format (e.g. 2301.00001) or use search_arxiv to find papers.")

    return {
        "bibtex": generate_arxiv_bibtex(paper),
    }


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

    Use the returned arxiv_id with get_arxiv_paper_abstract, get_arxiv_paper_bibtex,
    or download_pdf to access full paper content.
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
            "Use import_pdf (local file) or download_pdf_url (URL) instead, "
            "then convert_paper → get_paper_sections → get_paper_section."
        }


@mcp.tool
async def download_pdf(identifier: PAPER_ID) -> dict[str, Any]:
    """Download and cache the PDF for a paper, auto-detecting the source.

    Supports arXiv IDs, ACL Anthology DOIs (10.18653/v1/...), and
    bioRxiv/medRxiv DOIs (10.1101/...). Skips download if already cached.
    For other sources, use import_pdf or download_pdf_url instead.

    Next step: convert_paper → get_paper_sections → get_paper_section.
    """
    return await _download_pdf_by_provider(identifier)


@mcp.tool
async def convert_paper(identifier: PAPER_ID) -> dict[str, Any]:
    """Convert a downloaded PDF to markdown, then parse into sections.

    Auto-detects the provider from the identifier and routes to the correct
    cache namespace. This is a slow operation (5-10 minutes). Returns the
    section index on completion. Skips conversion if markdown is already cached.

    The PDF must be downloaded first via download_pdf (or import_pdf /
    download_pdf_url for other sources).

    Next step: get_paper_sections → get_paper_section.
    """
    target = manual._resolve_target(identifier)
    pdf = target["pdf_path"]

    if not pdf.exists():
        return {
            "error": f"PDF not cached for: {identifier}. "
            "Pipeline: download_pdf → convert_paper → get_paper_sections → get_paper_section. "
            "For local files use import_pdf; for URLs use download_pdf_url."
        }

    return await papers.convert_pdf(pdf, target["namespace"], target["canonical"])


@mcp.tool
async def get_paper_sections(identifier: PAPER_ID) -> dict[str, Any]:
    """Get the section index for a converted paper.

    Auto-detects the provider from the identifier. Returns section titles
    with sub-heading previews and approximate token counts.

    The paper must be converted first via convert_paper.

    Next step: get_paper_section(identifier, section_index_or_title).
    """
    target = manual._resolve_target(identifier)
    namespace = target["namespace"]
    canonical = target["canonical"]

    cached = cache.get(namespace, "sections", papers._sections_key(canonical))
    if cached is not None:
        return cached

    md_path = papers._markdown_path(namespace, canonical)
    if not md_path.exists():
        return {
            "error": f"Paper not converted yet for: {identifier}. "
            "Pipeline: download_pdf → convert_paper → get_paper_sections → get_paper_section."
        }

    markdown = md_path.read_text()
    sections = papers.parse_sections(markdown)
    sections_data = {
        "total_sections": len(sections),
        "total_approx_tokens": sum(s.get("approx_tokens", 0) for s in sections),
        "sections": sections,
    }
    cache.put(namespace, "sections", papers._sections_key(canonical), sections_data)
    return sections_data


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
# bioRxiv / medRxiv tools
# ---------------------------------------------------------------------------


async def _fetch_biorxiv_paper(doi: str) -> dict[str, Any]:
    """Fetch a bioRxiv/medRxiv paper and return it, or propagate error dict."""
    return await biorxiv.get_paper(doi)


@mcp.tool
async def get_biorxiv_paper_metadata(doi: BIORXIV_DOI) -> dict[str, Any]:
    """Get core metadata for a bioRxiv/medRxiv preprint: title, date, category, version, server, and publication status.

    If published_doi is present, the paper has been formally published — chain
    into get_paper_metadata or get_paper_bibtex with that DOI for journal metadata.
    """
    paper = await _fetch_biorxiv_paper(doi)
    if "error" in paper:
        return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title to find bioRxiv papers.")

    return {
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


@mcp.tool
async def get_biorxiv_paper_authors(doi: BIORXIV_DOI) -> dict[str, Any]:
    """Get the author list for a bioRxiv/medRxiv preprint, including the corresponding author and their institution."""
    paper = await _fetch_biorxiv_paper(doi)
    if "error" in paper:
        return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title to find bioRxiv papers.")

    authors = paper.get("authors", [])
    return {
        "author_count": len(authors),
        "authors": authors,
        "author_corresponding": paper.get("author_corresponding"),
        "author_corresponding_institution": paper.get("author_corresponding_institution"),
    }


@mcp.tool
async def get_biorxiv_paper_abstract(doi: BIORXIV_DOI) -> dict[str, Any]:
    """Get the abstract of a bioRxiv/medRxiv preprint."""
    paper = await _fetch_biorxiv_paper(doi)
    if "error" in paper:
        return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title to find bioRxiv papers.")

    return {
        "title": paper.get("title"),
        "abstract": paper.get("abstract"),
    }


@mcp.tool
async def get_biorxiv_paper_bibtex(doi: BIORXIV_DOI) -> dict[str, Any]:
    """Generate a BibTeX citation entry for a bioRxiv/medRxiv preprint.

    Uses @article if the paper has been published in a journal (published_doi
    available), otherwise @misc with the preprint DOI and server name.
    """
    paper = await _fetch_biorxiv_paper(doi)
    if "error" in paper:
        return _enrich_error(paper, "Check the DOI format (10.1101/...) or use search_crossref_by_title to find bioRxiv papers.")

    return {
        "bibtex": generate_biorxiv_bibtex(paper),
    }


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
async def download_pdf_url(
    url: Annotated[
        str,
        Field(
            description="Direct URL to a PDF file. Must point to the actual "
            "PDF, not a landing page."
        ),
    ],
    identifier: PAPER_ID,
) -> dict[str, Any]:
    """Download a PDF from any URL and cache it for conversion and section access.

    Use this for PDFs from publisher sites, institutional repositories, or
    personal pages that aren't covered by the arXiv/bioRxiv/ACL pipelines.
    The identifier is used as the cache key — use the DOI when available.

    Next step: convert_paper → get_paper_sections → get_paper_section.
    """
    return await manual.download_pdf_from_url(url, identifier)


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
    get_crossref_references.
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
    """Fetch a work from Crossref and return it, or raise if not found."""
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


@mcp.tool
async def get_crossref_references_count(doi: DOI) -> dict[str, Any]:
    """Get the number of references (bibliography entries) for a paper from Crossref."""
    work = await _fetch_crossref_work(doi)
    if "error" in work:
        return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")

    raw_refs = work.get("reference") or []
    return {"doi": doi, "count": len(raw_refs)}


@mcp.tool
async def get_crossref_references(
    doi: DOI,
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
) -> dict[str, Any]:
    """Get a page of references for a paper from Crossref.

    Returns structured reference metadata: DOIs, authors, titles, years,
    journals, volumes, and pages when available. Quality varies by publisher —
    some references include full structured fields, others only an unstructured
    citation string.

    Use get_crossref_references_count first to know the total, then page
    through with page and page_size.
    """
    work = await _fetch_crossref_work(doi)
    if "error" in work:
        return _enrich_error(work, "Check the DOI format or use search_crossref_by_title to find the correct DOI.")

    raw_refs = work.get("reference") or []
    total = len(raw_refs)

    start = (page - 1) * page_size
    end = start + page_size
    page_refs = [_format_crossref_reference(r) for r in raw_refs[start:end]]

    return {
        "doi": doi,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        "references": page_refs,
    }


# ---------------------------------------------------------------------------
# OpenCitations tools
# ---------------------------------------------------------------------------


@mcp.tool
async def get_opencitations_references_count(doi: DOI) -> dict[str, Any]:
    """Get the number of outgoing references for a paper from OpenCitations."""
    data = await opencitations.get_references(doi)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
    return {"doi": doi, "count": data["count"]}


@mcp.tool
async def get_opencitations_references(
    doi: DOI,
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
) -> dict[str, Any]:
    """Get a page of outgoing references (bibliography) for a paper from OpenCitations.

    Returns cited DOIs with publication dates and self-citation flags.
    OpenCitations aggregates from Crossref, PubMed, DataCite, OpenAIRE, and
    JaLC, so it may have references that Crossref alone does not.
    Note: returns DOI identifiers only, not full metadata.

    Use get_opencitations_references_count first to know the total, then page
    through with page and page_size.
    """
    data = await opencitations.get_references(doi)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")

    refs = data.get("references", [])
    total = len(refs)

    start = (page - 1) * page_size
    end = start + page_size

    return {
        "doi": doi,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        "references": refs[start:end],
    }


@mcp.tool
async def get_opencitations_citations_count(doi: DOI) -> dict[str, Any]:
    """Get the number of incoming citations for a paper from OpenCitations."""
    data = await opencitations.get_citations(doi)
    if "error" in data:
        return _enrich_error(data, "Check the DOI format. OpenCitations requires a valid DOI.")
    return {"doi": doi, "count": data["count"]}


@mcp.tool
async def get_opencitations_citations(
    doi: DOI,
    page: PAGE = 1,
    page_size: PAGE_SIZE = 20,
) -> dict[str, Any]:
    """Get a page of incoming citations (papers that cite this work) from OpenCitations.

    Returns citing DOIs with publication dates and self-citation flags.

    Use get_opencitations_citations_count first to know the total, then page
    through with page and page_size.
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
