"""Shared FastMCP application core.

Holds the `mcp` instance, the lifespan, the Annotated parameter vocabulary and
the helpers the tool groups share. Never imports `tools` (CI-enforced by
`_LAYERS` in `tests/test_layering.py`), so they can import from here.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, TypeVar

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import corpus, manual, papers
from .net import clients, http
from .providers import acl, arxiv, openalex
from .store import cache, stems
from .util import doinorm
from .util.jsonshape import as_dict, as_int, dict_list

_T = TypeVar("_T")

_MIGRATION_STAMP = ".migrations_v1"


def _run_startup_sweeps() -> None:
    """Run startup cache maintenance and one-time legacy stem migrations."""
    cache.gc_orphan_tmp_files()
    cache.gc_expired_negatives()
    cache.gc_expired_positives()
    stamp = cache.CACHE_ROOT / _MIGRATION_STAMP
    if stamp.exists():
        return
    stems.migrate_legacy_stems()
    manual.migrate_misrouted_arxiv()
    manual.migrate_acl_stems()
    if cache.CACHE_ROOT.is_dir():
        with contextlib.suppress(OSError):
            stamp.write_text("1\n", encoding="utf-8")


@asynccontextmanager
async def _lifespan(app: FastMCP) -> AsyncGenerator[None]:
    """Startup sweeps (off the event loop), then shutdown cleanup.

    A sweep added here must be idempotent and must not raise out of the lifespan.
    """
    await asyncio.to_thread(_run_startup_sweeps)
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
        "A DOI the Anthology hosts answers from it. Both work everywhere a DOI does, "
        "including the graph tools — so an OpenCitations row with no `doi` is still "
        "chainable on its `openalex`. Batch many "
        "identifiers with get_papers_metadata.\n\n"
        "PDF pipeline: download_pdf → convert_paper → get_paper_section or "
        "get_paper_markdown (get_paper_sections inspects the section index of an "
        "already-converted paper; convert_paper already returns sections and "
        "total_approx_tokens). Use get_paper_markdown when total_approx_tokens is "
        "small enough to read the paper in one call, or get_paper_section / "
        "find_in_paper for targeted section reading. download_pdf "
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
        "References/citations: get_paper_references defaults to source='auto', "
        "which surveys Crossref and OpenCitations in parallel and serves page 1 "
        "from the best source (or call get_paper_references_count first). "
        "get_paper_citations defaults to source='opencitations' and also "
        "supports source='openalex' (sorted by cited_by_count or "
        "publication_date, works on landmark papers where OpenCitations 504s). "
        "Search tools return slim triage hits — chain to get_paper_metadata. "
        "search_openalex is the broad topic search and its hits are free to "
        "chain (by doi or openalex_id), except ACL Anthology papers, whose "
        "cache is Anthology-keyed; search_arxiv the field-scoped preprint one; "
        "search_crossref_by_title finds a DOI from a title, and warms the "
        "reference tools rather than get_paper_metadata. search_authors is the "
        "way into get_author when you have a person rather than one of their "
        "papers; chain its hits on openalex_id. autocomplete_openalex is the "
        "cheap way from a name to an OpenAlex ID — it is the route into "
        "get_institution, and the one OpenAlex search that spends no credit "
        "budget, though its hits are not cached. Prefer it to search_openalex "
        "when you know the name and want the identifier. search_wikipedia "
        "searches article text, not papers: use it to situate a concept, method "
        "or person, and read its `did_you_mean` before concluding a topic is "
        "absent.\n\n"
        "Papers with Code (arXiv IDs, or DOIs / PMIDs / OpenAlex work IDs resolved "
        "to arXiv via OpenAlex): get_paper_code for repositories and HF "
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

# Shared MCP ToolAnnotations presets so MCP hosts can safely auto-approve
# read-only lookups and distinguish local cache writes from network fetches.
READ_ONLY_NETWORK_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

READ_ONLY_LOCAL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

CACHE_WRITE_NETWORK_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

CACHE_WRITE_LOCAL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

PREPRINT_ID = Annotated[
    str,
    Field(
        description="Preprint identifier: an arXiv ID (bare like 2301.00001 or "
        "hep-th/9901001, arXiv:-prefixed, or an arxiv.org URL) or a "
        "bioRxiv/medRxiv DOI (10.1101/...). Other shapes (including PMIDs and "
        "OpenAlex work IDs) are refused locally without a network request."
    ),
]

DOI = Annotated[
    str,
    Field(
        description="DOI (bare, doi:, or URL), PMID, OpenAlex work ID (W...), or ACL Anthology ID/URL."
    ),
]

AUTHOR_ID = Annotated[
    str,
    Field(description="OpenAlex author ID (A...) or ORCID (bare, orcid:, or URL)."),
]

INSTITUTION_ID = Annotated[
    str,
    Field(description="OpenAlex institution ID (I...) or ROR (bare, ror:, or URL)."),
]

ARXIV_ID = Annotated[
    str,
    Field(
        description="arXiv ID (bare, arXiv:, or URL), or a DOI/PMID/OpenAlex work ID resolved to arXiv via OpenAlex."
    ),
]

PAPER_ID = Annotated[
    str,
    Field(
        description="Paper identifier: arXiv ID, bioRxiv/medRxiv DOI, ACL Anthology ID/URL, DOI, PMID, or OpenAlex work ID (W...). Pipeline tools also accept freeform import labels."
    ),
]

# The harness's per-result cap: get_paper_section and get_paper_markdown declare
# it as `anthropic/maxResultSizeChars` and SECTION_MAX_CHARS caps requests at it.
SECTION_HARNESS_CAP = 200000

SECTION_OFFSET = Annotated[
    int,
    Field(description="Start character offset (pass next_offset to page).", ge=0),
]

SECTION_MAX_CHARS = Annotated[
    int,
    Field(description="Max characters to return (~4 chars/token).", ge=1, le=SECTION_HARNESS_CAP),
]


# A publication-year filter, bounded generously rather than precisely: the job is
# to reject a negative, a zero or a pasted identifier before it costs an upstream
# 400, not to claim the real range of either index.
SEARCH_YEAR_MIN = 1000
SEARCH_YEAR_MAX = 2100


# Single-homed: every "run the pipeline first" suggestion quotes this chain.
PIPELINE_CHAIN = (
    "download_pdf → convert_paper → get_paper_sections → get_paper_section (or get_paper_markdown)"
)


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

    The shapes are disjoint: a PMID is traded once (to its DOI or OpenAlex work ID)
    and never re-traded as a work ID.
    """
    if openalex.is_pmid(identifier):
        return await resolve_pmid_identifier(identifier, force_refresh=force_refresh)
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


