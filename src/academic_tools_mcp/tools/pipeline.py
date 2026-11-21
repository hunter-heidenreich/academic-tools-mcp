"""PDF pipeline tools: download / convert / sections / section / import."""

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastmcp import Context
from pydantic import Field

from .. import corpus, manual, papers
from ..app import (
    ALLOW_OA_URL,
    CACHE_SEARCH_NAMESPACE,
    CACHE_WRITE_LOCAL_ANNOTATIONS,
    CACHE_WRITE_NETWORK_ANNOTATIONS,
    CONVERT_FORCE_REFRESH,
    CONVERT_MODE,
    IMPORT_FORCE_REFRESH,
    PAGE,
    PAGE_SIZE,
    PAPER_ID,
    PDF_FORCE_REFRESH,
    READ_ONLY_LOCAL_ANNOTATIONS,
    SECTION_HARNESS_CAP,
    SECTION_MAX_CHARS,
    SECTION_OFFSET,
    SECTIONS_FORCE_REFRESH,
    enrich_error,
    mcp,
    not_converted_error,
    page_bounds,
    pdf_not_cached_error,
    read_markdown,
    resolve_paper_identifier,
)
from ..download import artifact, openaccess
from ..providers import acl, arxiv, biorxiv, openalex, pmc
from ..util import doinorm

_INTERNAL_PATH_KEYS = ("path", "markdown_path")

# Conversion modes whose markdown a new PDF did not produce, so a download keeps it.
_NOT_FROM_PDF = frozenset({"imported", "html", "jats"})

# Heartbeat interval for MCP progress notifications during long-running downloads
# and PDF conversions so clients do not hit idle tool-call timeouts.
_PROGRESS_HEARTBEAT_SECONDS = 15.0


async def _safe_report_progress(
    ctx: Context, progress: float, total: float | None = None, message: str | None = None
) -> None:
    """Emit a progress notification without letting a transport error abort the tool."""
    with contextlib.suppress(Exception):
        await ctx.report_progress(progress=progress, total=total, message=message)


@asynccontextmanager
async def _progress_heartbeat(ctx: Context | None, label: str) -> AsyncGenerator[None]:
    """Send periodic MCP progress heartbeats while a long-running operation executes."""
    if ctx is None:
        yield
        return

    started = time.monotonic()
    await _safe_report_progress(ctx, 0.0, None, f"Starting {label}...")

    async def _beat() -> None:
        while True:
            await asyncio.sleep(_PROGRESS_HEARTBEAT_SECONDS)
            elapsed = time.monotonic() - started
            await _safe_report_progress(
                ctx, round(elapsed, 1), None, f"{label} in progress ({elapsed:.0f}s elapsed)..."
            )

    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await _safe_report_progress(ctx, 1.0, 1.0, f"Finished {label}.")


def _markup_source(
    namespace: str,
    identifier: str,
    *,
    force_refresh: bool,
    has_usable_pdf: bool = False,
) -> tuple[Callable[[], Awaitable[dict[str, Any]]], papers.MarkupMode, str] | None:
    """The provider markup to try before the PDF — fetch, provenance, name — or ``None``.

    Getters are looked up at call time, so a patched provider applies.
    For generic DOIs / OpenAlex work IDs (``manual`` namespace), Europe PMC JATS
    is tried when no usable PDF is cached locally — giving open-access PMC papers
    a seconds-fast full-text path without overriding an already-downloaded or
    manually imported PDF.
    """
    if namespace == arxiv.NAMESPACE:
        return (
            lambda: arxiv.get_html(identifier, force_refresh=force_refresh),
            "html",
            "arXiv's HTML rendering",
        )
    if namespace == biorxiv.NAMESPACE:
        return (
            lambda: biorxiv.get_jats(identifier, force_refresh=force_refresh),
            "jats",
            f"{biorxiv.LABEL}'s JATS full text",
        )
    if (
        namespace == manual.NAMESPACE
        and not has_usable_pdf
        and (doinorm.looks_like_doi(identifier) or openalex.is_work_id(identifier))
    ):
        return (
            lambda: pmc.get_jats(identifier, force_refresh=force_refresh),
            "jats",
            f"{pmc.LABEL}'s JATS full text",
        )
    return None


