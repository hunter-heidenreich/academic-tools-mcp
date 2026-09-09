"""Bundled pymupdf text extractor for the fast conversion path.

Spawned by ``papers.convert._convert_fast`` under ``sys.executable``, which is
why this is a module and not an inline ``python -c``. Invariant: stdout carries
the extracted document and nothing else — diagnostics go to stderr with a
non-zero exit, hence the ``T201`` ignore.
"""

import sys


def main(argv: list[str]) -> int:
    """Write the PDF's text to stdout; diagnostics to stderr, non-zero exit on failure."""
    if len(argv) != 2:
        print("usage: python -m academic_tools_mcp.fast_extract <pdf_path>", file=sys.stderr)
        return 2

    pdf_path = argv[1]

    try:
        import pymupdf
    except ImportError as e:
        print(
            f"pymupdf is unusable ({e}). Install the optional extra with "
            "`pip install academic-tools-mcp[fast]`, or set PDF_FAST_CONVERTER "
            "to a different backend (e.g. 'pdftotext').",
            file=sys.stderr,
        )
        return 1

    try:
        with pymupdf.open(pdf_path) as doc:
            text = "\n\n".join(page.get_text() for page in doc)
    except Exception as e:  # noqa: BLE001 — pymupdf's failure set is open: FileNotFoundError through its own mupdf-backed types
        print(f"pymupdf failed to extract text from {pdf_path!r}: {e}", file=sys.stderr)
        return 1

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
