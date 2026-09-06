"""PDF pipeline tools: download / convert / sections / section / import."""

import asyncio
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from .. import _pdf_download, manual, oa_download, papers
from .._app import (
    _SECTION_HARNESS_CAP,
    ALLOW_OA_URL,
    CONVERT_FORCE_REFRESH,
    CONVERT_MODE,
    IMPORT_FORCE_REFRESH,
    PAPER_ID,
    PDF_FORCE_REFRESH,
    SECTION_MAX_CHARS,
    SECTION_OFFSET,
    SECTIONS_FORCE_REFRESH,
    _enrich_error,
    mcp,
    not_converted_error,
    pdf_not_cached_error,
)
from ..providers import acl_anthology, arxiv, biorxiv

_INTERNAL_PATH_KEYS = ("path", "markdown_path")


def _strip_internal_paths(result: dict[str, Any]) -> dict[str, Any]:
    """Drop cache filesystem paths before returning to the agent.

    The agent should drive the pipeline by identifier; exposing on-disk
    paths tempts it to read files directly instead of using the tools.
    """
    if not isinstance(result, dict):
        return result
    return {k: v for k, v in result.items() if k not in _INTERNAL_PATH_KEYS}


async def _download_pdf_by_provider(
    identifier: str, *, force_refresh: bool = False, allow_oa_url: bool = False
) -> dict[str, Any]:
    """Dispatch PDF download to the correct provider based on identifier type.

    When ``force_refresh=True`` causes a real re-download (``cached``
    comes back ``False``), the cached markdown + section index for this
    paper become stale relative to the new bytes on disk. Drop them
    here so the next ``convert_paper`` picks up the replacement file
    instead of returning previously-converted text — without this
    cascade an agent that re-downloads has to remember to also
    ``convert_paper(force_refresh=True)`` or quietly read stale text.
    """
    target = manual.resolve_target(identifier)
    ns = target["namespace"]

    if ns == "arxiv":
        result = await arxiv.download_pdf(identifier, force_refresh=force_refresh)
    elif ns == "acl_anthology":
        result = await acl_anthology.download_pdf(identifier, force_refresh=force_refresh)
    elif ns == "biorxiv":
        result = await biorxiv.download_pdf(identifier, force_refresh=force_refresh)
    elif allow_oa_url:
        # Generic publisher DOI + opt-in: fetch the open-access PDF URL
        # OpenAlex reports for this DOI (never an arbitrary URL). Lands in
        # the manual namespace, so the force_refresh cascade below and the
        # rest of the pipeline treat it like any other manual-namespace PDF.
        result = await oa_download.download_pdf(identifier, force_refresh=force_refresh)
    else:
        return {
            "error": (
                f"Cannot auto-download PDF for identifier: {identifier!r}. "
                "Direct download is only supported for arXiv IDs, "
                "bioRxiv/medRxiv DOIs (10.1101/...), and ACL Anthology DOIs "
                "(10.18653/v1/...)."
            ),
            "suggestion": (
                "For a generic publisher DOI, retry with allow_oa_url=True to "
                "fetch the open-access PDF URL OpenAlex reports (if any). "
                "Otherwise obtain the PDF yourself (publisher site, "
                "institutional access, browser, curl, etc.), then call "
                "import_paper(file_path, identifier) with the SAME identifier "
                "— it will be cached in the correct namespace so convert_paper "
                "→ get_paper_sections → get_paper_section find it. import_paper "
                "also accepts pre-converted .md/.markdown files, which skip the "
                "convert_paper step entirely."
            ),
        }

    # Cascade only on a real re-download. Every provider download_pdf returns an
    # explicit cached flag, so `cached is False` distinguishes a fresh fetch from
    # a cache hit (cached True) or a failure (handled by the "error" guard).
    if force_refresh and "error" not in result and result.get("cached") is False:
        canonical = target["canonical"]
        # Under the per-paper lock so a concurrent convert_pdf can't read a
        # half-cleared state.
        async with papers.sections_lock(ns, canonical):
            papers.drop_derived(ns, canonical)
        result["cascaded_invalidated"] = ["markdown", "sections"]

    return result


