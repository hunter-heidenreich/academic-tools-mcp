"""Shared FastMCP application core.

Holds the `mcp` instance, the lifespan, the Annotated parameter vocabulary and
the helpers the tool groups share. Never imports `tools` (CI-enforced by
`_LAYERS` in `tests/test_layering.py`), so they can import from here.
"""

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypeVar

from fastmcp import FastMCP
from pydantic import Field

from . import corpus, manual, papers
from .net import clients, http
from .providers import acl, openalex
from .store import cache, stems
from .util import doinorm

_T = TypeVar("_T")


@asynccontextmanager
async def _lifespan(app: FastMCP) -> AsyncGenerator[None]:
    """Startup sweeps, then shutdown cleanup.

    A sweep added here must be idempotent and must not raise out of the lifespan.
    """
    cache.gc_orphan_tmp_files()
    stems.migrate_legacy_stems()
    manual.migrate_misrouted_arxiv()
    manual.migrate_acl_stems()
    try:
        yield
    finally:
        await clients.aclose_all()


mcp = FastMCP(
    "academic-tools",
    lifespan=_lifespan,
    instructions=(
        "Academic paper research: OpenAlex, arXiv, bioRxiv/medRxiv, Crossref, "
        "OpenCitations, ACL Anthology, Wikipedia, Papers with Code.\n\n"
        "get_paper_metadata / _authors / _abstract / _bibtex take an arXiv ID, "
        "an ACL Anthology ID (P16-1160, 2023.acl-long.1) or URL, any DOI, a "
        "PMID or an OpenAlex work ID and route to the right provider; each "
        "response tags `_source`. "
        "A DOI the Anthology hosts answers from it. A PMID and an OpenAlex work ID "
        "(W4312223440) both work everywhere a DOI does, including the graph tools, "
        "whose OpenCitations rows hand both back — so a citing row with no `doi` is "
        "still chainable on its `openalex`. Batch many "
        "identifiers with get_papers_metadata.\n\n"
        "PDF pipeline: download_pdf → convert_paper → get_paper_sections → "
        "get_paper_section, all auto-detecting the provider. download_pdf "
        "handles arXiv/bioRxiv/ACL; other DOIs need allow_oa_url=True (fetches "
        "only the OA URL OpenAlex reports) or import_paper with a file you "
        "fetched, which also takes pre-converted .md. convert_paper turns an "
        "arXiv paper's HTML or a bioRxiv/medRxiv paper's JATS XML into markdown "
        "in seconds, with no PDF needed; other papers need download_pdf first. "
        "convert_paper(mode="
        "'fast') is a seconds-long degraded fallback: plain text, no tables, "
        "equations, or headings. A real download drops cached markdown + "
        "sections; markdown from import_paper, arXiv HTML or bioRxiv JATS survives "
        "unless "
        "force_refresh=True. find_in_paper returns section + char_offset per "
        "hit, to chain into get_paper_section.\n\n"
        "get_paper_updates checks a DOI for retraction and correction notices "
        "before you cite it; a false there means Crossref lists none, not that "
        "the paper stands.\n\n"
        "References/citations: call the `_count` tool first, then paginate. "
        "Search tools return slim triage hits — chain to get_paper_metadata. "
        "search_openalex is the broad topic search and its hits are free to "
        "chain, except ACL Anthology papers; search_arxiv the field-scoped "
        "preprint one; "
        "search_crossref_by_title finds a DOI from a title, and warms the "
        "reference tools rather than get_paper_metadata. search_authors is the "
        "way into get_author when you have a person rather than one of their "
        "papers; chain its hits on openalex_id. autocomplete_openalex is the "
        "cheap way from a name to an OpenAlex ID — it is the route into "
        "get_institution, and the one OpenAlex search that spends no credit "
        "budget, though its hits are not cached. Prefer it to search_openalex "
        "when you know the name and want the identifier.\n\n"
        "Papers with Code (arXiv IDs only): get_paper_code for repositories and HF "
        "artifacts, get_paper_catalog for tasks, methods and best leaderboard ranks, "
        "get_paper_evaluations for every reported result, get_pwc_task and "
        "get_benchmark_leaderboard for tasks and leaderboards, search_paperswithcode "
        "to find a paper. Its per-IP rate limit is tight: call sequentially, never "
        "in parallel, and after an error with retry_after_seconds make no Papers "
        "with Code call until that time passes.\n\n"
        "Failures return {error, suggestion?}; transient ones (5xx, 429, "
        "timeout) carry retry hints."
    ),
)

