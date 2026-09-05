---
paths:
  - "src/academic_tools_mcp/cache_search.py"
---
# Corpus search — BM25 over converted markdown

## cache_search.py

### Scope and tokenisation

BM25 keyword search across every cached markdown file (`.cache/<namespace>/markdown/*.md`), ranked by SQLite FTS5's built-in `bm25()` (k1=1.2, b=0.75 — FTS5's defaults). FTS5 builds the index, not `_tokenize`, which survives for snippet centring and for the stopword list `_query_words` reuses — the classic 50 minus content-bearing terms like "all", "no", "not", "very" that matter in academic prose — and it preserves intra-word hyphens / dots so `self-attention` and `BM25` survive intact.

### What a hit returns

For each top hit, returns the document title (first H1/H2), a ~200-char snippet centred on the position with the most distinct query terms cooccurring nearby, and the H1/H2 section the snippet falls under — as `section_index` (chainable) alongside the human-readable `section`, plus the hit's `char_offset`. Chain on the **index**: titles are not unique (10.9% of a real 2,493-paper corpus has duplicates), so `get_paper_section(id, title)` dead-ends on "Ambiguous section title" roughly one time in nine. Snippet and section offsets must come from `_textnorm.lower_with_map` (see `.claude/rules/utils.md`), never a raw `str.lower()` — including on the default `normalize=False` path, since lowercasing alone is not length-preserving and an unmapped offset drifts the snippet window off the real match.

### Result ordering

Results are ordered by score then `(namespace, stem)` so equal-scoring hits are deterministic across sessions even as the incremental index reorders entries; `top_k<=0` returns `[]`.

### Identifier inversion

Filename → canonical-ID inversion is per-namespace. arXiv inverts old-style IDs in **every** archive via regex (`_ARXIV_OLDSTYLE_STEM_RE`: `archive[.subj]_NNNNNNN` → `archive[.subj]/NNNNNNN`, e.g. `hep-th_9901001` → `hep-th/9901001`, `cs_0501001` → `cs/0501001`, `math.gt_0309136` → `math.gt/0309136`); new-style IDs start with a digit and pass through. bioRxiv/ACL use exact-prefix repairs (`10.1101_X` → `10.1101/X`, `10.18653_v1_X` → `10.18653/v1/X`); manual identifiers pass through unchanged.

### The FTS5 index

**Persistent incremental index — SQLite FTS5** at `.cache/__search_index__/index.db` (WAL, `synchronous=NORMAL`; opened per call, since connections aren't shareable across the `asyncio.to_thread` worker threads `search` runs in, and opening costs microseconds). A `files` table holds `(ns, stem, mtime_ns, size, unindexable)`; two **contentless** (`content=''`, `contentless_delete=1`) FTS5 tables hold the postings. Contentless is what keeps the database smaller than the corpus it indexes — the markdown is already on disk and the top-`k` winners are re-read for snippets anyway, so the text is never stored twice. Two tables rather than one because `normalize` is a query-time flag in this API but diacritic folding is a **build-time** tokenizer option in FTS5: `fts` uses `unicode61 remove_diacritics 0`, `fts_norm` uses `remove_diacritics 2`, and the flag selects between them, preserving the parameter's exact meaning.

### Staleness and self-healing

`_refresh_index()` (under the module-level `_INDEX_LOCK`, since `search` runs in worker threads) walks the corpus with `os.scandir` — each entry's stat comes from the dirent rather than a second syscall — compares each file's `(mtime_ns, size)` against the recorded row, re-indexes only what changed, and deletes rows whose file is gone. `search(force_refresh=True)` bypasses the staleness signal for the rare same-mtime-same-size edit. `_sweep_legacy_index` deletes the old `index.json` on the way past; `_connect` self-heals a corrupt or non-database file by unlinking it (plus `-wal`/`-shm`) and rebuilding, and `_ensure_schema` rebuilds on a `_SCHEMA_VERSION` mismatch. The reserved `__search_index__` dir has no `markdown/` subdir, so the scan skips it.

### Ranking

**Ranking** is FTS5's built-in `bm25()`, which returns a negative score most-relevant-first; it is flipped so the response keeps "higher is better", and rounded to 6 decimals (3 crushed a degenerate-IDF hit to `0.0` and broke the "every hit scores above zero" invariant). Corpus statistics are **global**: `namespace` selects which documents come back, not how they rank, so a paper scores the same in a filtered and an unfiltered search. Equal scores tie-break on `(ns, stem)`, since FTS5 orders by rank alone and would otherwise fall back to insertion order.

### Query tokenisation

**Query handling is deliberately split from `_tokenize`.** FTS5 tokenises the documents, so the query must be tokenised by FTS5 too: `_query_words` whitespace-splits and `_fts_query` double-quotes each word (an unquoted `NOT`/`OR`/`*`/`-`/`:` would be parsed as an operator, or raise). Both sides must use the same tokenizer: `_tokenize`'s ASCII-only regex would split "Gutiérrez" into `guti OR rrez` and gate a wholly non-Latin query out before FTS5 ever saw it, even though the index holds the term. What `_query_words` *does* keep from `_tokenize` is the stopword and single-character filter: the index stores raw markdown under `unicode61`, which strips neither, so an unfiltered "the" would OR in a term matching the whole corpus. `search` gates on the MATCH expression being non-empty (an empty one is an FTS5 syntax error) and returns `[]` before touching the index.

### Snippet terms

`_snippet_terms` unions `_tokenize`'s output with the folded/lowercased raw words, because `_extract_snippet`'s word-boundary scan needs punctuation stripped (`_tokenize`'s job) *and* needs the accented or non-Latin form intact (which `_tokenize` destroys) — otherwise a hit the index found came back centred on the document head with no `section_index`, unnavigable.

### `unindexable` and the CJK trade

**`unindexable`** records, per file, why a document can never match (`no_indexable_tokens`, `unreadable`) rather than letting it be silently absent; `search_cached_papers` surfaces it as `unindexable_count` / `unindexable` / `unindexable_note` only when non-empty. The probe matches what `unicode61` means by "no terms" — no Unicode letter or digit anywhere (`_ALNUM_RE`), so punctuation-only and empty files are caught while a paper in any script is not. Note `unicode61` does not segment CJK: a run delimited by whitespace or punctuation is one token, so CJK papers are indexed and findable by whole runs but not by sub-phrases — a deliberate trade (`trigram` would have cost 842 MB against 165 MB). Recall is pure keyword match — a "self-attention" query won't surface "scaled dot-product attention".
