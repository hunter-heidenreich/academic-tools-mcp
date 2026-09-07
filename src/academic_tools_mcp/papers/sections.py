"""Markdown structure: sections, sub-headings, and in-document search.

Pure — text in, dicts out. No filesystem, no cache, no asyncio, so the corpus
search and the section reader can both depend on it without pulling in the
converter.

Section splitting is fixed, not adaptive — see ``_SECTION_LEVELS``.

**One heading scan, one set of boundaries.** ``parse_sections``,
``find_in_markdown``, ``get_section_content`` and ``cache_search.search`` all
route through :func:`_scan`; ``first_section_heading`` is the one deliberate
exception, short-circuiting on the first heading rather than parsing the whole
document. A second implementation is agent-visible, not merely untidy: drop the
empty-section filter and a search hit names a section the reader's index does
not have; return a title instead of an index and the agent's chain into
``get_paper_section`` dies on "Ambiguous section title" whenever a paper repeats
a heading.
"""

import re
from typing import Any

from .. import _textnorm

# Approximate tokens per character (conservative estimate for English text)
_CHARS_PER_TOKEN = 4

# Heading lines: captures (level, title). Matched per line, so no re.MULTILINE.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")

# H1 and H2 both open a new section (converters disagree on which level the
# paper title gets), H3 is the sub-heading level, H4+ are ignored entirely.
_SECTION_LEVELS: frozenset[int] = frozenset({1, 2})
_SUB_LEVEL: int = 3


class Section:
    r"""One section's span in a markdown document.

    ``start``/``end`` are line indices into ``markdown.split("\n")``; the
    heading line itself is excluded, so ``lines[start:end]`` is the body.
    """

    __slots__ = ("end", "h3s", "start", "title")

    def __init__(self, title: str, start: int, end: int, h3s: list[str]) -> None:
        """Bind the heading title, its body span and any h3 subheadings."""
        self.title = title
        self.start = start
        self.end = end
        self.h3s = h3s

    def body(self, lines: list[str]) -> str:
        """The section's text, stripped — exactly what a reader receives."""
        return "\n".join(lines[self.start : self.end]).strip()


def _scan(markdown: str) -> tuple[list[Section], bool]:
    """The one heading scan: ``(spans, any_real_section_heading)``.

    Sections whose body is blank are dropped, so the indices returned here are
    the indices ``get_section_content`` accepts. Detection rides along rather
    than being recomputed, so a caller needing both makes one pass.
    """
    lines = markdown.split("\n")
    spans: list[Section] = []
    title = "Preamble"
    start = 0
    h3s: list[str] = []
    detected = False

    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        if level in _SECTION_LEVELS:
            detected = True
            spans.append(Section(title, start, i, h3s))
            title = m.group(2).strip()
            start = i + 1
            h3s = []
        elif level == _SUB_LEVEL:
            h3s.append(m.group(2).strip())

    spans.append(Section(title, start, len(lines), h3s))
    return [sp for sp in spans if sp.body(lines)], detected


def section_boundaries(markdown: str) -> list[Section]:
    """Split ``markdown`` into sections at H1/H2 headings. See :func:`_scan`."""
    return _scan(markdown)[0]


def has_detected_sections(markdown: str) -> bool:
    """Whether any real H1/H2 heading was found.

    A document with none collapses to one synthetic "Preamble", indistinguishable
    from a paper that genuinely has one section unless the caller is told.
    """
    return _scan(markdown)[1]


def first_section_heading(markdown: str) -> str | None:
    """The document's first H1/H2 heading text, or ``None`` if it has none.

    Not ``section_boundaries(md)[0].title``, which is ``"Preamble"`` for the
    span before the first heading — and which drops a heading with no body.
    """
    for line in markdown.split("\n"):
        m = _HEADING_RE.match(line)
        if m is not None and len(m.group(1)) in _SECTION_LEVELS:
            return m.group(2).strip()
    return None


