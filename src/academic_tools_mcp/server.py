from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from . import openalex
from .bibtex import generate_bibtex

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


if __name__ == "__main__":
    mcp.run()
