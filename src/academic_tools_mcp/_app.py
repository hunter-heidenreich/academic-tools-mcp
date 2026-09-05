"""Shared FastMCP application core.

Holds the `mcp` instance, the lifespan, the Annotated parameter-type vocabulary,
and the helpers used by more than one tool group. Imports infrastructure,
providers, and content modules only -- never the `tools` package -- so tool
modules can import from here without an import cycle.
"""

from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from . import _clients, cache, papers
from .providers import crossref


@asynccontextmanager
async def _lifespan(app: FastMCP):
    """Manage process-wide resources tied to the server's life.

    On startup: sweep ``.cache/`` for stale ``*.tmp`` files left behind
    by killed writers from previous runs, then rename any cached PDF or
    markdown still using a pre-``safe_stem`` filename so it isn't silently
    orphaned. Both are cheap and idempotent. New clients are pooled lazily
    on first use, so we don't pre-build them here.

    On shutdown: close every pooled httpx.AsyncClient so we don't leak
    sockets if the server is stopped while clients are idle.
    """
    cache.gc_orphan_tmp_files()
    papers.migrate_legacy_stems()
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
        "page_size=25) for huge collaboration lists. For 30+ identifiers "
        "at once (e.g. enriching a reference list), use get_papers_metadata "
        "— OpenAlex DOIs collapse into batched HTTP calls and arXiv / "
        "bioRxiv fetch concurrently.\n\n"
        "PDF pipeline: download_pdf → convert_paper → get_paper_sections → "
        "get_paper_section. All auto-detect the provider. find_in_paper "
        "scans a converted paper for a substring and returns section + "
        "char_offset for every hit (chain into get_paper_section to read "
        "context). For PDFs outside arXiv/bioRxiv/ACL, fetch the file "
        "yourself and hand it to import_paper. import_paper also accepts "
        "pre-converted .md/.markdown files — these skip convert_paper "
        "entirely (useful when the converter is unavailable or when you "
        "have a higher-quality manual conversion). get_paper_section pages "
        "by character offset (re-call with offset=next_offset) for long "
        "sections. download_pdf with force_refresh=True automatically "
        "drops cached markdown + sections so the next convert_paper "
        "picks up the new bytes.\n\n"
        "References/citations use count-then-page (`_count` first, then "
        "paginate). Search tools (search_arxiv, search_crossref_by_title) "
        "return slim triage hits — chain to get_paper_metadata for the full "
        "record (free cache hit). search_cached_papers does BM25 across "
        "every converted paper in your local cache.\n\n"
        "All tools return {error, suggestion?} on failure; transient errors "
        "(5xx, 429, timeouts) include retry hints."
    ),
)

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


# The pipeline stages an agent must run in order. Repeated verbatim in five
# error messages before this constant existed.
PIPELINE_CHAIN = "download_pdf → convert_paper → get_paper_sections → get_paper_section"


def not_converted_error(identifier: str) -> dict[str, Any]:
    """Uniform error for "this paper has no converted markdown yet".

    Four tools produced this condition in two different shapes: three jammed
    the recovery advice into the ``error`` string while ``find_in_paper``
    split it into a proper ``suggestion`` key. Agents branch on ``suggestion``,
    so the shape has to be the same everywhere.
    """
    return {
        "error": f"Paper not converted yet for: {identifier}.",
        "suggestion": f"Run the pipeline first: {PIPELINE_CHAIN}.",
    }


def pdf_not_cached_error(identifier: str) -> dict[str, Any]:
    """Uniform error for "no usable PDF is cached for this paper"."""
    return {
        "error": f"PDF not cached for: {identifier}.",
        "suggestion": (
            f"Run the pipeline first: {PIPELINE_CHAIN}. For PDFs outside "
            "arXiv/bioRxiv/ACL, fetch the file yourself and hand it to "
            "import_paper (accepts .pdf or .md/.markdown)."
        ),
    }


def _enrich_error(result: dict[str, Any], suggestion: str) -> dict[str, Any]:
    """Add a suggestion to an error dict if one isn't already present."""
    if "error" in result and "suggestion" not in result:
        result["suggestion"] = suggestion
    return result


def _arxiv_id_from_entry(paper: dict[str, Any]) -> str:
    """Extract the bare arXiv ID from an arXiv entry's id URL."""
    raw_id = paper.get("id", "")
    if "/abs/" in raw_id:
        return raw_id.split("/abs/")[-1]
    return raw_id


def _first(value: Any) -> Any:
    """First element of a list, else the value itself (or None for empties).

    Crossref returns several scalar-ish fields (title, container-title) as
    single-element lists, so unwrap them to match the OpenAlex shape. Shared by
    paper.py (metadata formatting) and search.py (Crossref triage hits).
    """
    if isinstance(value, list):
        return value[0] if value else None
    return value


# Canonical fallback order for a Crossref work's publication date. Prefers the
# formally-issued/published dates; `posted` (preprint date, present on every
# bioRxiv DOI and other preprint-only records) is the last resort so a record
# carrying only `posted` still yields a year. Shared by paper.py's metadata
# formatting and search.py's triage-hit year extraction so the two can't drift
# (they previously disagreed on whether `posted` counted).
_CROSSREF_DATE_KEYS = ("issued", "published-print", "published-online", "published", "posted")