DOI = Annotated[
    str,
    Field(
        description="Paper DOI. Full URL, doi:-prefixed, or bare (10.1234/example). "
        "A PMID works too — pmid:20079334, a pubmed.ncbi.nlm.nih.gov URL, or a "
        "bare 7-8 digit run — as does an OpenAlex work ID (W4312223440, "
        "openalex:-prefixed, or an openalex.org URL), so a `pmid` or `openalex` from "
        "an OpenCitations row pastes back in. An ACL Anthology ID or URL works too."
    ),
]

AUTHOR_ID = Annotated[
    str,
    Field(
        description="OpenAlex author ID (A5023888391) or an ORCID in any "
        "spelling — bare (0000-0002-1825-0097), orcid:-prefixed, or an orcid.org URL."
    ),
]

INSTITUTION_ID = Annotated[
    str,
    Field(
        description="OpenAlex institution ID (I27837315) or a ROR in any "
        "spelling — bare (00jmfr291), ror:-prefixed, or a ror.org URL."
    ),
]

ARXIV_ID = Annotated[
    str,
    Field(
        description="arXiv ID: bare (1706.03762, hep-th/9901001), arXiv:-prefixed, "
        "or an arxiv.org URL; any version suffix is ignored. DOIs are not accepted — "
        "find the arXiv ID with get_paper_metadata or search_arxiv."
    ),
]

PAPER_ID = Annotated[
    str,
    Field(
        description="Paper identifier — bare, doi:-prefixed, or a full URL. "
        "Auto-routed by shape: arXiv ID (2301.00001, hep-th/9901001), "
        "bioRxiv/medRxiv DOI (10.1101/...), ACL Anthology ID (P16-1160, "
        "2023.acl-long.1) or URL, any other DOI, a PMID (pmid:20079334, a "
        "pubmed.ncbi.nlm.nih.gov URL, or a bare 7-8 digit run), or an OpenAlex "
        "work ID (W4312223440, openalex:-prefixed, or an openalex.org URL). Pipeline and "
        "markdown tools (download_pdf, "
        "convert_paper, import_paper, get_paper_sections, get_paper_section, "
        "find_in_paper) also take a freeform label for a manually imported "
        "file; metadata tools require a shape above."
    ),
]

# The harness's per-result cap: get_paper_section declares it as
# `anthropic/maxResultSizeChars` and SECTION_MAX_CHARS caps requests at it.
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


# A publication-year filter, bounded generously rather than precisely: the job is
# to reject a negative, a zero or a pasted identifier before it costs an upstream
# 400, not to claim the real range of either index.
SEARCH_YEAR_MIN = 1000
SEARCH_YEAR_MAX = 2100


# Single-homed: every "run the pipeline first" suggestion quotes this chain.
PIPELINE_CHAIN = "download_pdf → convert_paper → get_paper_sections → get_paper_section"


def not_converted_error(identifier: str) -> dict[str, Any]:
    """Uniform error for "this paper has no converted markdown yet".

    Invariant: advice goes in ``suggestion``, never the ``error`` string.
    """
    return {
        "error": f"Paper not converted yet for: {identifier}.",
        "suggestion": f"Run the pipeline first: {PIPELINE_CHAIN}.",
    }


def pdf_not_cached_error(identifier: str) -> dict[str, Any]:
    """Uniform error for "no usable PDF is cached"; no ``retryable``, since nothing was tried."""
    return {
        "error": f"PDF not cached for: {identifier}.",
        "suggestion": (
            f"Run the pipeline first: {PIPELINE_CHAIN}. For PDFs outside "
            "arXiv/bioRxiv/ACL, fetch the file yourself and hand it to "
            "import_paper (accepts .pdf or .md/.markdown)."
        ),
    }


