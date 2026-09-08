"""Shared FastMCP application core.

Holds the `mcp` instance, the lifespan, the Annotated parameter vocabulary, and
the helpers more than one tool group needs. Never imports `tools`, so tool
modules can import from here without a cycle.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypeVar

from fastmcp import FastMCP
from pydantic import Field

from . import corpus, manual
from .net import clients
from .providers import acl
from .store import cache, stems

_T = TypeVar("_T")


@asynccontextmanager
async def _lifespan(app: FastMCP) -> AsyncGenerator[None]:
    """Startup migrations, then shutdown cleanup.

    Four cheap, idempotent sweeps: orphaned ``*.tmp``, pre-``safe_stem``
    filenames, ``manual`` files that now route to arXiv, and ACL PDFs named by
    Anthology ID. Clients are pooled lazily, so only shutdown touches them.
    """
    cache.gc_orphan_tmp_files()
    stems.migrate_legacy_stems()
    manual.migrate_misrouted_arxiv()
    acl.migrate_legacy_pdf_stems()
    try:
        yield
    finally:
        await clients.aclose_all()


mcp = FastMCP(
    "academic-tools",
    lifespan=_lifespan,
    instructions=(
        "Academic paper research: OpenAlex, arXiv, bioRxiv/medRxiv, Crossref, "
        "OpenCitations, ACL Anthology, Wikipedia.\n\n"
        "get_paper_metadata / _authors / _abstract / _bibtex take an arXiv ID "
        "or any DOI and route to the right provider; each response tags "
        "`_source`. For 30+ identifiers use get_papers_metadata.\n\n"
        "PDF pipeline: download_pdf → convert_paper → get_paper_sections → "
        "get_paper_section, all auto-detecting the provider. download_pdf "
        "handles arXiv/bioRxiv/ACL; other DOIs need allow_oa_url=True (fetches "
        "only the OA URL OpenAlex reports) or import_paper with a file you "
        "fetched, which also takes pre-converted .md. convert_paper(mode="
        "'fast') is a seconds-long degraded fallback: plain text, no tables, "
        "equations, or headings. A real download drops cached markdown + "
        "sections; markdown from import_paper survives unless "
        "force_refresh=True. find_in_paper returns section + char_offset per "
        "hit, to chain into get_paper_section.\n\n"
        "References/citations: call the `_count` tool first, then paginate. "
        "Search tools return slim triage hits — chain to get_paper_metadata; a "
        "search_crossref_by_title hit warms the reference tools, not "
        "get_paper_metadata.\n\n"
        "Failures return {error, suggestion?}; transient ones (5xx, 429, "
        "timeout) carry retry hints."
    ),
)

DOI = Annotated[
    str,
    Field(description="Paper DOI. Full URL, doi:-prefixed, or bare (10.1234/example)."),
]

AUTHOR_ID = Annotated[
    str,
    Field(description="OpenAlex author ID (A5023888391) or ORCID URL."),
]

PAPER_ID = Annotated[
    str,
    Field(
        description="Paper identifier — bare, doi:-prefixed, or a full URL. "
        "Auto-routed by shape: arXiv ID (2301.00001, hep-th/9901001), "
        "bioRxiv/medRxiv DOI (10.1101/...), ACL DOI (10.18653/v1/...), or any "
        "other DOI. Pipeline tools (download_pdf, convert_paper, import_paper, "
        "get_paper_sections, get_paper_section) also take a freeform label for "
        "a manually imported file; metadata tools require a shape above."
    ),
]

SECTION_HARNESS_CAP = 200000

SECTION_OFFSET = Annotated[
    int,
    Field(
        description="Character offset to start reading. Pass the next_offset "
        "from a previous call to page through.",
        ge=0,
    ),
]

SECTION_MAX_CHARS = Annotated[
    int,
    Field(
        description="Slice size in characters (~4 chars per token).",
        ge=1,
        le=SECTION_HARNESS_CAP,
    ),
]


# The ordered pipeline stages. Single-homed: every "run the pipeline first" error quotes it.
PIPELINE_CHAIN = "download_pdf → convert_paper → get_paper_sections → get_paper_section"


def not_converted_error(identifier: str) -> dict[str, Any]:
    """Uniform error for "this paper has no converted markdown yet".

    Invariant: recovery advice goes in ``suggestion``, never the ``error``
    string — all four tools that can produce this emit the same shape.
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


