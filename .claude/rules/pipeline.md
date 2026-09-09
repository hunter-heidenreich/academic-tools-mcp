---
paths:
  - "src/academic_tools_mcp/papers/*.py"
  - "src/academic_tools_mcp/manual.py"
  - "src/academic_tools_mcp/fast_extract.py"
---

# PDF + content pipeline

**How each function works is in its own docstring**, and `papers/__init__.py`
states the three-module split. This file covers only the cross-module invariants
and the edits that look safe and aren't.

## Layout

Artifact *naming* lives one layer down, in **`store/stems.py`**
(`.claude/rules/store.md`) — it depends only on `cache` + stdlib.
**Invariant: no module under `providers/` imports `papers`**; three of them reach
`stems` directly, and that is the whole reason it sits outside the pipeline. The
placement is what enforces it — `store/` sits below `providers/`, and there is no
converter in it to reach. `tests/test_layering.py` fails if a provider reaches
`papers` at all.

**Invariant: a module has one import name.** `papers/__init__.py` re-exports its
three submodules and deliberately not `stems`. **Patch the owning submodule,
never the facade** — it re-exports by value, so
`monkeypatch.setattr(papers, "_section_locks", ...)` rebinds an alias nothing reads.

## papers/convert.py

- **Placeholder substitution is `shlex.quote`d, and that quoting is the trust
  boundary**: a canonical-derived path cannot inject into the `bash -c` command.
  Templates therefore carry bare `{input}` / `{output_dir}` / `{python}`.
- **The `ConverterTemplateError` `except` set is open, not the three it is
  tempting to enumerate. Do not re-narrow it**, and keep both builders inside
  their caller's `try`, on the fast path too.
- **Two candidate passes, one ordering.** `_shallowest_first` governs both.
  Two passes with two orderings is the bug this shape prevents — a plain path
  sort silently inverts the depth rule depending on how the subdirectory happens
  to be named.
- **`_run_command` is the one subprocess driver**, so cancellation and timeout
  discipline cannot land in one mode and miss the other. But **what a mode says
  about an outcome stays with that mode** — don't push the error dicts into the
  driver to "finish" the DRY. That is where the two genuinely disagree.
- **The full-conversion write path holds only the global lock, not
  `sections_lock`.** That is why `store_markdown_and_index` may not re-read the
  file to checksum it (`.claude/rules/store.md` § Checksums). It is the one
  markdown writer outside the per-paper lock discipline.
- **`drop_derived()` is the only markdown unlinker, and every caller holds
  `sections_lock`** — `convert_pdf`'s `force_refresh` branch, and
  `tools/pipeline`'s `download_pdf` and `import_paper` cascades. The download
  cascade asks `recorded_conversion_mode()` first — the named read for "may I
  replace this markdown?", so the tool layer never reaches into the sections
  cache itself. It drops markdown and section index *together*: dropping one
  leaves a reader matching a checksum against bytes that no longer exist.
  **Don't unlink markdown anywhere else.**

### The sections-cache entry

**Invariant: every entry carries all four of `sections`, `sections_detected`,
`markdown_checksum`, `conversion_mode`**, and both readers subscript rather than
default. A missing `sections_detected` costs a re-parse; a *wrong* one is
reported to the agent as truth — the exact reading `sections_note` exists to
prevent. A new writer goes through `store_markdown_and_index`.

**`_convert_fast`'s cached-markdown branch delegates to
`_reparse_sections_locked` rather than assembling a third writer.** Not
tidiness: a local `recorded_mode or "fast"` stamps an entry predating the field
— `null`, meaning nobody knows — as degraded, and writes that claim to disk
permanently, while `convert_pdf`'s cached branch answers `null` for the identical
state.

### Sections and in-paper search

- **`_scan()` is the only heading scan**, reached by `section_boundaries`,
  `has_detected_sections`, `parse_sections`, `parse_sections_and_detect`,
  `find_in_markdown`, `get_section_content` and `corpus.search`. A private copy
  that drops the empty-section filter names a section the reader's index doesn't
  have; one that returns a title instead of an index dead-ends on "Ambiguous
  section title".
