"""The downloaded PDF on disk: is it one, and is the cached copy usable.

Separate from the transport that fetched it, because the pipeline asks these
questions about files it never downloaded — `manual.import_local_pdf`'s and
`tools/pipeline.convert_paper`'s.
"""

from pathlib import Path
from typing import Any

_PDF_MAGIC = b"%PDF-"

# Readers scan a prefix, not byte 0; a BOM or stray leading bytes is still a PDF.
_PDF_HEADER_SEARCH_BYTES = 1024


def has_pdf_magic(head: bytes) -> bool:
    """Whether ``head`` opens a PDF, within the slack a real reader allows."""
    return _PDF_MAGIC in head[:_PDF_HEADER_SEARCH_BYTES]


def is_usable_pdf(path: Path) -> bool:
    """Whether a cached PDF should be trusted as a hit.

    Gate every cached-PDF check on this, never ``Path.exists()``: it rejects
    what an interrupted or degenerate download leaves behind — a missing or
    unreadable path, a 0-byte file, an HTML landing page saved under a .pdf
    name. Not a validity proof: a file truncated after the header passes, and
    that is recoverable (the converter fails) where silently serving an empty
    file is not.
    """
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as f:
            return has_pdf_magic(f.read(_PDF_HEADER_SEARCH_BYTES))
    except OSError:
        return False


def cached_hit(dest: Path) -> dict[str, Any] | None:
    """Return the ``{path, size_bytes, cached}`` hit for ``dest``, or None to re-download.

    Owns the ``stat``, so a file unlinked between the usability check and the
    size read is a miss rather than an ``OSError`` out of the caller.
    """
    try:
        if not is_usable_pdf(dest):
            return None
        return {"path": str(dest), "size_bytes": dest.stat().st_size, "cached": True}
    except OSError:
        return None
