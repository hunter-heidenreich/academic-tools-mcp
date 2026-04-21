"""PDF-to-markdown conversion and section-level access.

This module handles:
  - Running a configurable PDF converter (MinerU, Marker, or custom) to produce markdown
  - Parsing markdown into sections with sub-heading previews
  - Retrieving individual sections by title or index
  - Automatic cache invalidation when markdown changes (via checksum)

The converter backend is configured via PDF_CONVERTER and PDF_CONVERTER_VENV
environment variables. See _CONVERTERS for named backends.

Section splitting is adaptive: it detects the heading level used for main
sections (H1 or H2) based on what the document actually contains.

Cache invalidation: section indices are checksummed against the source markdown.
If the markdown file changes (e.g., manual edits), the sections are re-parsed
on the next call.
"""

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any

from . import cache, config

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


def _detect_heading_levels(lines: list[str]) -> tuple[int, int]:
    """Detect which heading levels to use for sections and sub-headings.

    Scans the document for headings and picks the two most-used levels.
    Returns (section_level, sub_level).

    Heuristic:
      - Find the most common heading level -> that's the section level
      - The next level down (section_level + 1) is the sub-heading level
      - If only one level exists, sub_level = section_level + 1 (won't match anything)
    """
    level_counts: dict[int, int] = {}
    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            level_counts[level] = level_counts.get(level, 0) + 1

    if not level_counts:
        # No headings at all; default to H2/H3
        return 2, 3

    # Most common level is the section level
    section_level = max(level_counts, key=lambda k: level_counts[k])
    sub_level = section_level + 1

    return section_level, sub_level


def parse_sections(markdown: str) -> list[dict[str, Any]]:
    """Parse markdown into sections with sub-heading previews.

    Adaptive: detects whether the document uses H1 or H2 for main sections.
    Returns a list of section dicts:
      {"index": 0, "title": "Introduction", "h3s": ["Background"], "approx_tokens": 800}

    The "h3s" key contains sub-heading titles (one level below the section level).
    Content before the first section heading is captured as a "Preamble" section.
    """
    lines = markdown.split("\n")
    section_level, sub_level = _detect_heading_levels(lines)

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
            if level == section_level:
                _flush()
                current_title = title
                current_h3s = []
                current_lines = []
                continue
            elif level == sub_level:
                current_h3s.append(title)

        current_lines.append(line)

    # Flush the last section
    _flush()

    return sections


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
    section_level, _ = _detect_heading_levels(lines)

    boundaries: list[tuple[str, int, int]] = []
    current_title = "Preamble"
    current_start = 0

    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) == section_level:
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
) -> dict[str, Any]:
    """Convert a PDF to markdown, cache the result, and return section index.

    Args:
        pdf_path: Path to the cached PDF file.
        namespace: Cache namespace (e.g., "arxiv").
        canonical: Canonical ID for cache keying.

    Returns:
        Dict with markdown_path, sections list, or an error.
    """
    md_path = _markdown_path(namespace, canonical)

    # Check if already converted and cache is valid
    if md_path.exists():
        cached = cache.get(namespace, "sections", _sections_key(canonical))
        if cached is not None:
            # Verify checksum if present; missing checksum means re-parse (legacy behavior)
            stored_checksum = cached.get("markdown_checksum", None)
            current_checksum = _markdown_checksum(md_path)
            if stored_checksum is None or stored_checksum == current_checksum:
                # Cache is valid
                sections = cached.get("sections", parse_sections(md_path.read_text()))
                return {
                    "markdown_path": str(md_path),
                    "sections": sections,
                    "cached": True,
                }
            # else: checksum mismatch -> re-parse below

    if not pdf_path.exists():
        return {"error": f"PDF not found at: {pdf_path}"}

    # Report PDF size so callers can gauge feasibility
    pdf_size_bytes = pdf_path.stat().st_size
    pdf_size_mb = pdf_size_bytes / (1024 * 1024)

    # Run PDF converter in a subprocess
    extract_dir = Path(f"/tmp/pdf-convert-{canonical.replace('/', '_')}")
    converter_cmd = _build_converter_command(pdf_path, extract_dir)

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c",
            f'rm -rf "{extract_dir}" 2>/dev/null; {converter_cmd} 2>&1',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=600  # 10 minutes
        )
    except asyncio.TimeoutError:
        return {
            "error": f"PDF conversion timed out after 10 minutes (PDF: {pdf_size_mb:.1f} MB). "
            "The document may be too large for the converter.",
            "retryable": False,
            "pdf_size_mb": round(pdf_size_mb, 1),
        }

    if proc.returncode != 0:
        output = (stdout or b"").decode() + (stderr or b"").decode()
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
