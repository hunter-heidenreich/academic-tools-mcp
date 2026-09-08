"""Bundled pymupdf text-extraction runner for the fast conversion path.

Invoked as a subprocess by ``papers._convert_fast`` when
``PDF_FAST_CONVERTER=pymupdf``:

    python -m academic_tools_mcp._fast_extract <pdf_path>

Extracts plain text from every page and writes it to **stdout** (the contract
every fast-converter backend follows). pymupdf is an optional dependency —
install it with ``pip install academic-tools-mcp[fast]``. On a missing import
or any extraction error this writes a clear message to stderr and exits
non-zero so the caller surfaces a permanent (non-retryable) error rather than
caching an empty file.
"""

import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m academic_tools_mcp._fast_extract <pdf_path>", file=sys.stderr)
        return 2

    pdf_path = argv[1]

    try:
        import pymupdf
    except ImportError:
        print(
            "pymupdf is not installed. Install the optional extra with "
            "`pip install academic-tools-mcp[fast]`, or set PDF_FAST_CONVERTER "
            "to a different backend (e.g. 'pdftotext').",
            file=sys.stderr,
        )
        return 1

    try:
        with pymupdf.open(pdf_path) as doc:
            text = "\n\n".join(page.get_text() for page in doc)
    except Exception as e:  # noqa: BLE001 — surface any extraction failure cleanly
        print(f"pymupdf failed to extract text from {pdf_path!r}: {e}", file=sys.stderr)
        return 1

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
