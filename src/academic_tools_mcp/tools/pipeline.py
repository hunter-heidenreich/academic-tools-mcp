"""PDF pipeline tools: download / convert / sections / section / import."""

import asyncio
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from .. import manual, papers
from ..app import (
    ALLOW_OA_URL,
    CONVERT_FORCE_REFRESH,
    CONVERT_MODE,
    IMPORT_FORCE_REFRESH,
    PAPER_ID,
    PDF_FORCE_REFRESH,
    SECTION_HARNESS_CAP,
    SECTION_MAX_CHARS,
    SECTION_OFFSET,
    SECTIONS_FORCE_REFRESH,
    enrich_error,
    mcp,
    not_converted_error,
    pdf_not_cached_error,
    read_markdown,
)
from ..download import openaccess, streaming
from ..providers import acl, arxiv, biorxiv

_INTERNAL_PATH_KEYS = ("path", "markdown_path")
_MARKDOWN_EXTS = {".md", ".markdown"}

# Single-homed: three failure paths offer the same escape hatch.
_IMPORT_FALLBACK = (
    "Fetch the PDF yourself, then call import_paper(file_path, identifier) with "
    "the SAME identifier — it is cached in the right namespace, so convert_paper "
    "→ get_paper_sections → get_paper_section find it. import_paper also takes "
    "pre-converted .md/.markdown, which skips convert_paper."
)


def _strip_internal_paths(result: dict[str, Any]) -> dict[str, Any]:
    """Drop cache paths: agents drive this pipeline by identifier, not by file."""
    return {k: v for k, v in result.items() if k not in _INTERNAL_PATH_KEYS}


async def _download_pdf_by_provider(
    identifier: str, *, force_refresh: bool = False, allow_oa_url: bool = False
) -> dict[str, Any]:
    """Dispatch to the provider claiming this identifier, then cascade.

    Fresh bytes (``cached is False``) invalidate the markdown and section index they
    replaced, so
    the next ``convert_paper`` re-runs. Markdown recorded ``"imported"`` is
    exempt unless ``force_refresh``: no converter can reproduce it.
    """
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
                "bioRxiv/medRxiv DOIs (10.1101/...), and ACL Anthology DOIs "
                "(10.18653/v1/...)."
            ),
            "suggestion": (
                "For a generic publisher DOI, retry with allow_oa_url=True to "
                f"fetch the open-access PDF URL OpenAlex reports (if any). {_IMPORT_FALLBACK}"
            ),
        }

    if "error" in result:
        # net/http supplies a retry verdict, never advice — the tool layer adds it.
        return enrich_error(
            result,
            "Wait and retry — the provider is temporarily unavailable."
            if result.get("retryable") is True
            else _IMPORT_FALLBACK,
        )

    if result.get("cached") is False:
        canonical = target["canonical"]
        async with papers.sections_lock(ns, canonical):
            if force_refresh or papers.recorded_conversion_mode(ns, canonical) != "imported":
                papers.drop_derived(ns, canonical)
                result["cascaded_invalidated"] = ["markdown", "sections"]

    return result


@mcp.tool
async def download_pdf(
    identifier: PAPER_ID,
    force_refresh: PDF_FORCE_REFRESH = False,
    allow_oa_url: ALLOW_OA_URL = False,
) -> dict[str, Any]:
    """Download and cache a paper's PDF, auto-detecting the source.

    Step 1 of the PDF pipeline. Direct download covers arXiv IDs, bioRxiv/medRxiv
    DOIs (10.1101/...) and ACL DOIs (10.18653/v1/...); anything else is refused
    unless ``allow_oa_url``, since this tool never fetches a caller-supplied URL.
    import_paper is the fallback that always works.

    Returns ``{size_bytes, cached}``, plus ``{anthology_id, pdf_url}`` for ACL
    papers. A real download (``cached: False``) also drops the paper's cached
    markdown and section index, reported as ``cascaded_invalidated: ["markdown",
    "sections"]`` — so never pass ``force_refresh`` to convert_paper as well.
    Markdown from import_paper survives that unless ``force_refresh=True``.

    Errors: ``{error, suggestion, retryable?, retry_after_seconds?, backpressure?,
    max_concurrency?, not_found?, max_bytes?}``.
    ``retryable: True`` is the only value meaning a retry might work.

    Next step: convert_paper → get_paper_sections → get_paper_section.
    """
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
        f"converter is an operator fix, not a retry. {_IMPORT_FALLBACK}"
    )