def enrich_error(result: dict[str, Any], suggestion: str) -> dict[str, Any]:
    """Add *suggestion* to an error dict unless it has one; mutates in place and returns it."""
    if "error" in result and "suggestion" not in result:
        result["suggestion"] = suggestion
    return result


async def resolve_paper_identifier(
    identifier: str, *, force_refresh: bool = False
) -> tuple[str, dict[str, Any] | None]:
    """Trade an identifier for the one its paper is stored under.

    PMID or OpenAlex work ID → DOI → Anthology ID.

    Returns ``(identifier, None)`` or ``(identifier, error)``. **The one place an
    identifier changes identity**, so no paper gets a second cache key. Graph tools
    use the two non-DOI trades directly: they stay DOI-keyed.

    Not in ``manual.resolve_target``, which is pure and synchronous.
    """
    identifier, trade_error = await _trade_non_doi(identifier, force_refresh=force_refresh)
    if trade_error is not None:
        return identifier, trade_error
    return await _trade_hosted_doi(identifier), None


async def _trade_non_doi(
    identifier: str, *, force_refresh: bool
) -> tuple[str, dict[str, Any] | None]:
    """The PMID and OpenAlex-work-ID trades, in one order every caller shares.

    Both shapes are disjoint and each passes anything else through, so the order is
    not load-bearing — sharing it is, since the two front doors would otherwise drift
    on which identifiers they accept.
    """
    identifier, pmid_error = await resolve_pmid_identifier(identifier, force_refresh=force_refresh)
    if pmid_error is not None:
        return identifier, pmid_error
    return await resolve_work_id_identifier(identifier, force_refresh=force_refresh)


async def _trade_hosted_doi(identifier: str) -> str:
    """The Anthology ID for an unrouted DOI the Anthology hosts, else *identifier*. Never errors."""
    if not doinorm.looks_like_doi(identifier):
        return identifier
    if manual.resolve_target(identifier)["namespace"] != manual.NAMESPACE:
        return identifier

    anthology_id = await acl.anthology_id_for_doi(identifier)
    if anthology_id is None:
        return identifier
    await _repair_import(manual.refile_hosted_doi_stems, identifier, anthology_id)
    return anthology_id


async def resolve_pmid_identifier(
    identifier: str, *, force_refresh: bool = False
) -> tuple[str, dict[str, Any] | None]:
    """Trade a PMID for its DOI; pass every other identifier through untouched.

    Returns ``(identifier, None)`` or ``(identifier, error)``.
    """
    if not openalex.is_pmid(identifier):
        return identifier, None

    resolved = await openalex.resolve_pmid(identifier, force_refresh=force_refresh)
    if "error" in resolved:
        return identifier, enrich_error(
            resolved,
            "Check the PMID on pubmed.ncbi.nlm.nih.gov, or pass the paper's DOI "
            "directly. Only papers OpenAlex has indexed resolve by PMID.",
        )

    doi = resolved.get("doi")
    if not doi:
        # A real record with no DOI: every tool below is DOI- or arXiv-keyed, so
        # naming the OpenAlex id beats inventing a key the cache would then own.
        return identifier, {
            **http.not_found(
                f"OpenAlex indexes PMID {openalex.normalize_pmid(identifier)} "
                f"({resolved.get('openalex_id')}) without a DOI, and these tools "
                "are DOI-keyed."
            ),
            "suggestion": (
                "Fetch the PDF yourself and call import_paper with a label of "
                "your choosing to read the full text."
            ),
        }

    await _repair_import(manual.refile_pmid_stems, identifier, doi)
    return doi, None