@mcp.tool
async def download_pdf(
    identifier: PAPER_ID,
    force_refresh: PDF_FORCE_REFRESH = False,
    allow_oa_url: ALLOW_OA_URL = False,
) -> dict[str, Any]:
    """Download and cache the PDF for a paper, auto-detecting the source.

    Direct download is supported for three providers:
      - arXiv IDs (e.g. 2301.00001)
      - bioRxiv/medRxiv DOIs (10.1101/...)
      - ACL Anthology DOIs (10.18653/v1/...)

    Any other identifier (generic publisher DOI, freeform label, etc.)
    returns an error by default — this tool will NOT fetch arbitrary URLs.
    For a generic publisher DOI you can opt in with ``allow_oa_url=True``:
    the tool then fetches ONLY the open-access PDF URL that OpenAlex
    reports for that DOI (gold/hybrid/green OA). It still never fetches a
    caller-supplied URL, and it errors cleanly if the paper is
    closed-access, isn't in OpenAlex, or the URL turns out to be a landing
    page rather than a PDF — fall back to import_paper in those cases.
    Obtaining the file yourself and passing it to
    import_paper(file_path, identifier) with the same identifier always
    works and deduplicates with the rest of the pipeline.

    Skips download if already cached unless ``force_refresh=True``.
    ``force_refresh=True`` **cascades**: when the PDF is actually
    re-downloaded, the cached markdown and section index for that paper are
    dropped automatically, so the next ``convert_paper`` picks up the new
    bytes. You do not need to pass ``force_refresh`` to ``convert_paper`` as
    well. The response reports this as
    ``cascaded_invalidated: ["markdown", "sections"]``.

    Next step: convert_paper → get_paper_sections → get_paper_section.
    """
    return _strip_internal_paths(
        await _download_pdf_by_provider(
            identifier, force_refresh=force_refresh, allow_oa_url=allow_oa_url
        )
    )


@mcp.tool
async def convert_paper(
    identifier: PAPER_ID,
    force_refresh: CONVERT_FORCE_REFRESH = False,
    mode: CONVERT_MODE = "full",
) -> dict[str, Any]:
    """Convert a downloaded PDF to markdown and parse into sections.

    Step 2 of the PDF pipeline (download_pdf → convert_paper →
    get_paper_sections → get_paper_section). Skips the subprocess if the
    markdown is already cached — re-parses from the cached markdown if the
    sections index is missing or stale. ``force_refresh=True`` drops both the
    cached markdown and the section index so the converter re-runs.

    ``mode="full"`` (default): heavy converter (MinerU/Marker), high quality
    (tables/equations), but slow (up to 10 minutes, hard timeout) and
    serialised — only one full conversion runs server-wide at a time.
    ``mode="fast"``: lightweight text extractor (pdftotext/pymupdf), runs
    *outside* that lock — seconds, never ``busy`` — but DEGRADED (plain text,
    no tables/equations/figures/headings). Reach for it when ``full`` times
    out, the heavy converter isn't installed, or you just need searchable
    text. Both modes write the same cache slot; a later ``force_refresh``
    full conversion upgrades a fast one.

    Returns ``{sections, sections_detected, cached, conversion_mode}`` on
    success. ``cached`` is true when the expensive conversion was skipped
    (re-parses also count as cached). ``conversion_mode`` records what produced
    the markdown: ``"full"`` or ``"fast"`` for a conversion, ``"imported"`` for
    a pre-converted file handed to import_paper, or null for a paper converted
    before the field existed. Each section entry has
    ``{index, title, h3s, approx_tokens}``.

    Errors: ``{error, retryable, pdf_size_mb?, suggestion}``.
      - PDF not cached → suggestion points at download_pdf / import_paper.
      - Server already running another conversion (full mode only) →
        ``{busy: True, retryable: True, in_progress: {...}}``. Retry shortly,
        or call again with ``mode="fast"`` (it doesn't take the lock).
      - Conversion failure (subprocess error, timeout, no output) →
        non-retryable. On a full-mode timeout the suggestion points at
        ``mode="fast"``.
    """
    target = manual.resolve_target(identifier)
    pdf = target["pdf_path"]

    if not _pdf_download.is_usable_pdf(pdf):
        # Not just "absent": a 0-byte or non-%PDF- leftover must be treated
        # as a miss too, rather than handed to the converter.
        return pdf_not_cached_error(identifier)

    result = await papers.convert_pdf(
        pdf,
        target["namespace"],
        target["canonical"],
        force_refresh=force_refresh,
        mode=mode,
    )
    if "error" in result:
        # Error responses cross the same MCP boundary as success ones — strip
        # cache filesystem paths here too so a future error shape that happens
        # to carry one can't leak it to the agent.
        if result.get("busy"):
            return _strip_internal_paths(
                _enrich_error(
                    result,
                    "Another PDF is being converted right now. Wait and retry; "
                    "in the meantime you can still read sections of papers that "
                    "are already converted, retry this one with mode='fast' (a "
                    "quick degraded text-only extraction that skips the lock), or "
                    "work on non-PDF tools.",
                )
            )
        if result.get("timed_out") and mode == "full":
            return _strip_internal_paths(
                _enrich_error(
                    result,
                    "Full conversion exceeded the timeout. For a quick degraded "
                    "fallback, retry with mode='fast' (plain-text extraction, no "
                    "tables/equations) — or raise PDF_CONVERT_TIMEOUT if you need "
                    "the full-quality markdown.",
                )
            )
        return _strip_internal_paths(
            _enrich_error(
                result,
                "Conversion failed permanently — do not retry. "
                "The PDF may be too large, corrupted, or in an unsupported format. "
                "Try importing a different version or pre-converted markdown via import_paper.",
            )
        )
    return _strip_internal_paths(result)