async def _resolve_openalex_trade(
    identifier: str,
    *,
    resolved: dict[str, Any],
    refile: Callable[[str, str], int],
    error_suggestion: str,
    missing_label: Callable[[Any], str],
) -> tuple[str, dict[str, Any] | None]:
    """Complete a PMID or OpenAlex work-ID trade from an ``openalex.resolve_*`` result."""
    if "error" in resolved:
        return identifier, enrich_error(resolved, error_suggestion)

    doi = resolved.get("doi")
    if not doi:
        raw_id = resolved.get("openalex_id")
        if isinstance(raw_id, str) and openalex.is_work_id(
            bare_work_id := openalex.normalize_work_id(raw_id)
        ):
            await _repair_import(refile, identifier, bare_work_id)
            return bare_work_id, None
        return identifier, {
            **http.not_found(
                f"OpenAlex indexes {missing_label(raw_id)} without a "
                "DOI or usable OpenAlex work ID."
            ),
            "suggestion": (
                "Fetch the PDF yourself and call import_paper with a label of "
                "your choosing to read the full text."
            ),
        }

    await _repair_import(refile, identifier, doi)
    return doi, None


async def resolve_pmid_identifier(
    identifier: str, *, force_refresh: bool = False
) -> tuple[str, dict[str, Any] | None]:
    """Trade a PMID for its DOI; pass every other identifier through untouched.

    Returns ``(identifier, None)`` or ``(identifier, error)``.
    """
    if not openalex.is_pmid(identifier):
        return identifier, None

    return await _resolve_openalex_trade(
        identifier,
        resolved=await openalex.resolve_pmid(identifier, force_refresh=force_refresh),
        refile=manual.refile_pmid_stems,
        error_suggestion=(
            "Check the PMID on pubmed.ncbi.nlm.nih.gov, or pass the paper's DOI "
            "directly. Only papers OpenAlex has indexed resolve by PMID."
        ),
        missing_label=lambda raw_id: f"PMID {openalex.normalize_pmid(identifier)} ({raw_id})",
    )


