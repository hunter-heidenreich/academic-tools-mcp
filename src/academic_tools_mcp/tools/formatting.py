"""Pure payload shapers for paper metadata, author pages, and entity profiles.

Separated from :mod:`.paper` so the MCP tool module owns only identifier
resolution, provider/fallback orchestration, and ``@mcp.tool`` docstrings, while
every synchronous dict transformation can be unit-tested without async fixtures.
"""

from collections.abc import Callable
from typing import Any

from .. import manual
from ..app import crossref_date, openalex_arxiv_id, openalex_pmcid, openalex_pmid
from ..providers import arxiv, crossref, openalex
from ..util.jsonshape import as_dict, as_int, dict_list, unwrap_first


def arxiv_pdf_url(paper: dict[str, Any]) -> str | None:
    """Extract the PDF link from an arXiv entry's links list."""
    for link in dict_list(paper.get("links")):
        if link.get("title") == "pdf":
            return link.get("href")
    return None


def format_arxiv_metadata(
    paper: dict[str, Any],
    canonical_id: str | None,
    *,
    followed_published: bool | None = None,
) -> dict[str, Any]:
    """Shape an arXiv entry into the ``get_paper_metadata`` response envelope."""
    result = {
        "_source": "arxiv",
        "_canonical_id": canonical_id,
        "arxiv_id": arxiv.id_from_entry(paper),
        "title": paper.get("title"),
        "published": paper.get("published"),
        "updated": paper.get("updated"),
        "primary_category": paper.get("primary_category"),
        "categories": paper.get("categories"),
        "pdf_url": arxiv_pdf_url(paper),
        "doi": paper.get("doi"),
        "journal_ref": paper.get("journal_ref"),
        "comment": paper.get("comment"),
    }
    # Absent unless a chain was attempted, so the default shape is unchanged.
    if followed_published is not None:
        result["followed_published"] = followed_published
    return result


def format_biorxiv_metadata(
    paper: dict[str, Any],
    canonical_id: str | None,
    *,
    followed_published: bool | None = None,
) -> dict[str, Any]:
    """Shape a bioRxiv/medRxiv record into the ``get_paper_metadata`` response envelope."""
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


def format_acl_metadata(paper: dict[str, Any], canonical_id: str | None) -> dict[str, Any]:
    """Shape an ACL Anthology record into the ``get_paper_metadata`` response envelope."""
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


def format_openalex_metadata(work: dict[str, Any], canonical_id: str | None) -> dict[str, Any]:
    """Shape an OpenAlex work into the ``get_paper_metadata`` response envelope."""
    primary_location = as_dict(work.get("primary_location"))
    source_obj = as_dict(primary_location.get("source"))
    oa = as_dict(work.get("open_access"))
    return {
        "_source": "openalex",
        "_canonical_id": canonical_id,
        "title": work.get("title"),
        "doi": work.get("doi"),
        "pmid": openalex_pmid(work),
        "pmcid": openalex_pmcid(work),
        "arxiv_id": openalex_arxiv_id(work),
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


def format_openalex_via_preprint(
    work: dict[str, Any],
    journal_canonical: str,
    *,
    source: str,
    preprint_key: str,
    preprint: Any,
) -> dict[str, Any]:
    """The journal work a ``follow_published`` chain reached, tagged with its preprint.

    The two wrappers below keep the per-source names, since ``_source`` and the
    preprint key differ together and a caller reads them as a pair.
    """
    base = format_openalex_metadata(work, journal_canonical)
    base["_source"] = source
    base[preprint_key] = preprint
    base["followed_published"] = True  # symmetric with the False on fall-through
    return base


def format_openalex_via_biorxiv(
    work: dict[str, Any], preprint_doi: str | None, journal_canonical: str
) -> dict[str, Any]:
    """Shape an OpenAlex journal work reached from a bioRxiv/medRxiv preprint."""
    return format_openalex_via_preprint(
        work,
        journal_canonical,
        source="openalex_via_biorxiv",
        preprint_key="preprint_doi",
        preprint=preprint_doi,
    )


def format_openalex_via_arxiv(
    work: dict[str, Any], preprint_arxiv_id: str, journal_canonical: str
) -> dict[str, Any]:
    """Shape an OpenAlex journal work reached from an arXiv preprint."""
    return format_openalex_via_preprint(
        work,
        journal_canonical,
        source="openalex_via_arxiv",
        preprint_key="preprint_arxiv_id",
        preprint=preprint_arxiv_id,
    )


def format_crossref_metadata(work: dict[str, Any], canonical_id: str | None) -> dict[str, Any]:
    """Crossref work → the unified metadata shape.

    Mirrors :func:`format_openalex_metadata`'s key set, with ``pmid`` / ``arxiv_id`` /
    ``is_oa`` / ``oa_status`` / ``oa_url`` / ``pdf_url`` always null.
    """
    year, date = crossref_date(work)
    return {
        "_source": "crossref",
        "_canonical_id": canonical_id,
        "title": unwrap_first(work.get("title")),
        "doi": work.get("DOI"),
        "pmid": None,
        "pmcid": None,
        "arxiv_id": None,
        "publication_year": year,
        "publication_date": date,
        "type": work.get("type"),
        "language": work.get("language"),
        "venue": unwrap_first(work.get("container-title")),
        # Crossref's own name for the same number; int-guarded, the value is untyped JSON.
        "cited_by_count": as_int(work.get("is-referenced-by-count")),
        "is_oa": None,
        "oa_status": None,
        "oa_url": None,
        "pdf_url": None,
    }


def format_metadata_by_source(
    source: manual.MetadataSource, obj: dict[str, Any], canonical_id: str | None
) -> dict[str, Any]:
    """Dispatch a raw provider object to its per-source formatter.

    ``openalex`` is the fall-through, so callers gate on a successful ``_fetch_source``.
    """
    if source == "arxiv":
        return format_arxiv_metadata(obj, canonical_id)
    if source == "biorxiv":
        return format_biorxiv_metadata(obj, canonical_id)
    if source == "acl_anthology":
        return format_acl_metadata(obj, canonical_id)
    return format_openalex_metadata(obj, canonical_id)


def institution_names(items: Any, key: str) -> list[str]:
    """The non-empty string values of *key* across untyped institution rows."""
    return [name for inst in dict_list(items) if isinstance(name := inst.get(key), str) and name]


def authors_page(
    rows: list[dict[str, Any]],
    start: int,
    end: int,
    *,
    build: Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]],
) -> dict[str, Any]:
    """One page of authors plus that page's deduped institution roll-up.

    ``build`` turns one provider row into ``(author, institution names)``; the slice,
    the order-preserving dedupe and the key set are shared, so the Crossref and
    OpenAlex pages cannot drift into two shapes. ``author_count`` is the **whole**
    list the page was sliced from, never the page.
    """
    page_authors: list[dict[str, Any]] = []
    page_institutions: list[str] = []
    for row in rows[start:end]:
        author, inst_names = build(row)
        for name in inst_names:
            if name not in page_institutions:
                page_institutions.append(name)
        page_authors.append(author)
    return {
        "author_count": len(rows),
        "authors": page_authors,
        "page_institution_count": len(page_institutions),
        "page_institutions": page_institutions,
    }


