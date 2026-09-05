---
paths:
  - "src/academic_tools_mcp/cache_search.py"
---

# Corpus search — BM25 over converted markdown

## cache_search.py

### Scope and tokenisation

BM25 keyword search across every cached markdown file (`.cache/<namespace>/markdown/*.md`), ranked by SQLite FTS5's built-in `bm25()`. FTS5 builds the index, not `_tokenize`, which survives for snippet centring and for the stopword list `_query_words` reuses — a standard English list minus the terms that carry content in academic prose ("all", "no", "not", "very").

### What a hit returns

Chain on the **index**: titles are not unique (10.9% of a real corpus has duplicates), so `get_paper_section(id, title)` dead-ends on "Ambiguous section title" roughly one time in nine. Snippet and section offsets must come from `_textnorm.lower_with_map` (see `.claude/rules/utils.md`), never a raw `str.lower()` — including on the default `normalize=False` path, since lowercasing alone is not length-preserving and an unmapped offset drifts the snippet window off the real match. `_section_for_offset` must keep delegating to `papers.section_at_offset`: a fourth dialect of the same scan lacks the empty-section filter and can name a section the reader's own index has dropped.

### Identifier inversion

Filename → canonical-ID inversion is per-namespace because DOI suffixes may legitimately contain underscores — only slashes a known prefix introduced can be restored. arXiv old-style stems (`_ARXIV_OLDSTYLE_STEM_RE`) restore the single slash in **every** archive, not just the hyphenated physics ones; new-style IDs start with a digit and pass through. bioRxiv/ACL use exact-prefix repairs; manual identifiers pass through unchanged.

### The FTS5 index

**Persistent incremental index — SQLite FTS5** at `.cache/__search_index__/index.db` (WAL, `synchronous=NORMAL`, opened per call — SQLite connections aren't thread-shareable). A `files` table holds `(ns, stem, mtime_ns, size, unindexable)`; two **contentless** (`content=''`, `contentless_delete=1`) FTS5 tables hold the postings. Contentless because the markdown is already on disk and the top-`k` winners are re-read for snippets anyway — storing the text twice buys nothing; `contentless_delete=1` is what lets a removed paper be DELETEd. Two tables rather than one because `normalize` is a query-time flag in this API but diacritic folding is a **build-time** tokenizer option in FTS5: `fts` uses `unicode61 remove_diacritics 0`, `fts_norm` uses `remove_diacritics 2`, and the flag selects between them, preserving the parameter's exact meaning.

### Staleness and self-healing

`_refresh_index()` (under the module-level `_INDEX_LOCK`, since `search` runs in worker threads) walks the corpus with `os.scandir`, which pays one stat per file where `glob` + `Path.stat()` pays two, compares each file's `(mtime_ns, size)` against the recorded row, re-indexes only what changed, and deletes rows whose file is gone. `search(force_refresh=True)` bypasses the staleness signal for the rare same-mtime-same-size edit. `_sweep_legacy_index` deletes the old `index.json` on the way past; `_connect` self-heals a corrupt or non-database file by unlinking it (plus `-wal`/`-shm`) and rebuilding, and `_ensure_schema` rebuilds on a `_SCHEMA_VERSION` mismatch. The reserved `__search_index__` dir has no `markdown/` subdir, so the scan skips it.

### Ranking

FTS5's `bm25()` returns a negative score most-relevant-first; it is flipped so the response keeps "higher is better", and rounded to 6 decimals — **invariant: every hit scores above zero**, and 3 decimals rounds a degenerate-IDF score to `0.0` (guarded by `tests/test_cache_search.py::test_zero_score_hits_dropped`). Corpus statistics are **global**: `namespace` selects which documents come back, not how they rank, so a paper scores the same in a filtered and an unfiltered search. Equal scores tie-break on `(ns, stem)`, since FTS5 orders by rank alone and would otherwise fall back to insertion order.

### Query tokenisation

The query must be tokenised by FTS5, not `_tokenize`: `_tokenize`'s ASCII-only regex splits "Gutiérrez" into `guti OR rrez` and reduces a wholly non-Latin query to nothing, disagreeing with an index that stored the term fine. So `_query_words` whitespace-splits, `_fts_query` double-quotes each word (an unquoted `NOT`/`OR`/`*`/`-`/`:` parses as an operator or raises), and `search` gates on the MATCH expression being non-empty — never on `_tokenize`. Two things still route through `_tokenize`: the stopword and single-character filter (`unicode61` strips neither, so an unfiltered "the" ORs in a term matching the whole corpus), and `_snippet_terms`, which unions tokenised with raw-lowercased words because `_extract_snippet`'s word-boundary scan needs punctuation stripped *and* the accented form intact — otherwise the snippet centres on the document head and reports no `section_index`, leaving a real hit unnavigable.

### `unindexable` and the CJK trade

**`unindexable`** records, per file, why a document can never match (`no_indexable_tokens`, `unreadable`) rather than letting it be silently absent. The probe matches what `unicode61` means by "no terms" — no Unicode letter or digit anywhere (`_ALNUM_RE`), so punctuation-only and empty files are caught while a paper in any script is not. Note `unicode61` does not segment CJK: a run delimited by whitespace or punctuation is one token, so CJK papers are indexed and findable by whole runs but not by sub-phrases — a deliberate trade against `trigram`'s index size (the measurement is in `CHANGELOG.md`). Recall is pure keyword match — a "self-attention" query won't surface "scaled dot-product attention".