def enrich_error(result: dict[str, Any], suggestion: str) -> dict[str, Any]:
    """Add a suggestion to an error dict if one isn't already present."""
    if "error" in result and "suggestion" not in result:
        result["suggestion"] = suggestion
    return result


async def read_markdown(
    identifier: str, scan: Callable[[str], _T]
) -> tuple[manual.Target, _T] | dict[str, Any]:
    """Resolve *identifier*, read its cached markdown, apply ``scan``.

    Returns ``(target, scan(markdown))``, else :func:`not_converted_error`. The
    one home for the read the two tools outside ``papers.sections_lock`` share
    (``get_paper_section``, ``find_in_paper``): off the event loop, explicit
    UTF-8, and ``FileNotFoundError`` degraded, since a cascade can unlink
    between the ``exists()`` check and the read.
    """
    target = manual.resolve_target(identifier)
    md_path = stems.markdown_path(target["namespace"], target["canonical"])

    if not md_path.exists():
        return not_converted_error(identifier)

    def _read_and_scan() -> _T:
        return scan(md_path.read_text(encoding="utf-8"))

    try:
        return target, await asyncio.to_thread(_read_and_scan)
    except FileNotFoundError:
        return not_converted_error(identifier)


def page_bounds(page: int, page_size: int) -> tuple[int, int]:
    """Half-open slice bounds for a 1-based ``page``.

    The one home for the arithmetic ``tools/graph`` and ``get_paper_authors``
    share, including the ``has_more = end < total`` rule that goes with it.
    Envelope keys stay per-tool.
    """
    start = (page - 1) * page_size
    return start, start + page_size


# Shape guards: provider trees arrive verbatim, untyped below the top level.


def as_dict(value: Any) -> dict[str, Any]:
    """``value`` when it is a dict, else ``{}``."""
    return value if isinstance(value, dict) else {}