async def resolve_work_id_identifier(
    identifier: str, *, force_refresh: bool = False
) -> tuple[str, dict[str, Any] | None]:
    """Trade an OpenAlex work ID for its DOI; pass every other identifier through untouched.

    Returns ``(identifier, None)`` or ``(identifier, error)``. Sibling of
    :func:`resolve_pmid_identifier`, kept separate so each trade's error names the
    identifier the caller actually passed.
    """
    if not openalex.is_work_id(identifier):
        return identifier, None

    resolved = await openalex.resolve_work_id(identifier, force_refresh=force_refresh)
    if "error" in resolved:
        return identifier, enrich_error(
            resolved,
            "Check the ID on openalex.org, or pass the paper's DOI directly. Only "
            "works OpenAlex still indexes under that ID resolve.",
        )

    doi = resolved.get("doi")
    if not doi:
        # A real record with no DOI — the case that makes a DOI-less graph row
        # unchainable at all. Nothing below can key on it; say so rather than 404.
        return identifier, {
            **http.not_found(
                f"OpenAlex indexes {openalex.normalize_work_id(identifier)} without a "
                "DOI, and these tools are DOI-keyed."
            ),
            "suggestion": (
                "Fetch the PDF yourself and call import_paper with a label of "
                "your choosing to read the full text."
            ),
        }

    await _repair_import(manual.refile_work_id_stems, identifier, doi)
    return doi, None


def reject_non_doi(doi: str, *, subject: str) -> dict[str, Any] | None:
    """Error dict when ``doi`` is not DOI-shaped, else ``None``.

    Without this an arXiv ID buys a 404 round-trip and negative-caches it under an
    identifier that could never have resolved. ``subject`` names the refusing tool,
    which are DOI-only for different reasons. ``doinorm.looks_like_doi`` is the
    metadata dispatcher's predicate too.
    """
    if doinorm.looks_like_doi(doi):
        return None
    return {
        **http.not_found(f"Not a DOI: {doi!r}. {subject} are DOI-only."),
        "suggestion": (
            "Pass a DOI (e.g. 10.1038/nature12373), in bare, doi: or "
            "https://doi.org/ form, a PMID, or an OpenAlex work ID. For an arXiv paper, call "
            "get_paper_metadata first and use the doi field, or "
            "search_crossref_by_title to find one."
        ),
    }


async def resolve_doi_identifier(
    doi: str, *, subject: str, force_refresh: bool = False
) -> tuple[str, dict[str, Any] | None]:
    """Canonical DOI for a DOI-only tool, or the error that ends the call.

    The one entry every such tool takes, so the non-DOI trades and the rejection keep
    one order across all of them — resolving first is what makes the ``pmid`` and
    ``openalex`` ids the graph tools hand out on every OpenCitations row ones they also
    take. An Anthology ID trades for the DOI on its record.
    """
    doi, trade_error = await _trade_non_doi(doi, force_refresh=force_refresh)
    if trade_error is not None:
        return doi, trade_error
    if not doinorm.looks_like_doi(doi) and acl.is_anthology_id(doi):
        doi, acl_error = await _anthology_doi(doi, subject=subject, force_refresh=force_refresh)
        if acl_error is not None:
            return doi, acl_error
    if (bad := reject_non_doi(doi, subject=subject)) is not None:
        return doi, bad
    return doinorm.canonical(doi), None


async def _anthology_doi(
    identifier: str, *, subject: str, force_refresh: bool
) -> tuple[str, dict[str, Any] | None]:
    """An Anthology ID's DOI, or the error that ends the call (forwarded whole)."""
    record = await acl.get_paper(identifier, force_refresh=force_refresh)
    if "error" in record:
        hint = (
            "Check the Anthology ID on aclanthology.org, or pass the paper's DOI directly."
            if record.get("not_found")
            else "Retry, or pass the paper's DOI directly if you have it."
        )
        return identifier, enrich_error(record, hint)
    doi = record.get("doi")
    if not isinstance(doi, str) or not doi:
        anthology_id = acl.canonical_key(identifier)
        return anthology_id, {
            **http.not_found(
                f"ACL Anthology paper {anthology_id} has no DOI. {subject} are DOI-only."
            ),
            "suggestion": (
                "Read the paper's own reference list instead: "
                "download_pdf → convert_paper → get_paper_sections."
            ),
        }
    return doi, None