_MARKDOWN_EXTS = {".md", ".markdown"}


def _strip_internal_paths(result: dict[str, Any]) -> dict[str, Any]:
    """Drop cache paths: agents drive this pipeline by identifier, not by file."""
    return {k: v for k, v in result.items() if k not in _INTERNAL_PATH_KEYS}


async def _download_pdf_by_provider(
    identifier: str, *, force_refresh: bool = False, allow_oa_url: bool = False
) -> dict[str, Any]:
    """Dispatch to the provider claiming this identifier, then cascade.

    Fresh bytes (``cached is False``) invalidate the markdown and section index they
    replaced, so
    the next ``convert_paper`` re-runs. Markdown that did not come from those
    bytes — ``"imported"``, ``"html"`` or ``"jats"`` — is exempt unless ``force_refresh``.

    A PMID is traded for its DOI before routing, so storage and metadata agree
    on which paper it names.
    """
    identifier, pmid_error = await resolve_paper_identifier(identifier, force_refresh=force_refresh)
    if pmid_error is not None:
        return pmid_error

    target = manual.resolve_target(identifier)
    ns = target["namespace"]

    if ns == arxiv.NAMESPACE:
        result = await arxiv.download_pdf(identifier, force_refresh=force_refresh)
    elif ns == acl.NAMESPACE:
        result = await acl.download_pdf(identifier, force_refresh=force_refresh)
    elif ns == biorxiv.NAMESPACE:
        result = await biorxiv.download_pdf(identifier, force_refresh=force_refresh)
    elif allow_oa_url:
        # Only the URL OpenAlex reports, never an arbitrary one. Lands in `manual`.
        result = await openaccess.download_pdf(identifier, force_refresh=force_refresh)
    else:
        return {
            "error": (
                f"Cannot auto-download PDF for identifier: {identifier!r}. "
                "Direct download is only supported for arXiv IDs, "
                "bioRxiv/medRxiv DOIs (10.1101/...), and ACL Anthology papers."
            ),
            "suggestion": (
                "For a generic publisher DOI or OpenAlex work ID, retry with "
                "allow_oa_url=True to fetch the open-access PDF URL OpenAlex reports "
                f"(if any). {manual.IMPORT_SUGGESTION}"
            ),
        }

    if "error" in result:
        # net/http supplies a retry verdict, never advice — the tool layer adds it.
        return enrich_error(
            result,
            "Wait and retry — the provider is temporarily unavailable."
            if result.get("retryable") is True
            else manual.IMPORT_SUGGESTION,
        )

    if result.get("cached") is False:
        canonical = target["canonical"]
        async with papers.sections_lock(ns, canonical):
            if force_refresh or papers.recorded_conversion_mode(ns, canonical) not in _NOT_FROM_PDF:
                papers.drop_derived(ns, canonical)
                result["cascaded_invalidated"] = ["markdown", "sections"]

    return result


