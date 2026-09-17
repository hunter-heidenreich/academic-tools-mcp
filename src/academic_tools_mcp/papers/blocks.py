"""Markdown block emitters shared by the markup renderers (:mod:`.latexml`, :mod:`.jats`).

Pure. Each renderer walks its own tree; what they share is how a paragraph is kept
from reading as a heading and how a grid of cells becomes a pipe table.
"""

import re
from collections.abc import Iterable

# A body line starting with ``#`` (a code comment) would parse as a heading.
_LEADING_HASH_RE = re.compile(r"^#", re.MULTILINE)

# Caps a malformed span so one cell can't inflate the table.
MAX_SPAN = 100


def escape_headings(text: str) -> str:
    """Escape every line-leading ``#``, so body text never opens a section."""
    return _LEADING_HASH_RE.sub(r"\\#", text)


def span(value: str | None) -> int:
    """A ``colspan`` / ``rowspan`` attribute as a count: 1 unless a sane integer above it."""
    value = (value or "").strip()
    return min(int(value), MAX_SPAN) if value.isdecimal() and int(value) > 1 else 1


def pipe_table(rows: Iterable[Iterable[tuple[str, int, int]]]) -> str:
    """Rows of ``(text, colspan, rowspan)`` cells as a pipe table, the first row the header.

    Spanned columns and rows are blank, keeping every cell under its header. Text
    arrives collapsed; ``|`` is escaped here.
    """
    grid = []
    # Column -> rows a rowspan above still covers.
    covered: dict[int, int] = {}

    def skip_covered(cells: list[str]) -> None:
        while covered.get(len(cells), 0) > 0:
            covered[len(cells)] -= 1
            cells.append("")

    for row in rows:
        cells: list[str] = []
        for text, colspan, rowspan in row:
            skip_covered(cells)
            start = len(cells)
            cells.append(text.replace("|", "\\|"))
            cells.extend([""] * (colspan - 1))
            if rowspan > 1:
                covered.update(dict.fromkeys(range(start, start + colspan), rowspan - 1))
        # Covered columns past this row's last cell.
        while any(n > 0 and col >= len(cells) for col, n in covered.items()):
            if covered.get(len(cells), 0) > 0:
                covered[len(cells)] -= 1
            cells.append("")
        if any(cells):
            grid.append(cells)
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    lines = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in grid]
    lines.insert(1, "|" + " --- |" * width)
    return "\n".join(lines)
