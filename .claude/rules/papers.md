---
paths:
  - "src/academic_tools_mcp/papers/*.py"
---

# PDF + content pipeline

## Conversion and the caches

**Two candidate passes, one ordering.** `_shallowest_first` governs both — two
passes with two orderings is the bug this shape prevents, since a plain path sort
inverts the depth rule depending on how the subdirectory happens to be named.

**`_run_command` is the one subprocess driver, but what a mode says about an
outcome stays with that mode.** Don't push the error dicts into the driver to
"finish" the DRY; that is where the two genuinely disagree.

**The full-conversion write path holds only the global lock, not `sections_lock`** —
it is the one markdown writer outside the per-paper lock discipline, which is why
`store_markdown_and_index` may not re-read the file to checksum it.

**`drop_derived()` is the only markdown unlinker, and every caller holds
`sections_lock`.** It drops markdown and section index *together*: dropping one
leaves a reader matching a checksum against bytes that no longer exist. **Don't
unlink markdown anywhere else.**

### Sections and in-paper search

- **`_scan()` is the only heading scan**, and its readers span modules —
  `section_boundaries`, `has_detected_sections`, `parse_sections`,
  `parse_sections_and_detect`, `find_in_markdown`, `get_section_content` and
  `corpus.search`. A private copy that drops the empty-section filter names a
  section the reader's index doesn't have; one returning a title instead of an
  index dead-ends on "Ambiguous section title".
- **`Section.body(lines)` is the one body recipe**, which is what makes a hit's
  `char_offset` an offset into the text the reader returns. Two hand-spelled
  `"\n".join(lines[s:e]).strip()` cannot be relied on to stay equal.
- **Title lookup tries exact before substring, and folds diacritics only as a
  fallback.** Drop the exact tier and a title copied verbatim off the section index
  dead-ends on "Ambiguous section title" whenever a longer heading contains it;
  **fold both passes unconditionally and a paper carrying both spellings
  (`"Resume"`, `"Résumé"`) stops resolving either.**
- **`_section_locks` holds its locks weakly, and must never be bounded by count
  instead.** `release()` clears `locked()` before the waiter it woke has resumed, so
  any eviction pass reads a lock that is about to be entered as free — drop it and
  the waiter holds an orphan while the next caller builds a second `Lock` for the
  same paper.

## Rendering a provider's own markup

**Row and cell collection stops at a match; never walk the whole subtree.** Each
renderer carries its own `_descendants` for its own node type, and neither may fall
back to `iter()`: descending into a nested table pulls the inner rows into the
enclosing one *and* emits them again alone.

**The renderers' heading levels exist to feed `sections._scan`, not to mirror the
source.** `latexml._TITLE_LEVELS` maps title classes and JATS counts section
nesting, both landing on `_SECTION_LEVELS` and `_SUB_LEVEL` — emit a depth scheme
of your own and the reader cannot see the sections.

**`blocks.py` is where both renderers escape and build tables, and its escaping
tracks `sections._HEADING_RE`.** Widen it and `#`-leading prose reaches the agent
backslashed; narrow it and a listing's comment line opens a section.
