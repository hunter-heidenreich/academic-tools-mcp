"""PDF-to-markdown conversion and section-level access.

The pipeline in three layers, each importable on its own:

* :mod:`.sections` — pure markdown structure: split into sections, locate an
  offset, search within a document. No I/O, no asyncio.
* :mod:`.index` — the on-disk section index: read it, refresh it when the
  markdown drifted, drop it. Owns the per-paper lock.
* :mod:`.convert` — running a converter subprocess and storing what it
  produced. Owns the global single-conversion gate.

Artifact *naming* deliberately lives below all three, in
:mod:`academic_tools_mcp.store.stems` — one layer down, in the storage
package, so a provider that needs to name a PDF does not import a
converter. It is deliberately **not** re-exported here: routing it through
this facade would give one module two import names, which is how
``manual`` came to reach the same layer as both ``papers.safe_stem`` and
``stems.pdf_path``.

This module re-exports the surface the rest of the server uses; the submodules
are the home of each symbol and the place to read about it.
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