def section_at_offset(markdown: str, offset: int) -> tuple[int, str] | None:
    """Return ``(section_index, title)`` for a character offset, or None.

    The index is the one ``get_section_content`` accepts, so a corpus-search
    hit can be chained straight into ``get_paper_section`` without going
    through an ambiguous title.
    """
    if offset < 0:
        return None
    spans = section_boundaries(markdown)
    if not spans:
        return None

    # Newlines strictly before the offset; str.count clamps its own end.
    line_no = markdown.count("\n", 0, offset)
    # Spans are ordered and disjoint, so the first ending past line_no contains
    # it. A heading line therefore resolves forward, to the section it opens.
    for index, sp in enumerate(spans):
        if line_no < sp.end:
            return index, sp.title
    # Past the last surviving span: a trailing empty section was filtered out.
    last = len(spans) - 1
    return last, spans[last].title


def parse_sections(markdown: str) -> list[dict[str, Any]]:
    """Parse markdown into sections with sub-heading previews.

      {"index": 0, "title": "Introduction", "h3s": ["Background"], "approx_tokens": 800}

    Content before the first heading is captured as a "Preamble" section.
    """
    return _section_dicts(markdown.split("\n"), section_boundaries(markdown))


def parse_sections_and_detect(markdown: str) -> tuple[list[dict[str, Any]], bool]:
    """:func:`parse_sections` and :func:`has_detected_sections` in one scan.

    What every writer of a sections-cache entry wants; the entry carries both.
    """
    spans, detected = _scan(markdown)
    return _section_dicts(markdown.split("\n"), spans), detected


