from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from . import arxiv, cache, openalex, papers
from .bibtex import generate_arxiv_bibtex, generate_bibtex

mcp = FastMCP("academic-tools")

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


async def _fetch_work(doi: str) -> dict[str, Any]:
    """Fetch a work and return it, or raise if not found."""
    return await openalex.get_work(doi)


@mcp.tool
async def get_paper_metadata(doi: DOI) -> dict[str, Any]:
    """Get core metadata for a paper: title, year, type, venue, DOI, and open access info."""
    work = await _fetch_work(doi)
    if "error" in work:
        return work

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
    """Get the author list for a paper: names, positions, corresponding status, and institution names."""
    work = await _fetch_work(doi)
    if "error" in work:
        return work

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
        "authors": authors,
        "all_institutions": all_institutions,
    }


@mcp.tool
async def get_paper_abstract(doi: DOI) -> dict[str, Any]:
    """Get the abstract of a paper as plain text."""
    work = await _fetch_work(doi)
    if "error" in work:
        return work

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
        return work

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
        return work

    return {
        "title": work.get("title"),
        "topics": [
            {
                "name": t.get("display_name"),
                "score": round(t.get("score", 0), 4),
                "subfield": (t.get("subfield") or {}).get("display_name"),
                "field": (t.get("field") or {}).get("display_name"),
                "domain": (t.get("domain") or {}).get("display_name"),
            }
            for t in work.get("topics", [])
        ],
        "keywords": [
            {
                "keyword": k.get("display_name"),
                "score": round(k.get("score", 0), 4),
            }
            for k in work.get("keywords", [])
        ],
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
        return work

    return {
        "bibtex": generate_bibtex(work),
    }


@mcp.tool
async def get_author_profile(author_id: AUTHOR_ID) -> dict[str, Any]:
    """Get an author's profile: name, ORCID, current institutions, publication/citation counts, h-index, and top topics."""
    author = await openalex.get_author(author_id)
    if "error" in author:
        return author

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
        return author

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
    """Get core metadata for an arXiv paper: title, dates, categories, links, and publication info."""
    paper = await _fetch_arxiv_paper(arxiv_id)
    if "error" in paper:
        return paper

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
        return paper

    return {
        "authors": paper.get("authors", []),
    }


@mcp.tool
async def get_arxiv_paper_abstract(arxiv_id: ARXIV_ID) -> dict[str, Any]:
    """Get the abstract of an arXiv paper."""
    paper = await _fetch_arxiv_paper(arxiv_id)
    if "error" in paper:
        return paper

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
        return paper

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
    """Search arXiv papers. Returns titles, IDs, authors, and categories for matching papers."""
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
# Paper PDF pipeline tools
# ---------------------------------------------------------------------------


@mcp.tool
async def download_arxiv_pdf(arxiv_id: ARXIV_ID) -> dict[str, Any]:
    """Download and cache the PDF for an arXiv paper.

    Returns the local file path and size. Skips download if already cached.
    This must be called before convert_paper.
    """
    return await arxiv.download_pdf(arxiv_id)


@mcp.tool
async def convert_paper(arxiv_id: ARXIV_ID) -> dict[str, Any]:
    """Convert a downloaded arXiv PDF to markdown using MinerU, then parse into sections.

    This is a slow operation (5-10 minutes). Returns the section index on completion.
    The PDF must be downloaded first via download_arxiv_pdf.
    Skips conversion if markdown is already cached.
    """
    canonical = arxiv._canonical_arxiv_id(arxiv_id)
    pdf = arxiv.pdf_path(arxiv_id)

    if not pdf.exists():
        return {
            "error": f"PDF not cached. Call download_arxiv_pdf first for: {arxiv_id}"
        }

    return await papers.convert_pdf(pdf, arxiv.NAMESPACE, canonical)


@mcp.tool
async def get_paper_sections(arxiv_id: ARXIV_ID) -> dict[str, Any]:
    """Get the section index for a converted arXiv paper.

    Returns H2 section titles with H3 sub-heading previews and approximate
    token counts. The paper must be converted first via convert_paper.
    """
    canonical = arxiv._canonical_arxiv_id(arxiv_id)

    # Try cached section index first
    cached = cache.get(arxiv.NAMESPACE, "sections", papers._sections_key(canonical))
    if cached is not None:
        return cached

    # Fall back to parsing from markdown if it exists
    md_path = papers._markdown_path(arxiv.NAMESPACE, canonical)
    if not md_path.exists():
        return {
            "error": f"Paper not converted yet. Call convert_paper first for: {arxiv_id}"
        }

    markdown = md_path.read_text()
    sections = papers.parse_sections(markdown)
    sections_data = {"sections": sections}
    cache.put(arxiv.NAMESPACE, "sections", papers._sections_key(canonical), sections_data)
    return sections_data


@mcp.tool
async def get_paper_section(
    arxiv_id: ARXIV_ID,
    section: Annotated[
        str,
        Field(
            description="Section to retrieve: an integer index (e.g. '0') "
            "or a title substring (e.g. 'Introduction', 'Methods'). "
            "Use get_paper_sections to see available sections."
        ),
    ],
) -> dict[str, Any]:
    """Get the full markdown content of a specific section from a converted arXiv paper.

    Accepts a section index number or a title substring (case-insensitive).
    """
    canonical = arxiv._canonical_arxiv_id(arxiv_id)
    md_path = papers._markdown_path(arxiv.NAMESPACE, canonical)

    if not md_path.exists():
        return {
            "error": f"Paper not converted yet. Call convert_paper first for: {arxiv_id}"
        }

    markdown = md_path.read_text()

    # Try to parse as integer index
    try:
        section_key: int | str = int(section)
    except ValueError:
        section_key = section

    return papers.get_section_content(markdown, section_key)


if __name__ == "__main__":
    mcp.run()