def _crossref_date(work: dict[str, Any]) -> tuple[int | None, str | None]:
    """Extract ``(year, ISO-date)`` from a Crossref work's date-parts.

    Crossref dates are ``{"date-parts": [[year, month, day]]}`` with month and
    day optional. Walks ``_CROSSREF_DATE_KEYS`` and returns the year plus, when
    month (and optionally day) are present, a zero-padded ISO string. Guards
    malformed ``date-parts`` (``null`` / ``[]`` / ``[[null]]``) so a bad record
    degrades to ``(None, None)`` instead of crashing.
    """
    for key in _CROSSREF_DATE_KEYS:
        parts = (work.get(key) or {}).get("date-parts") or [[]]
        first = parts[0] if parts else []
        if first and isinstance(first[0], int):
            year = first[0]
            iso = f"{year:04d}"
            if len(first) >= 2 and isinstance(first[1], int):
                iso += f"-{first[1]:02d}"
                if len(first) >= 3 and isinstance(first[2], int):
                    iso += f"-{first[2]:02d}"
            return year, iso
    return None, None


FOLLOW_PUBLISHED = Annotated[
    bool,
    Field(
        description=(
            "If True and this is a bioRxiv/medRxiv preprint that has a "
            "journal version (published_doi field), automatically chain "
            "to OpenAlex and return the published record instead. "
            "On success the response carries _source='openalex_via_biorxiv', "
            "a preprint_doi field so the chain stays visible, and "
            "followed_published=True. If OpenAlex hasn't indexed the journal "
            "version yet, falls back to the preprint record with "
            "_source='biorxiv' and followed_published=False so the lag is "
            "explicit; a *transient* lookup failure (5xx/timeout) adds "
            "published_lookup_retryable=True so the chain can be retried. "
            "Has no effect for other identifier shapes or "
            "unpublished preprints (no published_doi → field absent)."
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

FALLBACK_CROSSREF = Annotated[
    bool,
    Field(
        description=(
            "If True and OpenAlex returns a definitive 'not found' for a "
            "DOI (HTTP 404 — not a transient 5xx/429/timeout), fall back "
            "to Crossref, whose indexing of recent DOIs is often ahead of "
            "OpenAlex. Useful for brand-new papers and niche venues. The "
            "fallback response carries _source='crossref' and a reduced "
            "field set: no abstract and no open-access fields "
            "(is_oa/oa_status/oa_url/pdf_url are null). Default False "
            "keeps the hard 'not found' error. Only affects DOI lookups "
            "that route to OpenAlex; no effect for arXiv/bioRxiv shapes."
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
            "effect on identifiers that import_paper handled — replace "
            "those with import_paper(..., force_refresh=True). Default "
            "False reads from the cached PDF if present."
        ),
    ),
]

IMPORT_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "If True, re-import this file even if a PDF/markdown is already "
            "cached under this identifier, replacing the cached copy. For a "
            "PDF this also drops any cached markdown + section index so the "
            "next convert_paper re-runs on the new bytes. Use this to swap in "
            "a corrected PDF or a higher-quality manual conversion. Default "
            "False returns the existing cached copy untouched."
        ),
    ),
]

ALLOW_OA_URL = Annotated[
    bool,
    Field(
        description=(
            "If True, papers that are NOT arXiv / bioRxiv / ACL (i.e. "
            "generic publisher DOIs) may be downloaded from the "
            "open-access PDF URL that OpenAlex reports for the DOI. Only "
            "the URL already present in the paper's OpenAlex metadata is "
            "fetched — never an arbitrary URL — so this stays gated, not a "
            "general scraper. Errors if the paper is closed-access, isn't "
            "in OpenAlex, or the URL turns out to be a landing page rather "
            "than a PDF. Default False keeps the strict refusal: fetch "
            "those PDFs yourself and use import_paper. Ignored for arXiv / "
            "bioRxiv / ACL identifiers, which always download directly."
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

CONVERT_MODE = Annotated[
    Literal["full", "fast"],
    Field(
        description=(
            "Conversion backend. 'full' (default) runs the heavy converter "
            "(MinerU/Marker) for high-quality markdown with tables/equations, "
            "but is slow (minutes) and serialised server-wide — only one runs "
            "at a time, others get a retryable 'busy' error. 'fast' runs a "
            "lightweight text extractor (pdftotext/pymupdf) outside that lock: "
            "seconds, never 'busy', but DEGRADED — plain text only, no tables, "
            "equations, figures, or real headings. Use 'fast' when 'full' "
            "times out, the heavy converter is unavailable, or you just need "
            "searchable text quickly. The response echoes conversion_mode."
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

AUTHORS_PAGE = Annotated[
    int,
    Field(description="Page number for the author list, starting at 1.", ge=1),
]

AUTHORS_PAGE_SIZE = Annotated[
    int,
    Field(description="Authors per page (1-25, default 25).", ge=1, le=25),
]

PAGE_SIZE = Annotated[
    int,
    Field(description="Number of results per page (1-50).", ge=1, le=50),
]

PAGE = Annotated[
    int,
    Field(description="Page number, starting at 1.", ge=1),
]


async def _fetch_crossref_work(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a work from Crossref and return it, or propagate an error dict."""
    return await crossref.get_work(doi, force_refresh=force_refresh)


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

_FIND_MAX_RESULTS = Annotated[
    int,
    Field(
        description=(
            "Maximum number of hits to return (1-100, default 20). "
            "Hits are ordered by document position; if the query matches "
            "more than this many times the trailing matches are dropped."
        ),
        ge=1,
        le=100,
    ),
]

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
