"""PDF-to-markdown conversion and section-level access.

The pipeline in three layers, each importable on its own:

* :mod:`.sections` — pure markdown structure: split into sections, locate an
  offset, search within a document. No I/O, no asyncio.
* :mod:`.index` — the on-disk section index: read it, refresh it when the
  markdown drifted, drop it. Owns the per-paper lock.
* :mod:`.convert` — running a converter subprocess and storing what it
  produced. Owns the global single-conversion gate.

Artifact *naming* deliberately lives one layer down, in
:mod:`academic_tools_mcp.store.stems`, so a provider that needs to name a PDF
does not import a converter. It is **not** re-exported here, and that is the
point: doing so gave the module two import names and only re-exported some of
its symbols, so ``manual`` reached one layer as both ``papers.safe_stem`` and
``stems.pdf_path`` in the same file. Reach naming through
:mod:`~academic_tools_mcp.store.stems`, never through this package.

This module re-exports the surface the rest of the server uses from its own
three submodules; each submodule is the home of its symbols and the place to
read about them.
"""

from .convert import ConverterTemplateError, convert_pdf
from .index import (
    drop_derived,
    get_or_parse_sections,
    recorded_conversion_mode,
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
    "ConverterTemplateError",
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
    "section_at_offset",
    "section_boundaries",
    "sections_lock",
    "store_markdown_and_index",
]
