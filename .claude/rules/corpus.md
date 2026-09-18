---
paths:
  - "src/academic_tools_mcp/corpus.py"
---

# Corpus search — BM25 over converted markdown

## Cross-module contracts

- **`_filename_to_canonical`'s output is the `canonical_id` an agent chains back
  into the paper tools** — a stem that doesn't round-trip is a hit that goes
  nowhere. The router and the inverter must be built from the same exported
  patterns (`arxiv.OLD_ARCHIVE_PATTERN`, `biorxiv.DOI_PREFIX`); spell either out
  again and they drift.
- **`search` must keep taking its section from `papers.section_at_offset` and its
  title from `papers.first_section_heading`.** A local heading scan drops the
  empty-section filter and names a section the reader's own index does not have.
- **`unindexable` records carry `canonical_id`**, inverted by the same
  `_filename_to_canonical` a hit uses, so both readers of the `files` table name a
  paper identically and the "use `find_in_paper` on them" advice is actionable.

## Two FTS5 facts, and what they cost

**FTS5 cannot decrement a contentless table's corpus statistics on `DELETE`**, so
every replaced document permanently inflates the `N` and average length `bm25()`
divides by. Unbounded, that is not cosmetic: as `N` inflates, a rare term's IDF and
a common term's converge, rarity stops being rewarded, and **the same query over the
same files returns different papers.** `_reset_postings` and `_churn` exist for that.

**`unicode61` does not segment CJK**, so a whitespace- or punctuation-delimited run
is one token — a deliberate trade against `trigram`'s index size, measured in
`CHANGELOG.md`. Separately, `unicode61` treats `_` as a separator while Python's
`\b` counts it as a word character, so `attention_model` is findable but not
centrable.

## Two edits that look safe and aren't

- **A broken index is not an empty corpus.** Quoting every word left FTS5 no syntax
  error to raise here, so a blanket `except sqlite3.Error` only swallows "database
  is locked" / "no such table" and answers a real question with a confident "none"
  — the exact failure `unindexable` exists to prevent.
- **Keep `_STOPWORDS` deliberately under-aggressive.** It is far shorter than a
  standard English list because words that carry content here must stay out of it.
