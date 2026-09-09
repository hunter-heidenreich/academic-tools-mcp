"""PDF-to-markdown conversion and section-level access.

The pipeline in three layers:

* :mod:`.sections` — pure markdown structure: split into sections, locate an
  offset, search within a document. No I/O, no asyncio.
* :mod:`.index` — the on-disk section index: write it, read it, refresh it when
  the markdown drifted, drop it. Owns the per-paper lock.
* :mod:`.convert` — running a converter subprocess and storing what it
  produced. Owns the global single-conversion gate.

Artifact *naming* deliberately lives one layer down, in
:mod:`academic_tools_mcp.store.stems`, so a provider that needs to name a PDF
does not import a converter. It is **not** re-exported here: a facade can carry
only part of it, and one module under two import names is reached as both
``papers.safe_stem`` and ``stems.pdf_path`` in the same file.

This module re-exports the surface the rest of the server uses, by value; each
submodule is the home of its symbols, the place to read about them, and the
place to patch them.
"""

from .convert import convert_pdf
from .index import (
    drop_derived,
    get_or_parse_sections,
    recorded_conversion_mode,
    rekey_sections,
    sections_lock,
    store_markdown_and_index,
)
from .sections import (
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
    "Section",
    "convert_pdf",
    "drop_derived",
    "find_in_markdown",
    "first_section_heading",
    "get_or_parse_sections",
    "get_section_content",
    "has_detected_sections",
    "parse_sections",
    "parse_sections_and_detect",
    "recorded_conversion_mode",
    "rekey_sections",
    "section_at_offset",
    "section_boundaries",
    "sections_lock",
    "store_markdown_and_index",
]