async def resolve_work_id_identifier(
    identifier: str, *, force_refresh: bool = False
) -> tuple[str, dict[str, Any] | None]:
    """Trade an OpenAlex work ID for its DOI (or normalized work ID when DOI-less).

    Returns ``(identifier, None)`` or ``(identifier, error)``. Sibling of
    :func:`resolve_pmid_identifier`, kept separate so each trade's error names the
    identifier the caller actually passed.
    """
    if not openalex.is_work_id(identifier):
        return identifier, None

    return await _resolve_openalex_trade(
        identifier,
        resolved=await openalex.resolve_work_id(identifier, force_refresh=force_refresh),
        refile=manual.refile_work_id_stems,
        error_suggestion=(
            "Check the ID on openalex.org, or pass the paper's DOI directly. Only "
            "works OpenAlex still indexes under that ID resolve."
        ),
        missing_label=lambda _raw_id: openalex.normalize_work_id(identifier),
    )


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
    if openalex.is_work_id(doi):
        return doi, {
            **http.not_found(f"OpenAlex indexes {doi} without a DOI. {subject} are DOI-only."),
            "suggestion": (
                "Metadata, author, abstract, BibTeX, and OA-download tools accept "
                f"{doi} directly; for references, read the paper's full text via "
                "download_pdf(..., allow_oa_url=True) or import_paper."
            ),
        }
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
    home for the reads outside ``papers.sections_lock`` (``get_paper_section``,
    ``get_paper_markdown``, ``find_in_paper``), so their guards can't drift: off
    the event loop, explicit UTF-8, ``OSError`` and ``UnicodeDecodeError``
    degraded — a cascade can unlink mid-read, and a hand-edited file can fail
    UTF-8 decoding.
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
    except (OSError, UnicodeDecodeError):
        return not_converted_error(identifier)


def page_bounds(page: int, page_size: int) -> tuple[int, int]:
    """Half-open slice bounds for a 1-based ``page``.

    One home for the arithmetic ``graph._page`` and ``get_paper_authors`` share;
    both then report ``has_more`` as ``end < total``. Envelope keys stay per-tool.
    """
    start = (page - 1) * page_size
    return start, start + page_size


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
        if isinstance(first, list) and first and (year := as_int(first[0])) is not None:
            iso = f"{year:04d}"
            if len(first) >= 2 and (month := as_int(first[1])) is not None:
                iso += f"-{month:02d}"
                if len(first) >= 3 and (day := as_int(first[2])) is not None:
                    iso += f"-{day:02d}"
            return year, iso
    return None, None


def openalex_pmid(work: dict[str, Any]) -> str | None:
    """The work's PMID as bare digits, or ``None``.

    OpenAlex spells it as a PubMed URL; normalized here so the server hands back
    a spelling it also accepts. ``isdecimal``, not ``is_pmid``: that predicate's
    floor on a bare run is a routing tier, and short PMIDs on early papers are
    real.
    """
    raw = as_dict(work.get("ids")).get("pmid")
    if not isinstance(raw, str) or not raw:
        return None
    normalized = openalex.normalize_pmid(raw)
    return normalized if normalized.isdecimal() else None