def format_crossref_authors(work: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    """One page of Crossref authors, in :func:`format_openalex_authors`' shape.

    ``institutions`` is real here — Crossref carries a per-author ``affiliation``
    — but usually empty, since most publishers do not deposit it; the
    ``page_institutions`` keys are emitted either way, so a paginating agent
    never feature-detects. ``author_count`` counts the **filtered** list the page
    was sliced from, never the raw one.
    """

    def build(a: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        inst_names = institution_names(a.get("affiliation"), "name")
        return {
            "name": crossref.author_name(a),
            "openalex_id": None,
            "position": a.get("sequence"),
            "is_corresponding": None,
            "institutions": inst_names,
        }, inst_names

    return authors_page(dict_list(work.get("author")), start, end, build=build)


def format_openalex_authors(work: dict[str, Any], start: int, end: int) -> dict[str, Any]:
    """One page of OpenAlex authors, plus that page's deduped institution roll-up.

    Returns ``{author_count, authors, page_institutions, page_institution_count}``,
    ``author_count`` being the whole list; the caller adds the shared envelope.
    """

    def build(a: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        author_info = as_dict(a.get("author"))
        inst_names = institution_names(a.get("institutions"), "display_name")
        return {
            "name": author_info.get("display_name"),
            "openalex_id": author_info.get("id"),
            "position": a.get("author_position"),
            "is_corresponding": a.get("is_corresponding"),
            "institutions": inst_names,
        }, inst_names

    return authors_page(dict_list(work.get("authorships")), start, end, build=build)


def authors_envelope(
    source: str,
    canonical_id: str | None,
    page: int,
    page_size: int,
    end: int,
    page_slice: dict[str, Any],
) -> dict[str, Any]:
    """The shared envelope around one author page.

    One home for the key set and for ``has_more``, which is the page's end against
    the *whole* list — so the Crossref branch and the provider branch cannot answer
    the "is there more?" question differently for the same paper.
    """
    return {
        "_source": source,
        "_canonical_id": canonical_id,
        "page": page,
        "page_size": page_size,
        "has_more": end < page_slice["author_count"],
        **page_slice,
    }


def format_author_profile(author: dict[str, Any]) -> dict[str, Any]:
    """Shape an OpenAlex author object into ``get_author``'s response envelope."""
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
                "years": sorted(y for y in years if as_int(y) is not None),
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


def format_institution_profile(institution: dict[str, Any]) -> dict[str, Any]:
    """Shape an OpenAlex institution object into ``get_institution``'s response envelope."""
    stats = as_dict(institution.get("summary_stats"))
    geo = as_dict(institution.get("geo"))
    names = institution.get("display_name_acronyms"), institution.get("display_name_alternatives")
    alternative_names = [
        name
        for group in names
        if isinstance(group, list)
        for name in group
        if isinstance(name, str)
    ]
    # Lineage includes the institution itself; the rest is what sits above it.
    lineage = institution.get("lineage")
    self_id = institution.get("id")
    parent_institutions = [
        oa_id
        for oa_id in (lineage if isinstance(lineage, list) else [])
        if isinstance(oa_id, str) and oa_id != self_id
    ]

    return {
        "name": institution.get("display_name"),
        "openalex_id": self_id,
        "ror": institution.get("ror"),
        "country_code": institution.get("country_code"),
        "type": institution.get("type"),
        "homepage_url": institution.get("homepage_url"),
        "works_count": institution.get("works_count"),
        "cited_by_count": institution.get("cited_by_count"),
        "h_index": stats.get("h_index"),
        "i10_index": stats.get("i10_index"),
        "city": geo.get("city"),
        "region": geo.get("region"),
        "alternative_names": alternative_names[:10],
        "parent_institutions": parent_institutions,
    }
