"""PDF-to-markdown conversion and section-level access.

This module handles:
  - Running a configurable PDF converter (MinerU, Marker, or custom) to produce markdown
  - Parsing markdown into sections with sub-heading previews
  - Retrieving individual sections by title or index
  - Automatic cache invalidation when markdown changes (via checksum)

The converter backend is configured via PDF_CONVERTER and PDF_CONVERTER_VENV
environment variables. See _CONVERTERS for named backends.

Section splitting is fixed, not adaptive: H1 and H2 are both treated as
section boundaries (different converters use different conventions for the
top level), H3 is tracked as the sub-heading level, and H4+ are ignored.

Cache invalidation: section indices are checksummed against the source markdown.
If the markdown file changes (e.g., manual edits), the sections are re-parsed
on the next call.
"""

import asyncio
import hashlib
import os
import re
import shlex
import shutil
import signal
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import _textnorm, cache, config

# Default subprocess timeout for PDF→markdown conversion. Big PDFs on
# CPU-only MinerU runs can legitimately take 20+ minutes, so we err
# generous. Tunable via PDF_CONVERT_TIMEOUT (seconds); 0/empty disables.
_DEFAULT_PDF_CONVERT_TIMEOUT = 1800.0

# Default timeout for the lightweight "fast" extraction path. Text-only
# extraction is seconds, not minutes, so the ceiling is tight. Tunable via
# PDF_FAST_CONVERT_TIMEOUT (seconds); 0/empty disables.
_DEFAULT_FAST_CONVERT_TIMEOUT = 120.0

# Global cap: at most one PDF→markdown conversion runs across the whole
# server at a time. Conversion can pin a CPU/GPU for tens of minutes;
# running multiple in parallel just thrashes resources. A second caller
# that arrives while one is already running gets a structured "busy"
# error and is expected to retry later — we deliberately do NOT queue,
# because a caller that wanted to wait could have done so itself.
_global_convert_lock = asyncio.Lock()
_current_conversion: dict[str, Any] | None = None

# Approximate tokens per character (conservative estimate for English text)
_CHARS_PER_TOKEN = 4

# Regex for heading lines: captures (level, title)
#   "# Foo"   -> (1, "Foo")
#   "## Bar"  -> (2, "Bar")
#   "### Baz" -> (3, "Baz")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")

# Built-in converter command templates.
# {input} = PDF path, {output_dir} = temp extraction directory.
_CONVERTERS: dict[str, str] = {
    "mineru": 'mineru -p "{input}" -o "{output_dir}"',
    "marker": 'marker_single "{input}" --output_dir "{output_dir}"',
}

# Built-in lightweight ("fast") extractor command templates. Unlike the heavy
# converters above, these emit extracted text to *stdout* (not an output dir)
# and produce plain text, not structured markdown — a deliberately degraded
# fallback. {input} = PDF path, {python} = the server's own interpreter (so the
# bundled pymupdf runner resolves against the env where the optional `[fast]`
# extra is installed).
_FAST_CONVERTERS: dict[str, str] = {
    "pdftotext": 'pdftotext -layout "{input}" -',
    "pymupdf": '{python} -m academic_tools_mcp._fast_extract "{input}"',
}


def _busy_error(pdf_size_mb: float) -> dict[str, Any]:
    """Build the response for a caller that hit the global conversion gate.

    Tells the caller what is currently running and how long it has been
    going so an agent can decide whether to back off briefly or move on.

    The holder mutates the ``_current_conversion`` global from inside the
    lock; a follower reads it without the lock. That's safe by design:
    the read is a single atomic Python load (GIL-protected), the value
    is either a fully-populated dict or ``None``, and ``or {}`` plus
    ``.get(..., default)`` cover the cleared-but-still-locked window
    (holder finished, cleared the global, hasn't released the lock yet).
    Worst case the response says "unknown/unknown, 0s" instead of the
    just-finished work — never a crash, never a partial read.
    """
    snapshot = _current_conversion or {}
    started_at = snapshot.get("started_at")
    elapsed = (time.monotonic() - started_at) if started_at is not None else 0.0
    canonical = snapshot.get("canonical", "unknown")
    namespace = snapshot.get("namespace", "unknown")
    return {
        "error": (
            f"PDF conversion already in progress for {namespace}/{canonical} "
            f"({elapsed:.0f}s elapsed). The server runs at most one conversion "
            "at a time; retry shortly."
        ),
        "retryable": True,
        "busy": True,
        "in_progress": {
            "namespace": namespace,
            "canonical": canonical,
            "elapsed_seconds": round(elapsed, 1),
        },
        "pdf_size_mb": round(pdf_size_mb, 1),
    }