- **`first_section_heading()` answers a different question from
  `section_at_offset`**, and both differ from `section_boundaries(md)[0].title`
  (which is `"Preamble"` for the pre-heading span). A title-page H1 with no body
  is dropped from the section index but is still the right name for a hit.
  `corpus._extract_title` delegates the function rather than re-spelling
  `level <= 2` — the levels constant is private, so there is nothing to copy.
- **`Section.body(lines)` is the one body recipe**, which is what makes a hit's
  `char_offset` an offset into the text the reader returns. Two hand-spelled
  `"\n".join(lines[s:e]).strip()` cannot be relied on to stay equal.
- **Title lookup folds diacritics only as a fallback.** Exact lowercased
  substring pass first, `textnorm.fold` only when it returns nothing — so folding
  can widen a miss into a hit (`"Resume"` → `"Résumé"`) but can never turn a
  query that already resolves into an "Ambiguous section title" error. **Fold
  both passes unconditionally and a paper carrying both spellings stops resolving
  either.**
- **Invariant: the `_section_locks` map is the sole owner of a lock across an
  await.** Every caller writes `async with sections_lock(...)` as one expression,
  and `Lock.acquire` on an uncontended lock returns without yielding — that is
  the whole reason eviction cannot race a caller. Bind the lock to a variable,
  await something, *then* enter it, and the key can be evicted and recreated,
  handing two callers two different `Lock` objects.

## manual.py

- **`_ROUTES` order is load-bearing**: an arXiv id is not a DOI, an ACL DOI is
  one, and the generic-DOI fallback is last.
- **The arXiv shape test is `arxiv.is_arxiv_id`**, called by both
  `resolve_target` and `resolve_metadata_source` — storage and metadata must not
  disagree about which ids are arXiv's. This module routes on three predicates of
  the same shape (`is_arxiv_id`, `biorxiv.is_biorxiv_doi`, `acl.is_acl_doi`) and
  owns none of them.
- **`resolve_metadata_source` is derived from `resolve_target`**, not a second
  pass over the shapes. Two parallel if-chains is the bug this shape prevents —
  ACL is the one namespace that changes hands, its PDFs from the Anthology and
  its metadata from OpenAlex.
- **An id `is_arxiv_id` rejects still gets a canonical key identical to arXiv's**,
  so it lands in `manual` and the same paper caches, downloads and converts twice.
  `migrate_misrouted_arxiv()` re-files what a narrower test left behind.
  Two invariants govern it:
  - **The sweep's candidate set must cover every spelling the router claims** —
    one the router learns and the sweep doesn't strands that paper's artifacts in
    `manual` for good. It **asks the router** rather than matching a shape of its
    own, so it cannot drift from the routing it exists to catch up with.
  - **It returns the recovered *key*, not the source filename.** The legacy
    `manual` key is `doinorm.canonical`, which strips `doi:` and not `arXiv:`, so
    reusing the filename would land `arxiv%3A2301.00001` in a namespace that only
    ever looks for `2301.00001`.
  Deliberately **not** `corpus._filename_to_canonical`: that one repairs the
  slash with each namespace's own *anchored* grammar, which can never match a
  stem carrying the `arXiv:` prefix the legacy `manual` key kept.
- **Both import functions stay synchronous; the async boundary is the tool
  layer.** `tools/pipeline.import_paper` wraps each in `asyncio.to_thread` — an
  arbitrarily large copy or parse run inline stalls every concurrent tool call —
  and holds `papers.sections_lock` across **both** branches, since each replaces
  the markdown + section-index pair `convert_pdf` and the cascade mutate under it.
- **Atomic writes only** — never `shutil.copy2` or `write_text` straight to the
  destination (`.claude/rules/store.md`).
- **This module deliberately does not download arbitrary URLs.** Agents fetch
  non-native PDFs themselves and hand the local file to `import_paper`.
- **Manual imports intentionally have no BibTeX generation** — the manual
  pipeline has no structured metadata. When the identifier is a DOI, chain into
  `get_paper_bibtex`.