def openalex_arxiv_id(work: dict[str, Any]) -> str | None:
    """The work's versionless arXiv ID, or ``None``.

    OpenAlex records an arXiv preprint in ``ids.arxiv`` when indexed, or in a
    location's landing page, PDF URL, or DataCite ``10.48550/arXiv.*`` DOI when
    merged under the published work. Normalized to the bare versionless form
    (preserving old-style archive case like ``math.GT/0309136``) that every
    arXiv-keyed tool accepts.
    """
    locations = (
        as_dict(work.get("primary_location")),
        as_dict(work.get("best_oa_location")),
        *dict_list(work.get("locations")),
    )
    candidates = (
        as_dict(work.get("ids")).get("arxiv"),
        work.get("doi"),
        *(loc.get(key) for loc in locations for key in ("landing_page_url", "pdf_url", "doi")),
    )
    for raw in candidates:
        if isinstance(raw, str) and arxiv.is_arxiv_id(raw):
            return arxiv.strip_version(arxiv.normalize_arxiv_id(raw))
    return None


FOLLOW_PUBLISHED = Annotated[
    bool,
    Field(
        description="For arXiv/bioRxiv preprints, follow to OpenAlex's journal version when known."
    ),
]

FORCE_REFRESH = Annotated[
    bool,
    Field(description="Bypass the local cache and re-fetch from upstream."),
]

FALLBACK_CROSSREF = Annotated[
    bool,
    Field(description="On a definitive OpenAlex 404 for a DOI, fall back to Crossref."),
]

PDF_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description="Re-download the PDF even if cached (invalidates derived markdown/sections)."
    ),
]

IMPORT_FORCE_REFRESH = Annotated[
    bool,
    Field(
        description="Overwrite any cached copy for this identifier (drops derived markdown for PDFs)."
    ),
]

ALLOW_OA_URL = Annotated[
    bool,
    Field(
        description="For non-native DOIs/PMIDs/work IDs, allow fetching the OA PDF URL OpenAlex reports."
    ),
]

CONVERT_FORCE_REFRESH = Annotated[
    bool,
    Field(description="Drop cached markdown/sections and re-run conversion."),
]

CONVERT_MODE = Annotated[
    Literal["full", "fast"],
    Field(
        description="'full' (default, slow, serialized, keeps tables/equations) or 'fast' (plain text, no lock)."
    ),
]

SECTIONS_FORCE_REFRESH = Annotated[
    bool,
    Field(description="Re-parse cached markdown into sections (local, no network)."),
]

AUTHORS_PAGE = Annotated[
    int,
    Field(description="1-based page number.", ge=1),
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
    Field(description="1-based page number.", ge=1),
]


REF_SOURCE = Annotated[
    Literal["auto", "crossref", "opencitations"],
    Field(
        description="'auto' (default, surveys both on page 1), 'crossref' (structured metadata), or 'opencitations' (DOI links)."
    ),
]

CITATION_SOURCE = Annotated[
    Literal["opencitations", "openalex"],
    Field(
        description="'opencitations' (default) returns DOI-to-DOI links with "
        "self-citation flags. 'openalex' pages citing works via OpenAlex with "
        "full triage metadata (title, year, cited_by_count, DOI/arXiv/PMID), "
        "supports sorting, and works for landmark papers where OpenCitations 504s."
    ),
]

CITATION_SORT = Annotated[
    openalex.CitationSort,
    Field(
        description="Sort order when source='openalex': 'cited_by_count' (default, "
        "most-cited first) or 'publication_date' (newest first). Ignored for "
        "source='opencitations'."
    ),
]

FIND_MAX_RESULTS = Annotated[
    int,
    Field(description="Max matches to return in document order.", ge=1, le=100),
]

CACHE_SEARCH_NAMESPACE = Annotated[
    str | None,
    Field(description="Optional cache namespace filter (arxiv, biorxiv, acl_anthology, manual)."),
]

CACHE_SEARCH_TOP_K = Annotated[
    int,
    Field(description="Max BM25 hits to return.", ge=1, le=corpus.MAX_TOP_K),
]
