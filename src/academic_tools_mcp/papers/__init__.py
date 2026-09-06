"""PDF-to-markdown conversion and section-level access.

The pipeline in three layers, each importable on its own:

* :mod:`.sections` — pure markdown structure: split into sections, locate an
  offset, search within a document. No I/O, no asyncio.
* :mod:`.index` — the on-disk section index: read it, refresh it when the
  markdown drifted, drop it. Owns the per-paper lock.
* :mod:`.convert` — running a converter subprocess and storing what it
  produced. Owns the global single-conversion gate.

Artifact *naming* deliberately lives below all three, in
:mod:`academic_tools_mcp._stems`, so a provider that needs to name a PDF does
not import a converter.

This module re-exports the surface the rest of the server uses; the submodules
are the home of each symbol and the place to read about it.
"""

from .._stems import (
    checksum_text,
    markdown_checksum,
    markdown_path,
    migrate_legacy_stems,
    pdf_path,
    safe_stem,
    sections_key,
)
from .convert import ConverterTemplateError, convert_pdf
from .index import (
    drop_derived,
    get_or_parse_sections,
    sections_lock,
    store_markdown_and_index,
)
from .sections import (
    HEADING_PATTERN,
    SECTION_LEVELS,
    Section,
    find_in_markdown,
    first_section_heading,
    get_section_content,
    has_detected_sections,
    parse_sections,
    parse_sections_and_detect,
    section_at_offset,
    section_boundaries,
)

__all__ = [
    "HEADING_PATTERN",
    "SECTION_LEVELS",
    "ConverterTemplateError",
    "Section",
    "checksum_text",
    "convert_pdf",
    "drop_derived",
    "find_in_markdown",
    "first_section_heading",
    "get_or_parse_sections",
    "get_section_content",
    "has_detected_sections",
    "markdown_checksum",
    "markdown_path",
    "migrate_legacy_stems",
    "parse_sections",
    "parse_sections_and_detect",
    "pdf_path",
    "safe_stem",
    "section_at_offset",
    "section_boundaries",
    "sections_key",
    "sections_lock",
    "store_markdown_and_index",
]
