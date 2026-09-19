---
paths:
  - "src/academic_tools_mcp/papers/*.py"
  - "src/academic_tools_mcp/manual.py"
---

# PDF + content pipeline

## papers/

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
- **Title lookup folds diacritics only as a fallback.** Folding can widen a miss
  into a hit (`"Resume"` → `"Résumé"`) but can never turn a query that already
  resolves into an "Ambiguous section title" error. **Fold both passes
  unconditionally and a paper carrying both spellings stops resolving either.**
- **Invariant: the `_section_locks` map is the sole owner of a lock across an
  await.** Bind the lock to a variable, await something, *then* enter it, and the
  key can be evicted and recreated — handing two callers two different `Lock`
  objects.

## manual.py

- **`resolve_target` and `resolve_metadata_source` route on three predicates they
  do not own** (`arxiv.is_arxiv_id`, `biorxiv.is_biorxiv_doi`, `acl.is_anthology_id`),
  because storage and metadata must not disagree about which ids are whose.
- **The `migrate_*` sweeps ask the router** rather than matching a shape of their
  own, so they cannot drift from the routing they exist to catch up with. A
  spelling the router learns and a sweep doesn't strands that paper's artifacts for
  good.
- **Every pipeline entry point trades a non-DOI identifier before routing,
  `import_paper` included** — it is the one that *writes*, so a spelling it files
  under and the readers resolve away is an import nothing reads back. The three
  `refile_*_stems` functions catch up with what an older build stranded, and **a
  new traded shape owes a re-filer**, or every import already made under it orphans
  the day it starts resolving.
- **An identifier changes identity only in `app.resolve_paper_identifier`.** Graph
  tools skip the Anthology trade: they are DOI-keyed.
- **Both import functions stay synchronous; the async boundary is the tool layer.**
  `tools/pipeline.import_paper` wraps each in `asyncio.to_thread` and holds
  `papers.sections_lock` across **both** branches.
- **This module deliberately does not download arbitrary URLs.** Agents fetch
  non-native PDFs themselves and hand the local file to `import_paper`.
- **Manual imports intentionally have no BibTeX generation** — there is no
  structured metadata. When the identifier is a DOI, chain into `get_paper_bibtex`.