async def _repair_import(
    refile: Callable[[str, str], int], raw_identifier: str, resolved: str
) -> None:
    """Re-file an import orphaned under *raw_identifier* onto *resolved*'s stem.

    Inline, not on a thread: the stems are computable, so a miss is a few
    ``stat`` calls and a hit is a ``rename``/``link``, never a copy. Holds the
    *destination*'s lock — the one every markdown writer takes.
    """
    dest = manual.resolve_target(resolved)
    async with papers.sections_lock(dest["namespace"], dest["canonical"]):
        refile(raw_identifier, resolved)


async def read_markdown(
    identifier: str, scan: Callable[[str], _T]
) -> tuple[manual.Target, _T] | dict[str, Any]:
    """Resolve *identifier*, read its cached markdown, apply ``scan``.

    Returns ``(target, scan(markdown))``, else :func:`not_converted_error`. One
    home for the two reads outside ``papers.sections_lock`` (``get_paper_section``,
    ``find_in_paper``), so their guards can't drift: off the event loop, explicit
    UTF-8, ``FileNotFoundError`` degraded — a cascade can unlink mid-read.
    """
    identifier, pmid_error = await resolve_paper_identifier(identifier)
    if pmid_error is not None:
        return pmid_error

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

    One home for the arithmetic ``graph._page`` and ``get_paper_authors`` share;
    both then report ``has_more`` as ``end < total``. Envelope keys stay per-tool.
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

    Crossref returns title / container-title as single-element lists.
    """
    if isinstance(value, list):
        return value[0] if value else None
    return value


# Crossref date fallback order: `posted` is the preprint date, so it answers last.
_CROSSREF_DATE_KEYS = ("issued", "published-print", "published-online", "published", "posted")


def crossref_date(work: dict[str, Any]) -> tuple[int | None, str | None]:
    """``(year, ISO-date)`` from a Crossref work, else ``(None, None)``.

    ``{"date-parts": [[year, month, day]]}``, month and day optional. Every level
    is shape-checked, not null-checked: the ``message`` arrives verbatim, and a
    bad record degrades instead of raising past ``{error}``.
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
            "bioRxiv/medRxiv and arXiv: if the preprint names its journal version "
            "(bioRxiv's published_doi, arXiv's doi), return OpenAlex's journal record "
            "instead — _source='openalex_via_biorxiv' plus preprint_doi, or "
            "'openalex_via_arxiv' plus preprint_arxiv_id, and followed_published=True. "
            "Not-yet-indexed "
            "falls back to the preprint with followed_published=False; a "
            "retryable lookup failure also sets published_lookup_retryable=True. "
            "No effect on other shapes, unpublished preprints, or a "
            "biorxiv_unavailable answer."
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
            "DOIs sooner. Accepted by all four paper tools. The response "
            "carries _source='crossref' in the shape its OpenAlex counterpart "
            "would have; per-tool nulls are in each docstring. Only affects "
            "DOIs routed to OpenAlex. If Crossref itself fails transiently, "
            "the OpenAlex error carries crossref_fallback_retryable: true."
        ),
    ),
]

PDF_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description=(
            "Drop the cached PDF and re-download — for a corrupt file, or a "
            "provider that replaced it under the same key (a v2 arXiv upload). "
            "For an arXiv/bioRxiv/ACL-shaped identifier you imported yourself "
            "this replaces your PDF and drops its markdown: use "
            "import_paper(..., force_refresh=True) to supply the file again."
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
            "Let a DOI no native provider hosts download from the open-access "
            "PDF URL OpenAlex reports for it. Only that URL is fetched, never an "
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
            "Drop cached markdown and section index so the converter re-runs "
            "(arXiv HTML and bioRxiv JATS are fetched again). "
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
            "DEGRADED — plain text, no tables, equations, figures or headings. "
            "The response echoes conversion_mode."
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
            "Maximum hits to return. Ranked by BM25; ties break by namespace, then filename."
        ),
        ge=1,
        le=corpus.MAX_TOP_K,
    ),
]
