---
paths:
  - "src/academic_tools_mcp/corpus.py"
---

# Corpus search — BM25 over converted markdown

**`corpus.py` carries ~35 docstrings and dense inline comments; they are the home
for how this module works.** This file covers only the cross-module contracts and
the two upstream facts about FTS5 that no comment beside a line can carry.

## Cross-module contracts

- **`_filename_to_canonical`'s output is the `canonical_id` an agent chains back
  into the paper tools** — a stem that doesn't round-trip is a hit that goes
  nowhere. The router and the inverter must match the same grammar, which is why
  both `_ARXIV_OLDSTYLE_STEM_RE` and `arxiv.is_arxiv_id` are built from
  `providers.arxiv`'s exported `OLD_ARCHIVE_PATTERN` / `OLD_NUMBER_PATTERN`, and
  why `_NAMESPACE_DOI_PREFIXES` holds `biorxiv.DOI_PREFIX` / `acl.ACL_DOI_PREFIX`
  rather than respelling them. Spell either out again and they drift.
- **The `manual` namespace is mostly publisher DOIs, not the freeform labels the
  name suggests** — `manual.resolve_target` sends every non-arXiv/bioRxiv/ACL DOI
  there. A suffix carrying further slashes still round-trips imperfectly.
- **`search` must keep taking the section from `papers.section_at_offset` and the
  hit's title from `papers.first_section_heading`.** A local copy of that scan
  drops the empty-section filter and names a section the reader's own index does
  not have (`.claude/rules/pipeline.md`).
- **`section_index` is the chainable handle, never the bare `section` title** —
  headings repeat within one paper often enough that `get_paper_section(id, title)`
  dead-ends on "Ambiguous section title".
- **`unindexable` records carry `canonical_id`, inverted by the same
  `_filename_to_canonical` a hit uses**, so both readers of the `files` table name
  a paper identically and the "use `find_in_paper` on them" advice is actionable.
  A bare `stem` is a filename, not an identifier any tool resolves.

## Two FTS5 facts, and what they cost

**FTS5 cannot decrement a contentless table's corpus statistics on `DELETE`** — it
has no stored content to subtract — so every replaced or removed document
permanently inflates the `N` and average length `bm25()` divides by. Unbounded,
that is not cosmetic: as `N` inflates, a rare term's IDF and a common term's
converge, rarity stops being rewarded, and **the same query over the same files
returns different papers**. `_reset_postings` and the `_churn` counter exist for
that; the mechanism is in their own comments.

**`unicode61` does not segment CJK.** A run delimited by whitespace or
punctuation is one token, so CJK papers are indexed and findable by whole runs
but not by sub-phrases — a deliberate trade against `trigram`'s index size (the
measurement is in `CHANGELOG.md`). Separately, `unicode61` treats `_` as a
separator while Python's `\b` counts it as a word character, so
`attention_model` is findable but not centrable: `char_offset` and
`section_index` come back `null`. Report that, never a section picked from
offset 0.

## Two edits that look safe and aren't

- **`_scan_markdown` takes no namespace filter, and must not grow one.** The
  prune deletes every indexed row the walk did not return, so a filtered walk
  wipes every other namespace.
- **A broken index is not an empty corpus.** `search` lets `sqlite3.Error`
  propagate and `search_cached_papers` turns it into `{error, retryable,
  suggestion}`. Quoting every word left FTS5 no syntax error to raise here, so a
  blanket catch only swallows "database is locked" / "no such table" and answers
  a real question with a confident "none" — the exact failure mode `unindexable`
  exists to prevent.

Keep `_STOPWORDS` deliberately under-aggressive: it is far shorter than a
standard English list because words that carry content here ("all", "no", "not",
"very") must stay out of it.