def _section_dicts(lines: list[str], spans: list[Section]) -> list[dict[str, Any]]:
    """Render scanned spans as the agent-facing section index."""
    return [
        {
            "index": index,
            "title": sp.title,
            "h3s": list(sp.h3s),
            # The stripped body, so the index and the reader agree.
            "approx_tokens": max(1, len(sp.body(lines)) // _CHARS_PER_TOKEN),
        }
        for index, sp in enumerate(spans)
    ]


# Snippet window around an in-paper match. ~60 chars on each side gives
# the agent enough context to recognise relevance without overflowing.
_FIND_SNIPPET_WINDOW = 60


def find_in_markdown(
    markdown: str,
    query: str,
    *,
    max_results: int = 20,
    case_sensitive: bool = False,
    whole_words: bool = False,
    normalize: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    r"""Scan markdown for ``query``: ``(hits, truncated)``.

    Each hit is ``{section_index, section, char_offset, match, snippet}``. The
    agent-facing description of the flags is ``find_in_paper``'s docstring; what
    belongs here is why the offsets chain — this and ``get_section_content``
    both slice through :meth:`Section.body`, so a ``char_offset`` indexes the
    text the reader returns.

    Under ``normalize`` the match runs on folded text but every reported value
    is sliced from the original, via ``_textnorm``'s position map. A query
    matching part of one character's expansion (the "f" of a "fi" ligature)
    reports the whole original character as ``match``.

    ``truncated`` means more matches exist beyond ``max_results``, not merely
    that the cap was reached.
    """
    if not query:
        return [], False

    lines = markdown.split("\n")
    spans = section_boundaries(markdown)

    if normalize:
        folded_query = _textnorm.fold(query)
        if not folded_query:
            # Query was entirely combining marks — an empty pattern would
            # match at every position, so there is nothing to find.
            return [], False
        pattern = re.escape(folded_query)
    else:
        pattern = re.escape(query)
    if whole_words:
        pattern = rf"\b{pattern}\b"
    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(pattern, flags)

    hits: list[dict[str, Any]] = []
    for section_index, span in enumerate(spans):
        section_text = span.body(lines)
        if normalize:
            search_text, index_map = _textnorm.fold_with_map(section_text)
        else:
            search_text, index_map = section_text, None
        for match in regex.finditer(search_text):
            if len(hits) >= max_results:
                return hits, True
            if index_map is None:
                pos = match.start()
                matched = match.group()
            else:
                pos, span_end = _textnorm.original_span(index_map, match.start(), match.end())
                matched = section_text[pos:span_end]
            ws = max(0, pos - _FIND_SNIPPET_WINDOW)
            we = min(len(section_text), pos + len(matched) + _FIND_SNIPPET_WINDOW)
            # Collapse newlines so the snippet renders on one line in
            # the agent's view; the surrounding context stays readable.
            snippet = section_text[ws:we].replace("\n", " ").strip()
            hits.append(
                {
                    "section_index": section_index,
                    "section": span.title,
                    "char_offset": pos,
                    "match": matched,
                    "snippet": snippet,
                }
            )
    return hits, False


def _match_section_title(section: str, spans: list[Section]) -> list[tuple[int, Section]]:
    """Sections whose title contains ``section``, case- and diacritic-insensitively.

    The folded pass runs only when the exact one finds nothing, so folding can
    widen a miss into a hit ("Resume" → "Résumé") but never turns a resolving
    query into an ambiguity error.
    """
    query = section.lower()
    matches = [(i, sp) for i, sp in enumerate(spans) if query in sp.title.lower()]
    if matches:
        return matches
    folded = _textnorm.fold(section).lower()
    return [(i, sp) for i, sp in enumerate(spans) if folded in _textnorm.fold(sp.title).lower()]


def get_section_content(
    markdown: str,
    section: int | str,
    offset: int = 0,
    max_chars: int = 16000,
) -> dict[str, Any]:
    """Retrieve a slice of a section's content by index or title.

    Args:
        markdown: Full markdown text.
        section: Integer index, or a string title matched as a case-insensitive
            substring — falling back to a diacritic-folded comparison when
            nothing matches exactly (see ``_match_section_title``).
        offset: Starting character offset within the section. Defaults to 0.
            Use ``next_offset`` from a previous call to page through.
        max_chars: Slice size in characters. Defaults to 16000 (~4000 tokens).
            Must be positive.

    Returns:
        On success: ``{index, title, content, offset, chars_returned,
        total_chars, approx_tokens, has_more, next_offset}``. ``approx_tokens``
        and ``total_chars`` describe the full section, not the slice.
        On error: ``{"error": ...}`` (lists available titles for unknown
        section names).
    """
    if max_chars <= 0:
        return {"error": f"max_chars must be positive, got {max_chars}"}
    if offset < 0:
        return {"error": f"offset must be non-negative, got {offset}"}

    lines = markdown.split("\n")

    spans = section_boundaries(markdown)

    if not spans:
        # A converter can exit 0 on a 0-page or image-only PDF. Say so, rather
        # than falling through to a literal "out of range (0--1)".
        return {
            "error": "The converted markdown for this paper is empty — no readable text.",
            "suggestion": (
                "The PDF probably has no extractable text layer (a scan or an "
                "image-only document). Re-run convert_paper with force_refresh=True "
                "to try again, or check the PDF opens and contains selectable text."
            ),
            "retryable": False,
        }

    if isinstance(section, int):
        if not 0 <= section < len(spans):
            return {"error": f"Section index {section} out of range (0-{len(spans) - 1})"}
        resolved_index, span = section, spans[section]
    else:
        matches = _match_section_title(section, spans)
        if len(matches) == 1:
            resolved_index, span = matches[0]
        elif len(matches) > 1:
            titles = [sp.title for _, sp in matches]
            return {"error": f"Ambiguous section title '{section}'. Matches: {titles}"}
        else:
            titles = [sp.title for sp in spans]
            return {"error": f"No section matching '{section}'. Available: {titles}"}

    full_content = span.body(lines)
    total_chars = len(full_content)
    approx_tokens = max(1, total_chars // _CHARS_PER_TOKEN)

    if offset > total_chars:
        return {"error": f"offset {offset} is beyond section length {total_chars}"}

    end_offset = min(offset + max_chars, total_chars)
    slice_content = full_content[offset:end_offset]
    has_more = end_offset < total_chars

    return {
        "index": resolved_index,
        "title": span.title,
        "content": slice_content,
        "offset": offset,
        "chars_returned": len(slice_content),
        "total_chars": total_chars,
        "approx_tokens": approx_tokens,
        "has_more": has_more,
        "next_offset": end_offset if has_more else None,
    }