def dict_list(value: Any) -> list[dict[str, Any]]:
    """The dict elements of ``value``, or ``[]`` when it isn't a list."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def unwrap_first(value: Any) -> Any:
    """First element of a list, else the value itself (None for empties).

    Crossref returns scalar-ish fields (title, container-title) as
    single-element lists; unwrap them to match the OpenAlex shape.
    """
    if isinstance(value, list):
        return value[0] if value else None
    return value


# Crossref publication-date fallback order. `posted` (the preprint date) is last
# so a preprint-only record still yields a year.
_CROSSREF_DATE_KEYS = ("issued", "published-print", "published-online", "published", "posted")


def crossref_date(work: dict[str, Any]) -> tuple[int | None, str | None]:
    """``(year, ISO-date)`` from a Crossref work, else ``(None, None)``.

    Dates are ``{"date-parts": [[year, month, day]]}``, month and day optional.
    Every level is shape-checked, not null-checked: the ``message`` arrives
    verbatim, so a bad record must degrade rather than raise past ``{error}``.
    """
    for key in _CROSSREF_DATE_KEYS:
        parts = as_dict(work.get(key)).get("date-parts")
        first = parts[0] if isinstance(parts, list) and parts else []
        if isinstance(first, list) and first and isinstance(first[0], int):
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
            "bioRxiv/medRxiv only: if the preprint has a published_doi, return "
            "OpenAlex's journal record instead — _source='openalex_via_biorxiv' "
            "plus preprint_doi and followed_published=True. Not-yet-indexed "
            "falls back to the preprint with followed_published=False; a "
            "retryable lookup failure also sets published_lookup_retryable=True. "
            "No effect on other shapes or unpublished preprints."
        ),
    ),
]

FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "Drop the cached record and re-fetch — for a preprint just "
            "published, a citation count that should have grown, or an "
            "identifier that previously 404'd. Otherwise the cache is used "
            "(per-provider TTL)."
        ),
    ),
]

FALLBACK_CROSSREF = Annotated[
    bool,
    Field(
        description=(
            "When OpenAlex returns a definitive 404 for a DOI (not a transient "
            "5xx/429/timeout), fall back to Crossref, which often indexes new "
            "DOIs sooner. The response carries _source='crossref' and a reduced "
            "field set: no abstract, no open-access fields. Only affects DOIs "
            "routed to OpenAlex."
        ),
    ),
]

PDF_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "Drop the cached PDF and re-download — for a corrupt file, or a "
            "provider that replaced it under the same key (a v2 arXiv upload). "
            "No effect on identifiers import_paper handled; replace those with "
            "import_paper(..., force_refresh=True)."
        ),
    ),
]

IMPORT_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "Replace the cached copy for this identifier instead of returning "
            "it. For a PDF this also drops cached markdown + section index, so "
            "the next convert_paper re-runs on the new bytes."
        ),
    ),
]

ALLOW_OA_URL = Annotated[
    bool,
    Field(
        description=(
            "Let a non-arXiv/bioRxiv/ACL DOI download from the open-access PDF "
            "URL OpenAlex reports for it. Only that URL is fetched, never an "
            "arbitrary one. Errors if the paper is closed-access, absent from "
            "OpenAlex, or the URL is a landing page. Otherwise such DOIs are "
            "refused — fetch the PDF yourself and use import_paper. Ignored for "
            "arXiv/bioRxiv/ACL."
        ),
    ),
]

CONVERT_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "Drop cached markdown and section index so the converter re-runs. "
            "Use after replacing the source PDF or upgrading the converter. "
            "Conversion takes minutes — only set this when you need fresh "
            "markdown."
        ),
    ),
]

CONVERT_MODE = Annotated[
    Literal["full", "fast"],
    Field(
        description=(
            "'full' (default) runs the heavy converter (MinerU/Marker) for "
            "tables and equations: minutes, and serialised server-wide — a "
            "concurrent call gets a retryable 'busy' error. 'fast' runs a light "
            "text extractor outside that lock: seconds, never 'busy', but "
            "DEGRADED — plain text, no tables, equations, figures, or real "
            "headings. The response echoes conversion_mode."
        ),
    ),
]

SECTIONS_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "Drop the cached section index and re-parse the existing markdown. "
            "Cheap — no subprocess, no network. Use if the checksum-based "
            "auto-refresh missed a stale index."
        ),
    ),
]

AUTHORS_PAGE = Annotated[
    int,
    Field(description="Page number, from 1.", ge=1),
]

AUTHORS_PAGE_SIZE = Annotated[
    int,
    Field(description="Authors per page.", ge=1, le=25),
]

PAGE_SIZE = Annotated[
    int,
    Field(description="Results per page.", ge=1, le=50),
]

PAGE = Annotated[
    int,
    Field(description="Page number, from 1.", ge=1),
]


REF_SOURCE = Annotated[
    Literal["auto", "crossref", "opencitations"],
    Field(
        description="'auto' (default) surveys both providers and pages from the "
        "better-covered one, biased toward Crossref for its richer entries. "
        "'crossref' gives structured metadata (author, title, year, journal, "
        "DOI), quality varying by publisher. 'opencitations' gives DOI-to-DOI "
        "links with cross-referenced IDs (OMID, OpenAlex, PMID) and "
        "self-citation flags, and may hold entries Crossref lacks."
    ),
]

FIND_MAX_RESULTS = Annotated[
    int,
    Field(
        description=(
            "Maximum hits to return. Ordered by document position; matches beyond this are dropped."
        ),
        ge=1,
        le=100,
    ),
]

CACHE_SEARCH_NAMESPACE = Annotated[
    str | None,
    Field(
        description=(
            "Restrict the search to one cache namespace (arxiv, biorxiv, "
            "acl_anthology, manual). Default searches all."
        ),
    ),
]

CACHE_SEARCH_TOP_K = Annotated[
    int,
    Field(
        description=(
            "Maximum hits to return. Ranked by BM25; ties go to the first-seen "
            "file in alphabetical order."
        ),
        ge=1,
        # The engine's own cap, not a transcribed number, so the two can't drift.
        le=corpus.MAX_TOP_K,
    ),
]