def _resolve_timeout(env_var: str, default: float) -> float | None:
    """Resolve a subprocess timeout from an env var.

    Returns the timeout in seconds, or None to disable the timeout.
    Unset / empty / "0" / negative / non-numeric values are treated as
    "use the default"; an explicit "none" / "off" / "disabled" disables.
    """
    raw = config.get(env_var)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in {"none", "off", "disabled", "0"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        return default
    if value <= 0:
        return None
    return value


def _resolve_convert_timeout() -> float | None:
    """Resolve the full PDF conversion timeout from PDF_CONVERT_TIMEOUT."""
    return _resolve_timeout("PDF_CONVERT_TIMEOUT", _DEFAULT_PDF_CONVERT_TIMEOUT)


def _resolve_fast_convert_timeout() -> float | None:
    """Resolve the fast-extraction timeout from PDF_FAST_CONVERT_TIMEOUT."""
    return _resolve_timeout("PDF_FAST_CONVERT_TIMEOUT", _DEFAULT_FAST_CONVERT_TIMEOUT)


def _build_converter_command(pdf_path: Path, output_dir: Path) -> str:
    """Build the shell command for PDF-to-markdown conversion.

    Reads PDF_CONVERTER and PDF_CONVERTER_VENV from environment.
    PDF_CONVERTER can be a named backend ("mineru", "marker") or a custom
    command template containing {input} and {output_dir} placeholders.
    PDF_CONVERTER_VENV is an optional path to a virtualenv to activate first.
    """
    converter = config.get("PDF_CONVERTER") or "mineru"

    # Named backend or custom command template
    template = _CONVERTERS.get(converter, converter)
    cmd = template.format(input=pdf_path, output_dir=output_dir)

    # Optionally activate a venv before running
    venv = config.get("PDF_CONVERTER_VENV")
    if venv:
        activate = Path(venv).expanduser() / "bin" / "activate"
        cmd = f'source "{activate}" && {cmd}'

    return cmd


def _build_fast_converter_command(pdf_path: Path) -> str:
    """Build the shell command for lightweight ("fast") text extraction.

    Reads PDF_FAST_CONVERTER from environment. It can be a named backend
    ("pdftotext" — the default — or "pymupdf") or a custom command template
    containing an {input} placeholder. The command MUST emit the extracted
    text to stdout. {python} expands to the server's own interpreter so the
    bundled pymupdf runner resolves against the env where the optional
    `[fast]` extra is installed.
    """
    converter = config.get("PDF_FAST_CONVERTER") or "pdftotext"
    template = _FAST_CONVERTERS.get(converter, converter)
    # str.format ignores the unused {python} key for templates (e.g. pdftotext)
    # that don't reference it.
    return template.format(input=pdf_path, python=shlex.quote(sys.executable))


def _markdown_checksum(md_path: Path) -> str:
    """Compute SHA-256 hex digest of a markdown file.

    Used for cache invalidation — if the markdown changes, sections must be re-parsed.
    Returns empty string if the file doesn't exist.
    """
    if not md_path.exists():
        return ""
    return hashlib.sha256(md_path.read_bytes()).hexdigest()


def _markdown_path(namespace: str, canonical: str) -> Path:
    """Return the cache path for converted markdown."""
    return cache._cache_dir(namespace, "markdown") / (canonical.replace("/", "_") + ".md")


def _sections_key(canonical: str) -> str:
    """Cache key for section index JSON."""
    return canonical.replace("/", "_")


# Per-paper async lock so two concurrent reads of the same paper don't both
# re-parse the markdown and race to write the sections cache. We cap the
# dict at ``_SECTION_LOCKS_MAX`` and evict the oldest entries (FIFO via
# OrderedDict.move_to_end on touch) so a long-running session that touches
# thousands of papers doesn't slowly grow this map without bound. Eviction
# only drops locks that are not currently held — a held lock means a
# coroutine is mid-section-cache write and dropping it would let a racing
# caller skip the serialisation we depend on.
_SECTION_LOCKS_MAX: int = 1024
_section_locks: "OrderedDict[tuple[str, str], asyncio.Lock]" = OrderedDict()


def _sections_lock(namespace: str, canonical: str) -> asyncio.Lock:
    """Return the async lock guarding the sections cache for one paper.

    Adding/looking up under the GIL is atomic, so racing constructors
    are safe — only one Lock wins, the other is discarded uncontended.
    Touched entries move to the end so the FIFO eviction below removes
    the least-recently-used keys when the cap is exceeded.
    """
    key = (namespace, canonical)
    lock = _section_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        existing = _section_locks.setdefault(key, lock)
        if existing is lock:
            # We were the inserting writer — enforce the cap. Evict from
            # the front (oldest), skipping any lock that is currently
            # held (a held lock is doing real work right now and the
            # caller depends on its mutual exclusion) and the key we just
            # inserted (the caller is about to use it). ``held_skips``
            # counts consecutive un-evictable locks rotated to the back;
            # once it reaches the map size we've cycled past every entry
            # and none is evictable, so we bail rather than spin — going
            # slightly over cap is fine, hanging is not. Bounding the
            # probe this way keeps a full pass O(N) instead of re-scanning
            # the whole map with all(...) on every iteration.
            held_skips = 0
            while len(_section_locks) > _SECTION_LOCKS_MAX:
                if held_skips >= len(_section_locks):
                    break
                evict_key, evict_lock = next(iter(_section_locks.items()))
                if evict_key == key or evict_lock.locked():
                    _section_locks.move_to_end(evict_key)
                    held_skips += 1
                    continue
                _section_locks.pop(evict_key, None)
                held_skips = 0
        else:
            lock = existing
    _section_locks.move_to_end(key)
    return lock


# Fixed heading levels: H1 and H2 both open a new section (converters
# disagree on which level to use for the top), H3 is tracked as the
# sub-heading level, everything deeper is ignored.
_SECTION_LEVELS: frozenset[int] = frozenset({1, 2})
_SUB_LEVEL: int = 3


def parse_sections(markdown: str) -> list[dict[str, Any]]:
    """Parse markdown into sections with sub-heading previews.

    H1 and H2 are both treated as section boundaries; H3 is tracked as a
    sub-heading within the enclosing section. Returns a list of section dicts:
      {"index": 0, "title": "Introduction", "h3s": ["Background"], "approx_tokens": 800}

    Content before the first section heading is captured as a "Preamble" section.
    """
    lines = markdown.split("\n")

    sections: list[dict[str, Any]] = []
    current_title = "Preamble"
    current_h3s: list[str] = []
    current_lines: list[str] = []

    def _flush():
        content = "\n".join(current_lines)
        # Only add if there's meaningful content (not just whitespace)
        if content.strip():
            sections.append(
                {
                    "index": len(sections),
                    "title": current_title,
                    "h3s": current_h3s[:],
                    "approx_tokens": max(1, len(content) // _CHARS_PER_TOKEN),
                }
            )

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if level in _SECTION_LEVELS:
                _flush()
                current_title = title
                current_h3s = []
                current_lines = []
                continue
            elif level == _SUB_LEVEL:
                current_h3s.append(title)

        current_lines.append(line)

    # Flush the last section
    _flush()

    return sections


# Snippet window around an in-paper match. ~60 chars on each side gives
# the agent enough context to recognise relevance without overflowing.
_FIND_SNIPPET_WINDOW = 60


def find_in_markdown(
    markdown: str,
    query: str,
    *,
    max_results: int = 20,
    case_sensitive: bool = False,
    whole_words: bool = False,
    normalize: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Scan markdown for occurrences of ``query`` and return per-hit context.

    Each hit carries the section title, section index (matching what
    ``get_paper_section`` exposes), the character offset within that
    section's stripped text (so an agent can call
    ``get_paper_section(identifier, section_index, offset=char_offset)``
    to land at the match), and a ~120-char snippet centred on the match.

    ``whole_words=True`` wraps the query in ``\\b…\\b`` so "set" doesn't
    match "subset". ``case_sensitive=False`` is the default — academic
    prose capitalisation is unreliable.

    ``normalize=True`` NFKD-folds the query and each section's text and
    strips combining marks before matching, so "cafe" matches "café" and
    "Gutierrez" matches "Gutiérrez" (and vice versa). Offsets, ``match``,
    and ``snippet`` are still sliced from the ORIGINAL (un-folded) text —
    a fold-with-position-map translates each match back to original
    offsets — so chaining into ``get_paper_section`` still lands on the
    match. Caveat: ``\\b`` word boundaries are ASCII-oriented; folding
    turns diacritic Latin words into ASCII so ``whole_words`` works for
    them, but non-Latin scripts (CJK, Arabic) stay unreliable for
    ``whole_words`` and are largely unaffected by folding.

    Hit offsets align with ``get_paper_section``'s stripped section text
    because both apply the same ``"\\n".join(lines[s:e]).strip()`` recipe.

    Returns ``(hits, truncated)`` where ``truncated`` is ``True`` when the
    scan stopped at ``max_results`` with more matches still in the document,
    so callers can signal "more exist" instead of silently capping.
    """
    if not query:
        return [], False

    lines = markdown.split("\n")
    boundaries: list[tuple[str, int, int]] = []
    current_title = "Preamble"
    current_start = 0
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) in _SECTION_LEVELS:
            boundaries.append((current_title, current_start, i))
            current_title = m.group(2).strip()
            current_start = i + 1
    boundaries.append((current_title, current_start, len(lines)))
    # Drop empty sections so the indexing matches get_section_content.
    boundaries = [(t, s, e) for t, s, e in boundaries if "\n".join(lines[s:e]).strip()]

    if normalize:
        folded_query = _textnorm.fold(query)
        if not folded_query:
            # Query was entirely combining marks — an empty pattern would
            # match at every position, so there is nothing to find.
            return [], False
        pattern = re.escape(folded_query)
    else:
        pattern = re.escape(query)
    if whole_words:
        pattern = rf"\b{pattern}\b"
    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(pattern, flags)

    hits: list[dict[str, Any]] = []
    for section_index, (title, start, end) in enumerate(boundaries):
        # Same recipe as get_section_content so offsets align.
        section_text = "\n".join(lines[start:end]).strip()
        # When normalising, match against the folded text but keep a map
        # back to original offsets so char_offset/match/snippet stay
        # aligned with the un-folded section text get_section_content
        # returns.
        if normalize:
            search_text, index_map = _textnorm.fold_with_map(section_text)
        else:
            search_text, index_map = section_text, None
        for match in regex.finditer(search_text):
            if len(hits) >= max_results:
                return hits, True
            if index_map is None:
                pos = match.start()
                matched = match.group()
            else:
                pos = index_map[match.start()]
                matched = section_text[pos : index_map[match.end()]]
            ws = max(0, pos - _FIND_SNIPPET_WINDOW)
            we = min(len(section_text), pos + len(matched) + _FIND_SNIPPET_WINDOW)
            # Collapse newlines so the snippet renders on one line in
            # the agent's view; the surrounding context stays readable.
            snippet = section_text[ws:we].replace("\n", " ").strip()
            hits.append(
                {
                    "section_index": section_index,
                    "section": title,
                    "char_offset": pos,
                    "match": matched,
                    "snippet": snippet,
                }
            )
    return hits, False


def get_section_content(
    markdown: str,
    section: int | str,
    offset: int = 0,
    max_chars: int = 16000,
) -> dict[str, Any]:
    """Retrieve a slice of a section's content by index or title.

    Args:
        markdown: Full markdown text.
        section: Integer index or string title (case-insensitive partial match).
        offset: Starting character offset within the section. Defaults to 0.
            Use ``next_offset`` from a previous call to page through.
        max_chars: Slice size in characters. Defaults to 16000 (~4000 tokens).
            Must be positive.

    Returns:
        On success: ``{index, title, content, offset, chars_returned,
        total_chars, approx_tokens, has_more, next_offset}``. ``approx_tokens``
        and ``total_chars`` describe the full section, not the slice.
        On error: ``{"error": ...}`` (lists available titles for unknown
        section names).
    """
    if max_chars <= 0:
        return {"error": f"max_chars must be positive, got {max_chars}"}
    if offset < 0:
        return {"error": f"offset must be non-negative, got {offset}"}

    lines = markdown.split("\n")

    boundaries: list[tuple[str, int, int]] = []
    current_title = "Preamble"
    current_start = 0

    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) in _SECTION_LEVELS:
            boundaries.append((current_title, current_start, i))
            current_title = m.group(2).strip()
            current_start = i + 1

    boundaries.append((current_title, current_start, len(lines)))

    boundaries = [(t, s, e) for t, s, e in boundaries if "\n".join(lines[s:e]).strip()]

    if isinstance(section, int):
        if 0 <= section < len(boundaries):
            resolved_index = section
            title, start, end = boundaries[section]
        else:
            return {"error": f"Section index {section} out of range (0-{len(boundaries) - 1})"}
    else:
        query = section.lower()
        matches = [(i, t, s, e) for i, (t, s, e) in enumerate(boundaries) if query in t.lower()]
        if len(matches) == 1:
            resolved_index, title, start, end = matches[0]
        elif len(matches) > 1:
            titles = [t for _, t, _, _ in matches]
            return {"error": f"Ambiguous section title '{section}'. Matches: {titles}"}
        else:
            titles = [t for t, _, _ in boundaries]
            return {"error": f"No section matching '{section}'. Available: {titles}"}

    full_content = "\n".join(lines[start:end]).strip()
    total_chars = len(full_content)
    approx_tokens = max(1, total_chars // _CHARS_PER_TOKEN)

    if offset > total_chars:
        return {"error": f"offset {offset} is beyond section length {total_chars}"}

    end_offset = min(offset + max_chars, total_chars)
    slice_content = full_content[offset:end_offset]
    has_more = end_offset < total_chars

    return {
        "index": resolved_index,
        "title": title,
        "content": slice_content,
        "offset": offset,
        "chars_returned": len(slice_content),
        "total_chars": total_chars,
        "approx_tokens": approx_tokens,
        "has_more": has_more,
        "next_offset": end_offset if has_more else None,
    }


def _finalize_markdown(
    namespace: str,
    canonical: str,
    md_path: Path,
    raw_markdown: str,
    mode: str,
) -> dict[str, Any]:
    """Post-process extractor output, cache it, and return the section index.

    Shared tail for both conversion modes ("full" and "fast"): normalise
    trailing whitespace, strip image paths, write the markdown to the cache,
    parse sections, and store the section index tagged with the conversion
    mode so callers can tell a degraded fast extraction from a full one.
    """
    # Normalise trailing whitespace line-by-line.
    markdown = "\n".join(line.rstrip() for line in raw_markdown.split("\n"))

    # Strip unused image paths: ``![caption](path)`` → ``![caption]()``
    # When there is no caption, the path is never useful, so drop it.
    # When there is a caption, keep the caption text and drop the path.
    markdown = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"![\1]()", markdown)

    # Atomic UTF-8 write: a crash mid-write can't leave a torn markdown file,
    # and non-ASCII content survives a non-UTF-8 host locale.
    cache._atomic_write_text(md_path, markdown)

    sections = parse_sections(markdown)
    cache.put(
        namespace,
        "sections",
        _sections_key(canonical),
        {
            "sections": sections,
            "markdown_checksum": _markdown_checksum(md_path),
            "conversion_mode": mode,
        },
    )
    return {
        "markdown_path": str(md_path),
        "sections": sections,
        "cached": False,
        "conversion_mode": mode,
    }


async def _convert_fast(
    pdf_path: Path,
    namespace: str,
    canonical: str,
    pdf_size_mb: float,
) -> dict[str, Any]:
    """Lightweight text extraction, run *outside* the global conversion lock.

    Shells out to PDF_FAST_CONVERTER (default ``pdftotext``) capturing stdout,
    then caches the text as markdown via the shared finaliser. Deliberately
    degraded: plain text, no tables/equations/figures, no real headings. Cheap
    and not GPU-bound, so it never serialises behind a heavy MinerU conversion
    and never returns a ``busy`` error. The per-paper sections lock serialises
    concurrent fast calls on the same paper so they don't both spawn and race
    the cache write; stderr is captured separately so stdout stays clean text.
    """
    md_path = _markdown_path(namespace, canonical)
    async with _sections_lock(namespace, canonical):
        # A racing fast caller may have written the markdown between the outer
        # cached-check and our acquiring this lock — re-check before spawning.
        if md_path.exists():
            markdown = md_path.read_text(encoding="utf-8")
            sections = parse_sections(markdown)
            cache.put(
                namespace,
                "sections",
                _sections_key(canonical),
                {
                    "sections": sections,
                    "markdown_checksum": _markdown_checksum(md_path),
                    "conversion_mode": "fast",
                },
            )
            return {
                "markdown_path": str(md_path),
                "sections": sections,
                "cached": True,
                "conversion_mode": "fast",
            }

        cmd = _build_fast_converter_command(pdf_path)
        timeout = _resolve_fast_convert_timeout()

        try:
            # start_new_session=True so a timeout can SIGKILL the whole tree.
            # stderr is kept separate (not merged) so stdout is pure text.
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            return {
                "error": (
                    f"Could not start fast PDF extractor subprocess: {e}. "
                    "Check that the PDF_FAST_CONVERTER command is installed "
                    "(default 'pdftotext' needs poppler-utils; 'pymupdf' needs "
                    "`pip install academic-tools-mcp[fast]`)."
                ),
                "retryable": False,
                "conversion_mode": "fast",
                "pdf_size_mb": round(pdf_size_mb, 1),
            }

        try:
            if timeout is None:
                stdout, stderr = await proc.communicate()
            else:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                pass
            return {
                "error": (
                    f"Fast PDF extraction timed out after {timeout:.0f}s "
                    f"(PDF: {pdf_size_mb:.1f} MB). "
                    "Increase PDF_FAST_CONVERT_TIMEOUT or set it to 'none' to disable."
                ),
                "retryable": False,
                "timed_out": True,
                "timeout_seconds": timeout,
                "conversion_mode": "fast",
                "pdf_size_mb": round(pdf_size_mb, 1),
            }

        if proc.returncode != 0:
            # Prefer stderr (where extractors write diagnostics); fall back to
            # stdout. Replace undecodable bytes rather than raising.
            output = (stderr or b"").decode("utf-8", errors="replace") or (stdout or b"").decode(
                "utf-8", errors="replace"
            )
            return {
                "error": f"Fast PDF extraction failed (exit {proc.returncode}): {output[-500:]}",
                "retryable": False,
                "conversion_mode": "fast",
                "pdf_size_mb": round(pdf_size_mb, 1),
            }

        markdown = (stdout or b"").decode("utf-8", errors="replace")
        # pdftotext separates pages with a form-feed; turn it into a blank line.
        markdown = markdown.replace("\f", "\n")
        if not markdown.strip():
            return {
                "error": (
                    f"Fast PDF extractor produced no text (PDF: {pdf_size_mb:.1f} MB). "
                    "The PDF may be image-only/scanned — try full conversion (MinerU "
                    "runs OCR) instead."
                ),
                "retryable": False,
                "conversion_mode": "fast",
                "pdf_size_mb": round(pdf_size_mb, 1),
            }

        return _finalize_markdown(namespace, canonical, md_path, markdown, "fast")


async def convert_pdf(
    pdf_path: Path,
    namespace: str,
    canonical: str,
    *,
    force_refresh: bool = False,
    mode: str = "full",
) -> dict[str, Any]:
    """Convert a PDF to markdown, cache the result, and return section index.

    Args:
        pdf_path: Path to the cached PDF file.
        namespace: Cache namespace (e.g., "arxiv").
        canonical: Canonical ID for cache keying.
        force_refresh: If True, drop any cached markdown + section index
            for this paper so the converter re-runs. Use after replacing the
            source PDF or upgrading the converter.
        mode: ``"full"`` (default) runs the heavy converter (MinerU/Marker)
            under the global single-conversion lock. ``"fast"`` runs a
            lightweight text extractor (PDF_FAST_CONVERTER, default
            ``pdftotext``) outside that lock — a deliberately degraded path
            that never serialises or returns ``busy``, useful when the full
            converter times out or is unavailable.

    Returns:
        Dict with markdown_path, sections, cached, conversion_mode, or an error.
    """
    md_path = _markdown_path(namespace, canonical)

    if force_refresh:
        # Drop both halves under the per-paper lock so a concurrent reader
        # can't catch a half-cleared state (markdown gone, stale sections
        # entry still pointing at the old checksum).
        async with _sections_lock(namespace, canonical):
            if md_path.exists():
                md_path.unlink()
            cache.invalidate(namespace, "sections", _sections_key(canonical))

    # If the markdown is already cached, never re-run the slow conversion —
    # re-parse from the existing markdown if the sections cache is missing
    # or stale, and refresh the sections cache. The lock serialises this
    # block per paper so two concurrent callers don't both re-parse.
    if md_path.exists():
        async with _sections_lock(namespace, canonical):
            markdown = md_path.read_text(encoding="utf-8")
            current_checksum = _markdown_checksum(md_path)
            cached_sections = cache.get(namespace, "sections", _sections_key(canonical))

            if cached_sections is not None:
                stored_checksum = cached_sections.get("markdown_checksum")
                if stored_checksum is not None and stored_checksum == current_checksum:
                    # Don't use dict.get's default arg — it evaluates eagerly
                    # and would call parse_sections on every cache hit.
                    sections = cached_sections.get("sections")
                    if sections is None:
                        sections = parse_sections(markdown)
                    return {
                        "markdown_path": str(md_path),
                        "sections": sections,
                        "cached": True,
                        "conversion_mode": cached_sections.get("conversion_mode"),
                    }

            # Sections cache missing or stale — re-parse the existing markdown
            # and refresh the sections cache. No subprocess needed. Preserve the
            # recorded conversion_mode if a (stale-checksum) entry carried one.
            recorded_mode = (
                cached_sections.get("conversion_mode") if cached_sections is not None else None
            )
            sections = parse_sections(markdown)
            cache.put(
                namespace,
                "sections",
                _sections_key(canonical),
                {
                    "sections": sections,
                    "markdown_checksum": current_checksum,
                    "conversion_mode": recorded_mode,
                },
            )
            return {
                "markdown_path": str(md_path),
                "sections": sections,
                "cached": True,
                "conversion_mode": recorded_mode,
            }

    if not pdf_path.exists():
        return {"error": f"PDF not found at: {pdf_path}"}

    # Report PDF size so callers can gauge feasibility
    pdf_size_bytes = pdf_path.stat().st_size
    pdf_size_mb = pdf_size_bytes / (1024 * 1024)

    # Fast path: lightweight extraction outside the global lock. Never
    # serialises behind a heavy conversion and never returns a busy error.
    if mode == "fast":
        return await _convert_fast(pdf_path, namespace, canonical, pdf_size_mb)

    # Global single-conversion gate. The check-then-acquire is safe
    # because asyncio.Lock.acquire() on an uncontended lock returns
    # without yielding — no other coroutine can sneak in between
    # `if locked()` and `async with`.
    if _global_convert_lock.locked():
        return _busy_error(pdf_size_mb)

    async with _global_convert_lock:
        global _current_conversion
        _current_conversion = {
            "namespace": namespace,
            "canonical": canonical,
            "started_at": time.monotonic(),
        }
        # Bound before the try so the finally can always clean it up, even if
        # subprocess setup throws before the assignment below.
        extract_dir: Path | None = None
        try:
            # Run PDF converter in a subprocess
            extract_dir = Path(f"/tmp/pdf-convert-{canonical.replace('/', '_')}")
            converter_cmd = _build_converter_command(pdf_path, extract_dir)
            quoted_extract = shlex.quote(str(extract_dir))

            timeout = _resolve_convert_timeout()

            try:
                # start_new_session=True puts the converter (and any children
                # it spawns) into a fresh process group so we can SIGKILL the
                # whole tree on timeout. Without it, killing `proc` only kills
                # bash and orphans the converter, which keeps eating CPU/GPU.
                proc = await asyncio.create_subprocess_exec(
                    "bash",
                    "-c",
                    f"rm -rf {quoted_extract} 2>/dev/null; {converter_cmd} 2>&1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as e:
                # Process spawn failed (bash missing, fork EAGAIN, perms).
                # Different from a converter that ran and failed.
                return {
                    "error": (
                        f"Could not start PDF converter subprocess: {e}. "
                        "Check that bash is on PATH and that the PDF_CONVERTER / "
                        "PDF_CONVERTER_VENV env vars point at a usable command."
                    ),
                    "retryable": False,
                    "pdf_size_mb": round(pdf_size_mb, 1),
                }

            try:
                if timeout is None:
                    stdout, stderr = await proc.communicate()
                else:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                # Take down the whole process group, then give it a moment to
                # actually exit before we return so we don't leave zombies.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    pass
                return {
                    "error": (
                        f"PDF conversion timed out after {timeout:.0f}s "
                        f"(PDF: {pdf_size_mb:.1f} MB). "
                        "Increase PDF_CONVERT_TIMEOUT or set it to 'none' to disable."
                    ),
                    "retryable": False,
                    "timed_out": True,
                    "timeout_seconds": timeout,
                    "pdf_size_mb": round(pdf_size_mb, 1),
                }

            if proc.returncode != 0:
                # Converter output may include binary noise on crashes;
                # replace undecodable bytes rather than raising
                # UnicodeDecodeError ourselves.
                output = (stdout or b"").decode("utf-8", errors="replace") + (stderr or b"").decode(
                    "utf-8", errors="replace"
                )
                return {
                    "error": f"PDF conversion failed (exit {proc.returncode}): {output[-500:]}",
                    "retryable": False,
                    "pdf_size_mb": round(pdf_size_mb, 1),
                }

            # Find the generated markdown file in the output directory
            stem = pdf_path.stem
            candidates = list(extract_dir.glob(f"**/{stem}.md"))

            if not candidates:
                # Try any .md file in the output
                candidates = list(extract_dir.glob("**/*.md"))

            if not candidates:
                return {
                    "error": f"PDF converter produced no markdown output (PDF: {pdf_size_mb:.1f} MB).",
                    "retryable": False,
                    "pdf_size_mb": round(pdf_size_mb, 1),
                }

            source_md = candidates[0]
            return _finalize_markdown(
                namespace, canonical, md_path, source_md.read_text(encoding="utf-8"), "full"
            )
        finally:
            # Clean up the temp extraction dir on every exit — success *and*
            # all four failure paths (spawn error, timeout, non-zero exit,
            # no-markdown) — so failed conversions don't leak /tmp dirs.
            if extract_dir is not None:
                shutil.rmtree(extract_dir, ignore_errors=True)
            _current_conversion = None