@mcp.tool
async def convert_paper(
    identifier: PAPER_ID,
    force_refresh: CONVERT_FORCE_REFRESH = False,
    mode: CONVERT_MODE = "full",
) -> dict[str, Any]:
    """Convert a downloaded PDF to markdown and parse it into sections.

    Step 2 of the PDF pipeline. Skips the converter when the markdown is already
    cached, re-parsing from it if the section index is missing or stale. Both
    modes write the same cache slot, so ``mode="full"`` with ``force_refresh``
    upgrades a fast conversion.

    Returns ``{sections, sections_detected, cached, conversion_mode}``, each
    section entry ``{index, title, h3s, approx_tokens}``. ``cached`` is true
    whenever the expensive conversion was skipped, re-parses included.
    ``conversion_mode`` is provenance: ``"full"`` / ``"fast"``, ``"imported"``
    (a file handed to import_paper), or null (converted before the field existed).

    Errors: ``{error, retryable, conversion_mode, pdf_size_mb?, suggestion}``,
    where ``conversion_mode`` names the mode that *failed*.
      - No usable PDF cached → ``{error, suggestion}`` only, pointing at
        download_pdf / import_paper; nothing was tried, so no ``retryable``.
      - Another conversion in flight (full mode only) → ``{busy: True,
        retryable: True, in_progress: {...}}``; retry, or use ``mode="fast"``.
      - Timeout → ``{timed_out: True, timeout_seconds}``; the suggestion points
        at the other mode, whose budget differs.
      - Anything else → non-retryable; the ``error`` string names the cause.
    """
    target = manual.resolve_target(identifier)
    pdf = target["pdf_path"]

    # Not merely absent: a 0-byte or non-%PDF- leftover is a miss too.
    if not streaming.is_usable_pdf(pdf):
        return pdf_not_cached_error(identifier)

    result = await papers.convert_pdf(
        pdf,
        target["namespace"],
        target["canonical"],
        force_refresh=force_refresh,
        mode=mode,
    )
    if "error" in result:
        return _strip_internal_paths(enrich_error(result, _convert_suggestion(result, mode)))
    return _strip_internal_paths(result)


@mcp.tool
async def get_paper_sections(
    identifier: PAPER_ID,
    force_refresh: SECTIONS_FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Get the section index for a converted paper.

    Step 3 of the PDF pipeline. Cheap — no network, no conversion — and it
    re-parses automatically when the cached markdown's checksum changed.

    Returns ``{total_sections, total_approx_tokens, sections_detected,
    conversion_mode, sections}``, each section entry ``{index, title, h3s,
    approx_tokens}`` with ``h3s`` its sub-headings. ``conversion_mode`` is
    provenance: ``"full"`` / ``"fast"``, ``"imported"`` (a file handed to
    import_paper), or null (converted before the field existed).

    ``sections_detected: false`` means the markdown had **no headings at all**,
    so the single section returned is synthetic and its title meaningless — not
    a one-section paper. A ``sections_note`` then says what to do instead.

    Errors: not yet converted → guidance to run convert_paper.
    Next step: get_paper_section(identifier, index_or_title).
    """
    target = manual.resolve_target(identifier)
    sections_data = await papers.get_or_parse_sections(
        target["namespace"], target["canonical"], force_refresh=force_refresh
    )
    if sections_data is None:
        return not_converted_error(identifier)

    # Subscripted: _reparse_sections_locked re-parses an entry missing either key.
    # Only `conversion_mode` may be null; only per-row keys are defaulted.
    sections_list = sections_data["sections"]
    detected = sections_data["sections_detected"]
    response: dict[str, Any] = {
        "total_sections": len(sections_list),
        "total_approx_tokens": sum(s.get("approx_tokens", 0) for s in sections_list),
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


@mcp.tool(meta={"anthropic/maxResultSizeChars": SECTION_HARNESS_CAP})
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

    The escape hatch for papers outside arXiv/bioRxiv/ACL: fetch the file
    yourself and pass it with the paper's DOI or arXiv ID, and the rest of the
    pipeline finds it under that identifier without re-fetching. An unrecognised
    identifier works too — the file lands in the ``manual`` namespace. A PDF is
    validated by its ``%PDF-`` header; markdown is read as UTF-8 and indexed
    immediately.

    Returns ``{identifier, namespace, cached}`` plus ``size_bytes`` for a PDF or
    ``section_count`` for markdown — call get_paper_sections for the full index.
    Landing PDF bytes over an existing file — or any ``force_refresh`` PDF import —
    also drops the paper's markdown and section index, reported as
    ``cascaded_invalidated: ["markdown", "sections"]``. ``identifier`` is the
    canonical cache key, which may differ from what you passed
    (``arXiv:2301.00001v2`` → ``2301.00001v2``).

    Errors: file not found, blank identifier, not a valid PDF, non-UTF-8
    markdown, or unsupported extension → ``{error, suggestion?}``.
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
