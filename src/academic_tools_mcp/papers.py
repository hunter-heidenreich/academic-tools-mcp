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
import signal
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from . import cache, config

# Default subprocess timeout for PDF→markdown conversion. Big PDFs on
# CPU-only MinerU runs can legitimately take 20+ minutes, so we err
# generous. Tunable via PDF_CONVERT_TIMEOUT (seconds); 0/empty disables.
_DEFAULT_PDF_CONVERT_TIMEOUT = 1800.0

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


def _resolve_convert_timeout() -> float | None:
    """Resolve the PDF conversion timeout from PDF_CONVERT_TIMEOUT.

    Returns the timeout in seconds, or None to disable the timeout.
    Unset / empty / "0" / negative / non-numeric values are treated as
    "use the default"; an explicit "none" / "off" / "disabled" disables.
    """
    raw = config.get("PDF_CONVERT_TIMEOUT")
    if raw is None:
        return _DEFAULT_PDF_CONVERT_TIMEOUT
    raw = raw.strip().lower()
    if raw in {"none", "off", "disabled", "0"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_PDF_CONVERT_TIMEOUT
    if value <= 0:
        return None
    return value


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
    return cache._cache_dir(namespace, "markdown") / (
        canonical.replace("/", "_") + ".md"
    )


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
            # the front (oldest) and skip any lock that is currently
            # held; a held lock is doing real work right now and the
            # caller depends on its mutual exclusion.
            while len(_section_locks) > _SECTION_LOCKS_MAX:
                evict_key, evict_lock = next(iter(_section_locks.items()))
                if evict_lock.locked():
                    # Move it to the end so we don't spin re-checking
                    # this same held lock; the next eviction pass will
                    # find a free one ahead of it.
                    _section_locks.move_to_end(evict_key)
                    # If every lock is held (extremely unlikely), bail
                    # rather than spin forever — going slightly over cap
                    # is fine, hanging is not.
                    if all(l.locked() for l in _section_locks.values()):
                        break
                    continue
                _section_locks.pop(evict_key, None)
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
            sections.append({
                "index": len(sections),
                "title": current_title,
                "h3s": current_h3s[:],
                "approx_tokens": max(1, len(content) // _CHARS_PER_TOKEN),
            })

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
) -> list[dict[str, Any]]:
    """Scan markdown for occurrences of ``query`` and return per-hit context.

    Each hit carries the section title, section index (matching what
    ``get_paper_section`` exposes), the character offset within that
    section's stripped text (so an agent can call
    ``get_paper_section(identifier, section_index, offset=char_offset)``
    to land at the match), and a ~120-char snippet centred on the match.

    ``whole_words=True`` wraps the query in ``\\b…\\b`` so "set" doesn't
    match "subset". ``case_sensitive=False`` is the default — academic
    prose capitalisation is unreliable.

    Hit offsets align with ``get_paper_section``'s stripped section text
    because both apply the same ``"\\n".join(lines[s:e]).strip()`` recipe.
    """
    if not query:
        return []

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
    boundaries = [
        (t, s, e) for t, s, e in boundaries
        if "\n".join(lines[s:e]).strip()
    ]

    pattern = re.escape(query)
    if whole_words:
        pattern = rf"\b{pattern}\b"
    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(pattern, flags)

    hits: list[dict[str, Any]] = []
    for section_index, (title, start, end) in enumerate(boundaries):
        # Same recipe as get_section_content so offsets align.
        section_text = "\n".join(lines[start:end]).strip()
        for match in regex.finditer(section_text):
            if len(hits) >= max_results:
                return hits
            pos = match.start()
            matched = match.group()
            ws = max(0, pos - _FIND_SNIPPET_WINDOW)
            we = min(len(section_text), pos + len(matched) + _FIND_SNIPPET_WINDOW)
            # Collapse newlines so the snippet renders on one line in
            # the agent's view; the surrounding context stays readable.
            snippet = section_text[ws:we].replace("\n", " ").strip()
            hits.append({
                "section_index": section_index,
                "section": title,
                "char_offset": pos,
                "match": matched,
                "snippet": snippet,
            })
    return hits


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

    boundaries = [
        (t, s, e) for t, s, e in boundaries
        if "\n".join(lines[s:e]).strip()
    ]

    if isinstance(section, int):
        if 0 <= section < len(boundaries):
            resolved_index = section
            title, start, end = boundaries[section]
        else:
            return {
                "error": f"Section index {section} out of range (0-{len(boundaries) - 1})"
            }
    else:
        query = section.lower()
        matches = [
            (i, t, s, e) for i, (t, s, e) in enumerate(boundaries)
            if query in t.lower()
        ]
        if len(matches) == 1:
            resolved_index, title, start, end = matches[0]
        elif len(matches) > 1:
            titles = [t for _, t, _, _ in matches]
            return {
                "error": f"Ambiguous section title '{section}'. Matches: {titles}"
            }
        else:
            titles = [t for t, _, _ in boundaries]
            return {
                "error": f"No section matching '{section}'. Available: {titles}"
            }

    full_content = "\n".join(lines[start:end]).strip()
    total_chars = len(full_content)
    approx_tokens = max(1, total_chars // _CHARS_PER_TOKEN)

    if offset > total_chars:
        return {
            "error": f"offset {offset} is beyond section length {total_chars}"
        }

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


async def convert_pdf(
    pdf_path: Path,
    namespace: str,
    canonical: str,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Convert a PDF to markdown, cache the result, and return section index.

    Args:
        pdf_path: Path to the cached PDF file.
        namespace: Cache namespace (e.g., "arxiv").
        canonical: Canonical ID for cache keying.
        force_refresh: If True, drop any cached markdown + section index
            for this paper so the converter subprocess re-runs. Use after
            replacing the source PDF or upgrading the converter.

    Returns:
        Dict with markdown_path, sections list, or an error.
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
            markdown = md_path.read_text()
            current_checksum = _markdown_checksum(md_path)
            cached_sections = cache.get(
                namespace, "sections", _sections_key(canonical)
            )

            if cached_sections is not None:
                stored_checksum = cached_sections.get("markdown_checksum")
                if (
                    stored_checksum is not None
                    and stored_checksum == current_checksum
                ):
                    # Don't use dict.get's default arg — it evaluates eagerly
                    # and would call parse_sections on every cache hit.
                    sections = cached_sections.get("sections")
                    if sections is None:
                        sections = parse_sections(markdown)
                    return {
                        "markdown_path": str(md_path),
                        "sections": sections,
                        "cached": True,
                    }

            # Sections cache missing or stale — re-parse the existing markdown
            # and refresh the sections cache. No subprocess needed.
            sections = parse_sections(markdown)
            cache.put(
                namespace,
                "sections",
                _sections_key(canonical),
                {"sections": sections, "markdown_checksum": current_checksum},
            )
            return {
                "markdown_path": str(md_path),
                "sections": sections,
                "cached": True,
            }

    if not pdf_path.exists():
        return {"error": f"PDF not found at: {pdf_path}"}

    # Report PDF size so callers can gauge feasibility
    pdf_size_bytes = pdf_path.stat().st_size
    pdf_size_mb = pdf_size_bytes / (1024 * 1024)

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
                    "bash", "-c",
                    f'rm -rf {quoted_extract} 2>/dev/null; {converter_cmd} 2>&1',
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
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout
                    )
            except asyncio.TimeoutError:
                # Take down the whole process group, then give it a moment to
                # actually exit before we return so we don't leave zombies.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
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
                output = (
                    (stdout or b"").decode("utf-8", errors="replace")
                    + (stderr or b"").decode("utf-8", errors="replace")
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
            markdown = source_md.read_text()

            # Post-process the raw converter output before caching
            lines = markdown.split("\n")
            lines = [line.rstrip() for line in lines]
            markdown = "\n".join(lines)

            # Strip unused image paths: ``![caption](path)`` → ``![caption]()``
            # When there is no caption, the path is never useful, so drop it.
            # When there is a caption, keep the caption text and drop the path.
            markdown = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'![\1]()', markdown)

            # Store markdown in cache
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(markdown)

            # Parse sections and cache with checksum
            sections = parse_sections(markdown)
            sections_data = {
                "sections": sections,
                "markdown_checksum": _markdown_checksum(md_path),
            }
            cache.put(namespace, "sections", _sections_key(canonical), sections_data)

            # Clean up temp directory
            import shutil
            shutil.rmtree(extract_dir, ignore_errors=True)

            return {
                "markdown_path": str(md_path),
                "sections": sections,
                "cached": False,
            }
        finally:
            _current_conversion = None
