"""arXiv's LaTeXML HTML rendering to section-structured markdown.

Pure — text in, text out, stdlib ``html.parser`` only. The output is shaped for
:mod:`.sections`: the paper title is ``#``, sections, the abstract and the
bibliography are ``##``, subsections ``###``, anything deeper ``####``.

Only the ``ltx_document`` article is rendered; arXiv's page chrome (header, nav,
footer, the issue-report dialog) sits outside it. Math is its LaTeX source from
``alttext`` — ``$…$`` inline, ``$$…$$`` for display — never the MathML beside it.
Images are dropped and captions kept, as for converter output.
"""

import re
from html.parser import HTMLParser

# HTML elements that never take an end tag.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)

# Subtrees with nothing to read: scripting, UI, and LaTeXML's own table of contents.
_SKIP_TAGS = frozenset({"script", "style", "svg", "button", "dialog", "form", "nav", "annotation"})

# LaTeXML's markers for undefined macros and footnote marks: noise in running text.
_SKIP_CLASSES = frozenset({"ltx_ERROR", "ltx_note_mark", "ltx_TOC"})

_BLOCK_TAGS = frozenset(
    {
        "article",
        "section",
        "div",
        "p",
        "figure",
        "figcaption",
        "blockquote",
        "header",
        "footer",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "table",
        "pre",
    }
)

# Heading depth by LaTeXML title class; ``<hN>`` numbering varies by document class.
_TITLE_LEVELS = {
    "ltx_title_document": 1,
    "ltx_title_part": 2,
    "ltx_title_chapter": 2,
    "ltx_title_section": 2,
    "ltx_title_appendix": 2,
    "ltx_title_abstract": 2,
    "ltx_title_bibliography": 2,
    "ltx_title_subsection": 3,
}
_DEEPEST_LEVEL = 4

_WHITESPACE_RE = re.compile(r"\s+")

# A body line opening with ``#`` — a code comment, a ``#1`` — would parse as a heading.
_LEADING_HASH_RE = re.compile(r"^#", re.MULTILINE)

# Bounds a malformed ``colspan``/``rowspan`` so one cell can't inflate the table.
_MAX_SPAN = 100


class _Node:
    __slots__ = ("attrs", "children", "classes", "tag")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.classes = frozenset(attrs.get("class", "").split())
        self.children: list[_Node | str] = []