@mcp.tool
async def get_paper_sections(
    identifier: PAPER_ID,
    force_refresh: SECTIONS_FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Get the section index for a converted paper.

    Step 3 of the PDF pipeline. Cheap to call (no network, no conversion).
    Auto re-parses if the cached markdown's checksum changed;
    ``force_refresh=True`` drops the section index unconditionally so
    the next read re-parses the markdown.

    Returns ``{total_sections, total_approx_tokens, sections_detected,
    sections}`` where each section entry has ``{index, title, h3s,
    approx_tokens}`` — ``h3s`` is the list of sub-headings under that section.

    ``sections_detected: false`` means the converted markdown had **no
    headings at all**, so the single section returned is synthetic and its
    title carries no meaning. A ``sections_note`` then explains what to do
    instead. Do not read that case as "this paper has one section".

    Errors: not yet converted → guidance to run convert_paper.
    Next step: get_paper_section(identifier, index_or_title).
    """
    target = manual.resolve_target(identifier)
    sections_data = await papers.get_or_parse_sections(
        target["namespace"], target["canonical"], force_refresh=force_refresh
    )
    if sections_data is None:
        return not_converted_error(identifier)

    sections_list = sections_data.get("sections", [])
    # Always recorded: ``papers._reparse_sections_locked`` treats an entry
    # without this key as stale and re-parses, so a cached index predating the
    # flag yields the real answer rather than an optimistic default.
    detected = sections_data["sections_detected"]
    response: dict[str, Any] = {
        "total_sections": len(sections_list),
        "total_approx_tokens": sum(s.get("approx_tokens", 0) for s in sections_list),
        "sections_detected": detected,
        "sections": sections_list,
    }
    if not detected:
        # Without this an agent cannot tell "this paper has one section" from
        # "no headings were found, so the whole document is one synthetic
        # Preamble". The distinction matters most on the largest documents —
        # every 100 KB+ single-section paper in a real corpus was this case,
        # theses where blind paging is the worst possible reading strategy.
        response["sections_note"] = (
            "No headings were found in the converted markdown, so the whole "
            "document is a single synthetic 'Preamble' section — this is not a "
            "one-section paper. Section titles are unavailable; use "
            "find_in_paper to locate content, or re-run convert_paper with "
            "mode='full' if this was converted with mode='fast' (the fast "
            "backend emits plain text with no headings)."
        )
    return response


@mcp.tool(meta={"anthropic/maxResultSizeChars": _SECTION_HARNESS_CAP})
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
    ``approx_tokens`` describe the full section, not the slice. When
    ``has_more`` is true, call again with ``offset=next_offset`` to continue.

    Errors: not yet converted → guidance to run convert_paper. Unknown or
    ambiguous section title → error listing the available titles.
    """
    target = manual.resolve_target(identifier)
    md_path = papers.markdown_path(target["namespace"], target["canonical"])

    if not md_path.exists():
        return not_converted_error(identifier)

    try:
        section_key: int | str = int(section)
    except ValueError:
        section_key = section

    # Read + slice off the event loop (like find_in_paper). Read UTF-8
    # explicitly so a non-UTF-8 host locale can't mis-decode the cached
    # markdown, and degrade to the clean "not converted" error if the file was
    # unlinked by a concurrent force_refresh cascade between the exists() check
    # and the read rather than letting FileNotFoundError escape.
    def _read_and_extract() -> dict[str, Any]:
        markdown = md_path.read_text(encoding="utf-8")
        return papers.get_section_content(markdown, section_key, offset=offset, max_chars=max_chars)

    try:
        return await asyncio.to_thread(_read_and_extract)
    except FileNotFoundError:
        return not_converted_error(identifier)


_MARKDOWN_EXTS = {".md", ".markdown"}


@mcp.tool
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
    """Import a local PDF or pre-converted markdown into the cache.

    For papers outside arXiv/bioRxiv/ACL: fetch the file yourself, then
    call this with the paper's DOI / arXiv ID as the identifier. The same
    identifier deduplicates with the rest of the pipeline so a later
    download_pdf or convert_paper finds it without re-fetching. Unrecognised
    identifiers still work — the file lands in a ``manual`` namespace and
    the rest of the pipeline keys off the same identifier.

    File type is detected by extension:
      - .pdf → validated via %PDF- header, then cached for convert_paper →
        get_paper_sections → get_paper_section.
      - .md / .markdown → read as UTF-8, cached, and parsed into sections
        immediately; skip convert_paper.

    Already-cached identifiers return ``cached: True`` untouched; pass
    ``force_refresh=True`` to replace a cached PDF/markdown (e.g. a corrected
    file or a better manual conversion). Replacing a PDF cascades — the cached
    markdown + section index are dropped so the next convert_paper re-runs.

    Returns ``{identifier, namespace, size_bytes, cached}`` for PDFs, or
    ``{identifier, namespace, section_count, cached}`` for markdown — call
    get_paper_sections for the full section index with previews.
    ``identifier`` is the canonical cache key the file was filed under, which
    may differ from what you passed (``arXiv:2301.00001v2`` → ``2301.00001v2``).

    Errors: file not found, blank identifier, not a valid PDF, non-UTF-8
    markdown, or unsupported extension → ``{error}``.
    """
    ext = Path(file_path).suffix.lower()

    # Both import paths run off the event loop. import_local_pdf copies an
    # arbitrarily large file through atomic.copy (MAX_PDF_BYTES bounds
    # downloads, not local imports), and import_markdown reads and parses an
    # arbitrarily large document — either
    # would stall every concurrent tool call for the duration. The manual
    # functions stay synchronous so their direct callers and tests are
    # unaffected; the boundary is here, matching get_paper_section and
    # find_in_paper.
    # Invariant: both branches hold ``papers.sections_lock`` across the write.
    # Each replaces the markdown / section-index pair that convert_pdf and the
    # force_refresh cascade mutate under the same lock — the PDF branch via
    # papers.drop_derived, which unlinks the markdown. Without it a
    # concurrent reader can see a half-replaced state, or lose the file between
    # its exists() check and its read.
    target = manual.resolve_target(identifier)
    if ext == ".pdf":
        async with papers.sections_lock(target["namespace"], target["canonical"]):
            return _strip_internal_paths(
                await asyncio.to_thread(
                    manual.import_local_pdf, file_path, identifier, force_refresh=force_refresh
                )
            )
    if ext in _MARKDOWN_EXTS:
        async with papers.sections_lock(target["namespace"], target["canonical"]):
            result = _strip_internal_paths(
                await asyncio.to_thread(
                    manual.import_markdown, file_path, identifier, force_refresh=force_refresh
                )
            )
        if "sections" in result:
            sections = result.pop("sections")
            result["section_count"] = len(sections)
        return result
    return {
        "error": (
            f"Unsupported file extension {ext!r}. "
            "Expected .pdf (for the PDF pipeline) or .md/.markdown (for "
            "pre-converted text)."
        ),
    }