@mcp.tool(annotations=CACHE_WRITE_NETWORK_ANNOTATIONS)
async def download_pdf(
    identifier: PAPER_ID,
    force_refresh: PDF_FORCE_REFRESH = False,
    allow_oa_url: ALLOW_OA_URL = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Download and cache a paper's PDF (step 1 of the PDF pipeline).

    Auto-downloads arXiv, bioRxiv/medRxiv, and ACL Anthology; other DOIs require
    ``allow_oa_url=True`` (fetches only OpenAlex's OA URL) or ``import_paper``.
    A fresh download drops derived markdown/sections (except imported/HTML/JATS
    unless ``force_refresh=True``).
    """
    async with _progress_heartbeat(ctx, f"PDF download for {identifier}"):
        return _strip_internal_paths(
            await _download_pdf_by_provider(
                identifier, force_refresh=force_refresh, allow_oa_url=allow_oa_url
            )
        )


def _convert_suggestion(result: dict[str, Any], mode: str) -> str:
    """Recovery advice for a conversion failure, chosen per cause.

    One message for all of them contradicts the ``error`` it rides beside, so
    the residual asserts no cause and defers to it.
    """
    if result.get("busy"):
        return (
            "Another PDF is converting. Wait and retry, read sections of papers "
            "already converted, or retry this one with mode='fast' — a degraded "
            "text-only extraction that skips the lock."
        )
    if result.get("timed_out"):
        if mode == "full":
            return (
                "Full conversion exceeded its timeout. Retry with mode='fast' for "
                "a degraded plain-text fallback, or raise PDF_CONVERT_TIMEOUT to "
                "keep the full-quality markdown."
            )
        return (
            "Fast extraction exceeded its timeout, so a fast retry hits the same "
            "wall. Retry with mode='full', whose budget is far longer, or raise "
            "PDF_FAST_CONVERT_TIMEOUT."
        )
    return (
        "Read the error above — it names what failed. A missing or misconfigured "
        f"converter is an operator fix, not a retry. {manual.IMPORT_SUGGESTION}"
    )


def _total_approx_tokens(sections: list[dict[str, Any]]) -> int:
    """Sum ``approx_tokens`` across a section index."""
    return sum(int(s.get("approx_tokens", 0)) for s in sections)


def _finish_convert(result: dict[str, Any]) -> dict[str, Any]:
    """Strip internal paths and attach ``total_approx_tokens`` to a conversion result."""
    cleaned = _strip_internal_paths(result)
    cleaned["total_approx_tokens"] = _total_approx_tokens(cleaned.get("sections", []))
    return cleaned


@mcp.tool(annotations=CACHE_WRITE_NETWORK_ANNOTATIONS)
async def convert_paper(
    identifier: PAPER_ID,
    force_refresh: CONVERT_FORCE_REFRESH = False,
    mode: CONVERT_MODE = "full",
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Convert a paper to markdown and index its sections (step 2 of the PDF pipeline).

    Tries arXiv HTML, bioRxiv JATS, or (when no PDF is cached) Europe PMC JATS
    first (seconds, no PDF needed), else converts the cached PDF. Returns
    ``{sections, total_approx_tokens, sections_detected, cached, conversion_mode}``
    — proceed straight to ``get_paper_section`` or ``get_paper_markdown``. Full
    mode is serialized server-wide (concurrent calls return ``busy: True``);
    ``mode="fast"`` is degraded plain text without the lock.
    """
    # No force_refresh: it means "re-convert", and a PMID's DOI does not go stale
    # with the markdown. Unlike download_pdf and import_paper, which do thread it.
    identifier, pmid_error = await resolve_paper_identifier(identifier)
    if pmid_error is not None:
        return pmid_error

    target = manual.resolve_target(identifier)
    pdf = target["pdf_path"]
    has_pdf = artifact.is_usable_pdf(pdf)

    markup_error: dict[str, Any] | None = None
    markup_label = ""
    source = _markup_source(
        target["namespace"],
        identifier,
        force_refresh=force_refresh,
        has_usable_pdf=has_pdf,
    )
    if source is not None:
        fetch_markup, markup_mode, markup_label = source
        converted = await papers.convert_markup(
            target["namespace"],
            target["canonical"],
            fetch_markup,
            mode=markup_mode,
            force_refresh=force_refresh,
        )
        if converted is not None and "error" not in converted:
            return _finish_convert(converted)
        markup_error = converted

    # Not merely absent: a 0-byte or non-%PDF- leftover is a miss too.
    if not has_pdf:
        if markup_error is not None:
            return _strip_internal_paths(
                enrich_error(
                    markup_error,
                    f"{markup_label} is temporarily unavailable. Retry, or run "
                    "download_pdf and then convert_paper to convert the PDF instead.",
                )
            )
        return pdf_not_cached_error(identifier)

    async with _progress_heartbeat(ctx, f"PDF conversion ({mode}) for {identifier}"):
        result = await papers.convert_pdf(
            pdf,
            target["namespace"],
            target["canonical"],
            force_refresh=force_refresh,
            mode=mode,
        )
    if "error" in result:
        return _strip_internal_paths(enrich_error(result, _convert_suggestion(result, mode)))
    return _finish_convert(result)


@mcp.tool(annotations=READ_ONLY_LOCAL_ANNOTATIONS)
async def get_paper_sections(
    identifier: PAPER_ID,
    force_refresh: SECTIONS_FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Inspect the section index of an already-converted or imported paper (local, no network).

    Returns ``{total_sections, total_approx_tokens, sections_detected,
    conversion_mode, sections}``. Optional right after ``convert_paper``, which
    already returns ``sections`` and ``total_approx_tokens``.
    """
    # No force_refresh: threading it here would put a PMID trade on the network and
    # break this tool's "no network" promise, which is the whole point of the flag.
    identifier, pmid_error = await resolve_paper_identifier(identifier)
    if pmid_error is not None:
        return pmid_error

    target = manual.resolve_target(identifier)
    sections_data = await papers.get_or_parse_sections(
        target["namespace"], target["canonical"], force_refresh=force_refresh
    )
    if sections_data is None:
        return not_converted_error(identifier)

    # Subscripted: reparse_sections_locked re-parses an entry missing either key.
    # Only `conversion_mode` may be null; only per-row keys are defaulted.
    sections_list = sections_data["sections"]
    detected = sections_data["sections_detected"]
    response: dict[str, Any] = {
        "total_sections": len(sections_list),
        "total_approx_tokens": _total_approx_tokens(sections_list),
        "sections_detected": detected,
        "conversion_mode": sections_data.get("conversion_mode"),
        "sections": sections_list,
    }
    if not detected:
        response["sections_note"] = (
            "No headings were found in the converted markdown, so the whole "
            "document is a single synthetic 'Preamble' section — this is not a "
            "one-section paper. Section titles are unavailable; use "
            "find_in_paper to locate content, or re-run convert_paper with "
            "mode='full' if this was converted with mode='fast' (the fast "
            "backend emits plain text with no headings)."
        )
    return response


@mcp.tool(
    annotations=READ_ONLY_LOCAL_ANNOTATIONS,
    meta={"anthropic/maxResultSizeChars": SECTION_HARNESS_CAP},
)
async def get_paper_section(
    identifier: PAPER_ID,
    section: Annotated[
        str,
        Field(
            description="Integer index (e.g. '0') or case-insensitive title "
            "substring (e.g. 'Introduction'). Diacritics are ignored when "
            "nothing matches exactly, so 'Resume' finds 'Résumé'. "
            "Call get_paper_sections to see the available sections."
        ),
    ],
    offset: SECTION_OFFSET = 0,
    max_chars: SECTION_MAX_CHARS = 16000,
) -> dict[str, Any]:
    """Read a slice of a section's body. Final step of the PDF pipeline.

    Returns: ``{index, title, content, offset, chars_returned, total_chars,
    approx_tokens, has_more, next_offset}``. ``total_chars`` and
    ``approx_tokens`` describe the full section, not the slice.

    Errors: not yet converted → guidance to run convert_paper. Unknown or
    ambiguous section title → error listing the available titles; an
    out-of-range index → the valid range. Markdown with no readable text →
    ``{error, suggestion, retryable: False}``.
    """
    try:
        section_key: int | str = int(section)
    except ValueError:
        section_key = section

    read = await read_markdown(
        identifier,
        lambda markdown: papers.get_section_content(
            markdown, section_key, offset=offset, max_chars=max_chars
        ),
    )
    if isinstance(read, dict):
        return read
    _, content = read
    return content


@mcp.tool(
    annotations=READ_ONLY_LOCAL_ANNOTATIONS,
    meta={"anthropic/maxResultSizeChars": SECTION_HARNESS_CAP},
)
async def get_paper_markdown(
    identifier: PAPER_ID,
    offset: SECTION_OFFSET = 0,
    max_chars: SECTION_MAX_CHARS = 100000,
) -> dict[str, Any]:
    """Read a slice of a converted paper's full markdown document.

    Alternative to get_paper_section when you want the whole paper in one call
    rather than reading section-by-section (check ``total_approx_tokens`` from
    convert_paper or get_paper_sections first; for long papers or targeted
    lookup, prefer find_in_paper and get_paper_section).

    Returns: ``{content, offset, chars_returned, total_chars, approx_tokens,
    has_more, next_offset}``. ``total_chars`` and ``approx_tokens`` describe
    the full markdown document, not the slice.

    Errors: not yet converted → guidance to run convert_paper. Offset beyond
    document length → ``{error}``. Markdown with no readable text →
    ``{error, suggestion, retryable: False}``.
    """
    read = await read_markdown(
        identifier,
        lambda markdown: papers.get_markdown_content(markdown, offset=offset, max_chars=max_chars),
    )
    if isinstance(read, dict):
        return read
    _, content = read
    return content


@mcp.tool(annotations=CACHE_WRITE_LOCAL_ANNOTATIONS)
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
    force_refresh: IMPORT_FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Import a local ``.pdf`` or ``.md``/``.markdown`` file into the paper cache.

    Routes into the provider namespace matching ``identifier`` (or ``manual``
    for freeform labels). Markdown imports skip ``convert_paper`` and are
    indexed immediately.
    """
    # Cheapest rejection first, before any routing work.
    ext = Path(file_path).suffix.lower()
    if ext != ".pdf" and ext not in _MARKDOWN_EXTS:
        return {
            "error": f"Unsupported file extension {ext!r}.",
            "suggestion": (
                "Pass a .pdf (routed through the conversion pipeline) or a "
                ".md/.markdown file (imported directly, skipping conversion). "
                "Convert or rename the file first if it is neither."
            ),
        }

    # After the extension check, so a bad suffix costs no request; before
    # resolve_target, or this writes under a key every reader trades away.
    identifier, pmid_error = await resolve_paper_identifier(identifier, force_refresh=force_refresh)
    if pmid_error is not None:
        return pmid_error

    # Synchronous and unbounded in size, so off the event loop; under
    # sections_lock, which every writer of the markdown/section-index pair takes.
    target = manual.resolve_target(identifier)
    if ext == ".pdf":
        async with papers.sections_lock(target["namespace"], target["canonical"]):
            return _strip_internal_paths(
                await asyncio.to_thread(
                    manual.import_local_pdf, file_path, identifier, force_refresh=force_refresh
                )
            )

    async with papers.sections_lock(target["namespace"], target["canonical"]):
        result = _strip_internal_paths(
            await asyncio.to_thread(
                manual.import_markdown, file_path, identifier, force_refresh=force_refresh
            )
        )
    # Slimmed on the cached hit too; get_paper_sections owns the full index.
    if "sections" in result:
        result["section_count"] = len(result.pop("sections"))
    return result


@mcp.tool(annotations=READ_ONLY_LOCAL_ANNOTATIONS)
async def list_cached_papers(
    namespace: CACHE_SEARCH_NAMESPACE = None,
    converted_only: Annotated[
        bool,
        Field(description="Only list papers that already have converted markdown."),
    ] = False,
    page: PAGE = 1,
    page_size: PAGE_SIZE = 50,
) -> dict[str, Any]:
    """Inventory downloaded PDFs and converted markdown in the local cache.

    Answers "what papers are already in my local cache?", whether each is
    downloaded-only or converted (with ``conversion_mode`` and
    ``total_approx_tokens``), and total local disk usage. Cheap — local
    filesystem scan only, no network calls.

    Returns ``{total_papers, converted_count, pdf_only_count, total_disk_bytes,
    page, page_size, has_more, papers: [{namespace, canonical_id, has_pdf,
    pdf_size_bytes, has_markdown, markdown_size_bytes, conversion_mode,
    total_approx_tokens}, ...]}``. Chain ``canonical_id`` straight into
    ``get_paper_sections``, ``get_paper_section``, ``get_paper_markdown``,
    ``find_in_paper``, or ``convert_paper``.
    """
    inventory = await asyncio.to_thread(
        corpus.list_cached, namespace, converted_only=converted_only
    )
    all_papers = inventory["papers"]
    start, end = page_bounds(page, page_size)
    return {
        "total_papers": inventory["total_papers"],
        "converted_count": inventory["converted_count"],
        "pdf_only_count": inventory["pdf_only_count"],
        "total_disk_bytes": inventory["total_disk_bytes"],
        "page": page,
        "page_size": page_size,
        "has_more": end < len(all_papers),
        "papers": all_papers[start:end],
    }


@mcp.resource("papers://cached", mime_type="application/json")
async def cached_papers_resource() -> str:
    """MCP resource exposing the local downloaded/converted paper inventory as JSON."""
    inventory = await asyncio.to_thread(corpus.list_cached)
    return json.dumps(inventory, indent=2)