class _TreeBuilder(HTMLParser):
    """A forgiving DOM: an unmatched end tag is ignored, an unclosed one closes with its parent."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k: v or "" for k, v in attrs})
        self._stack[-1].children.append(node)
        if tag not in _VOID:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._stack[-1].children.append(_Node(tag, {k: v or "" for k, v in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self._stack[-1].children.append(data)


def _collapse(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _skipped(node: _Node) -> bool:
    return node.tag in _SKIP_TAGS or not _SKIP_CLASSES.isdisjoint(node.classes)


def _math(node: _Node) -> str:
    """LaTeX source from ``alttext``; MathML children are never read."""
    tex = _collapse(node.attrs.get("alttext", ""))
    if not tex:
        return ""
    return f"$${tex}$$" if node.attrs.get("display") == "block" else f"${tex}$"


def _heading_level(node: _Node) -> int | None:
    if not node.tag.startswith("h") or not node.tag[1:].isdecimal():
        return None
    for cls, level in _TITLE_LEVELS.items():
        if cls in node.classes:
            return level
    # Subsubsection, paragraph and anything deeper, or an ``<hN>`` without a title class.
    return _DEEPEST_LEVEL if "ltx_title" in node.classes else min(int(node.tag[1:]), _DEEPEST_LEVEL)


def _inline(node: _Node | str) -> str:
    """A subtree flattened to one line of text, math as LaTeX."""
    if isinstance(node, str):
        return node
    if _skipped(node):
        return ""
    if node.tag == "math":
        return _math(node)
    if node.tag in ("br", "img"):
        return " "
    text = "".join(_inline(child) for child in node.children)
    # A footnote's text reads inline beside the sentence it annotates.
    if "ltx_note_outer" in node.classes:
        return f" [{_collapse(text)}] "
    if node.tag in _BLOCK_TAGS or node.tag in ("td", "th", "tr"):
        return f" {text} "
    return text


def _equation(node: _Node) -> str:
    """A display equation table as ``$$…$$ (n)``, one line per row."""
    rows = []
    for row in _descendants(node, "tr"):
        tag = ""
        parts = []
        for cell in row.children:
            if isinstance(cell, str):
                continue
            if "ltx_eqn_eqno" in cell.classes:
                tag = _collapse(_inline(cell))
            else:
                parts.append(_inline(cell))
        line = _collapse(" ".join(parts))
        if line:
            rows.append(f"{line} {tag}".rstrip())
    return "\n".join(rows)


def _span(cell: _Node, attr: str) -> int:
    value = cell.attrs.get(attr, "1")
    return min(int(value), _MAX_SPAN) if value.isdecimal() and int(value) > 1 else 1


def _table(node: _Node) -> str:
    """A tabular as a pipe table, the first row taken as the header.

    A spanning cell fills the columns and rows it covers with blanks, so every
    later cell stays under its own header.
    """
    rows = []
    # Column index -> how many more rows a rowspan above still covers it.
    covered: dict[int, int] = {}

    def skip_covered(cells: list[str]) -> None:
        while covered.get(len(cells), 0) > 0:
            covered[len(cells)] -= 1
            cells.append("")

    for row in _descendants(node, "tr"):
        cells: list[str] = []
        for cell in row.children:
            if not (isinstance(cell, _Node) and cell.tag in ("td", "th")):
                continue
            skip_covered(cells)
            start = len(cells)
            colspan, rowspan = _span(cell, "colspan"), _span(cell, "rowspan")
            cells.append(_collapse(_inline(cell)).replace("|", "\\|"))
            cells.extend([""] * (colspan - 1))
            if rowspan > 1:
                covered.update(dict.fromkeys(range(start, start + colspan), rowspan - 1))
        # A rowspan past this row's last cell still covers its column here.
        while any(n > 0 and col >= len(cells) for col, n in covered.items()):
            if covered.get(len(cells), 0) > 0:
                covered[len(cells)] -= 1
            cells.append("")
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    lines = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
    lines.insert(1, "|" + " --- |" * width)
    return "\n".join(lines)


def _descendants(node: _Node, tag: str) -> list[_Node]:
    """Every ``tag`` element under ``node``, not descending into a match."""
    found: list[_Node] = []
    for child in node.children:
        if isinstance(child, _Node):
            if child.tag == tag:
                found.append(child)
            else:
                found.extend(_descendants(child, tag))
    return found


class _Renderer:
    def __init__(self) -> None:
        self.blocks: list[str] = []
        self._inline: list[str] = []

    def flush(self) -> None:
        text = _collapse("".join(self._inline))
        self._inline = []
        if text:
            self.blocks.append(_LEADING_HASH_RE.sub(r"\\#", text))

    def block(self, text: str) -> None:
        self.flush()
        if text.strip():
            self.blocks.append(_LEADING_HASH_RE.sub(r"\\#", text.strip()))

    def heading(self, level: int, text: str) -> None:
        self.flush()
        self.blocks.append(f"{'#' * level} {text}")

    def walk(self, node: _Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                self._inline.append(child)
            else:
                self.element(child)

    def element(self, node: _Node) -> None:
        if _skipped(node):
            return
        if (level := _heading_level(node)) is not None:
            self.heading(level, _collapse(_inline(node)))
        elif node.tag == "math":
            if node.attrs.get("display") == "block":
                self.block(_math(node))
            else:
                self._inline.append(_math(node))
        elif node.tag == "table":
            if "ltx_equation" in node.classes or "ltx_eqn_table" in node.classes:
                self.block(_equation(node))
            else:
                self.block(_table(node))
        elif node.tag in ("ul", "ol"):
            self.flush()
            ordered = node.tag == "ol"
            items = [c for c in node.children if isinstance(c, _Node) and c.tag == "li"]
            for n, item in enumerate(items, start=1):
                marker = f"{n}." if ordered and "ltx_bibitem" not in item.classes else "-"
                self.blocks.append(f"{marker} {_collapse(_inline(item))}")
        elif node.tag == "img":
            return
        elif node.tag in _BLOCK_TAGS:
            self.flush()
            self.walk(node)
            self.flush()
        else:
            self._inline.append(_inline(node))


def _document(root: _Node) -> _Node:
    """The ``ltx_document`` article, or the whole tree when there is none."""
    stack: list[_Node] = [root]
    while stack:
        node = stack.pop()
        if "ltx_document" in node.classes:
            return node
        stack.extend(c for c in reversed(node.children) if isinstance(c, _Node))
    return root


def to_markdown(html: str) -> str:
    """Render arXiv's LaTeXML HTML as markdown, one blank line between blocks."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()

    renderer = _Renderer()
    renderer.walk(_document(builder.root))
    renderer.flush()
    return "\n\n".join(renderer.blocks) + "\n" if renderer.blocks else ""
