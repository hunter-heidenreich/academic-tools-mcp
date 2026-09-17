"""bioRxiv's JATS XML to section-structured markdown.

Pure — text in, text out, parsed with ``defusedxml``. The output is shaped for
:mod:`.sections` exactly as :mod:`.latexml`'s is: the article title is ``#``, the
abstract, top-level sections, back matter and the reference list are ``##``,
subsections ``###``, anything deeper ``####``.

Tags match by local name, so HighWire's namespaced attributes and a default
namespace alike are ignored. Math is its LaTeX source (``tex-math``, else MathML
``alttext``) — ``$…$`` inline, ``$$…$$`` for display. Graphics are dropped and
labels and captions kept, as for converter output; a table bioRxiv ships only as
an image is its caption alone.
"""

import re
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from .blocks import escape_headings, pipe_table, span

_DEEPEST_LEVEL = 4

# Subtrees with nothing to read: identifiers, images, and files outside the text.
_SKIP_TAGS = frozenset(
    {
        "object-id",
        "graphic",
        "inline-graphic",
        "media",
        "supplementary-material",
        "alternatives-fallback",
    }
)

# Elements that are sections by another name, titled by their own ``title`` child.
_SECTION_TAGS = frozenset({"sec", "ack", "app", "glossary", "notes", "bio"})

# Containers walked for the blocks inside them.
_CONTAINER_TAGS = frozenset({"body", "app-group", "boxed-text", "fn-group", "fn", "floats-group"})

_WHITESPACE_RE = re.compile(r"\s+")

# A ``tex-math`` body is often a whole LaTeX document around the formula.
_TEX_DOCUMENT_RE = re.compile(r"\\begin\{document\}(.*?)\\end\{document\}", re.DOTALL)


def _local(element: Element) -> str:
    """The tag without its ``{namespace}``."""
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _attr(element: Element, name: str) -> str | None:
    """An attribute by local name, whatever namespace it carries."""
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1] == name:
            return value
    return None


def _collapse(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _child(element: Element, name: str) -> Element | None:
    return next((c for c in element if _local(c) == name), None)


def _tex(element: Element) -> str:
    """A formula's LaTeX, unwrapped from a LaTeX document and its ``$`` delimiters."""
    tex = "".join(element.itertext())
    if m := _TEX_DOCUMENT_RE.search(tex):
        tex = m.group(1)
    return _collapse(tex).strip("$").strip()


def _formula(element: Element) -> str:
    """The LaTeX inside a ``disp-formula`` / ``inline-formula``: ``tex-math``, else ``alttext``."""
    for node in element.iter():
        name = _local(node)
        if name == "tex-math" and (tex := _tex(node)):
            return tex
        if name == "math" and (alt := _collapse(_attr(node, "alttext") or "")):
            return alt
    return ""


def _inline(element: Element) -> str:
    """A subtree flattened to one line of text, math as LaTeX. Tails are the caller's."""
    name = _local(element)
    if name in _SKIP_TAGS:
        return ""
    if name == "inline-formula":
        tex = _formula(element)
        return f"${tex}$" if tex else ""
    if name == "disp-formula":
        tex = _formula(element)
        return f" $${tex}$$ " if tex else ""
    if name == "math":
        alt = _collapse(_attr(element, "alttext") or "")
        return f"${alt}$" if alt else ""
    if name == "tex-math":
        tex = _tex(element)
        return f"${tex}$" if tex else ""
    if name == "break":
        return " "
    parts = [element.text or ""]
    for child in element:
        parts.append(_inline(child))
        parts.append(child.tail or "")
    text = "".join(parts)
    return f" {text} " if name in ("p", "title", "label", "td", "th", "list-item") else text


def _table(element: Element) -> str:
    """A ``table`` as a pipe table (``blocks.pipe_table``)."""
    return pipe_table(
        [
            (_collapse(_inline(cell)), span(_attr(cell, "colspan")), span(_attr(cell, "rowspan")))
            for cell in row
            if _local(cell) in ("td", "th")
        ]
        for row in element.iter()
        if _local(row) == "tr"
    )


class _Renderer:
    def __init__(self) -> None:
        self.blocks: list[str] = []

    def paragraph(self, text: str) -> None:
        if text := _collapse(text):
            self.blocks.append(escape_headings(text))

    def heading(self, level: int, element: Element | None, fallback: str = "") -> None:
        text = _collapse(_inline(element)) if element is not None else ""
        if text := text or fallback:
            self.blocks.append(f"{'#' * min(level, _DEEPEST_LEVEL)} {text}")

    def float_(self, element: Element) -> None:
        """A ``fig`` / ``table-wrap``: label and caption as one paragraph, then any table."""
        label = _child(element, "label")
        caption = _child(element, "caption")
        head = " ".join(_collapse(_inline(e)) for e in (label, caption) if e is not None)
        self.paragraph(head)
        for node in element.iter():
            if _local(node) == "table" and (table := _table(node)):
                self.blocks.append(table)

    def references(self, element: Element) -> None:
        self.heading(2, _child(element, "title"), "References")
        for ref in element:
            if _local(ref) == "ref":
                if text := _collapse(" ".join(_inline(child) for child in ref)):
                    self.blocks.append(f"- {escape_headings(text)}")
            elif _local(ref) == "ref-list":
                self.references(ref)

    def walk(self, element: Element, level: int) -> None:
        """Render ``element``'s children as blocks, sections opening at ``level``."""
        for child in element:
            name = _local(child)
            if name in _SKIP_TAGS or name in ("title", "label"):
                continue
            if name in _SECTION_TAGS:
                self.heading(
                    level, _child(child, "title"), "Acknowledgments" if name == "ack" else ""
                )
                self.walk(child, level + 1)
            elif name == "ref-list":
                self.references(child)
            elif name in ("fig", "table-wrap", "fig-group", "table-wrap-group"):
                self.float_(child)
            elif name == "table":
                self.blocks.append(_table(child))
            elif name == "disp-formula":
                if tex := _formula(child):
                    self.blocks.append(f"$${tex}$$")
            elif name in ("list", "def-list"):
                for item in child:
                    if text := _collapse(_inline(item)):
                        self.blocks.append(f"- {escape_headings(text)}")
            elif name in _CONTAINER_TAGS:
                self.walk(child, level)
            else:
                self.paragraph(_inline(child))


def _render(root: Element) -> list[str]:
    renderer = _Renderer()
    meta = next((e for e in root.iter() if _local(e) == "article-meta"), None)
    if meta is not None:
        title = next((e for e in meta.iter() if _local(e) == "article-title"), None)
        renderer.heading(1, title)
        for abstract in meta:
            # Graphical and teaser abstracts restate the real one.
            if _local(abstract) != "abstract" or _attr(abstract, "abstract-type"):
                continue
            renderer.heading(2, _child(abstract, "title"), "Abstract")
            renderer.walk(abstract, 3)
    for part in root:
        if _local(part) in ("body", "back"):
            renderer.walk(part, 2)
    return renderer.blocks


def to_markdown(xml: str) -> str:
    """Render a JATS article as markdown, one blank line between blocks.

    ``""`` for a document that does not parse, or that ``defusedxml`` refuses — no
    usable rendering, so the caller falls back to the PDF.
    """
    try:
        root = fromstring(xml)
    except (ParseError, DefusedXmlException, ValueError):
        return ""
    blocks = [block for block in _render(root) if block.strip()]
    return "\n\n".join(blocks) + "\n" if blocks else ""
