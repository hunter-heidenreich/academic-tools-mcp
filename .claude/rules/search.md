---
paths:
  - "src/academic_tools_mcp/cache_search.py"
---

# Corpus search — BM25 over converted markdown

## Scope

BM25 keyword search across every cached markdown file (`.cache/<namespace>/markdown/*.md`), ranked by SQLite FTS5's built-in `bm25()`. **FTS5 tokenises both the documents and the query; `_tokenize`'s only remaining caller is `_snippet_terms`.** What the query path shares with it is the `_STOPWORDS` constant, not the function. Keep that set deliberately under-aggressive — it is far shorter than a standard English list, because words that carry content here ("all", "no", "not", "very") must stay out of it.

## What a hit returns

`section_index` is the chainable handle, never the bare `section` title: headings repeat within one paper often enough (the measurement is in `papers.py` § Section boundaries) that `get_paper_section(id, title)` dead-ends on "Ambiguous section title". Snippet and section offsets must come from `_textnorm.lower_with_map` (rationale in `.claude/rules/utils.md`), never a raw `str.lower()` — **including on the default `normalize=False` path**. `_section_for_offset` must keep delegating to `papers.section_at_offset` — a local copy of that scan drops the empty-section filter and names a section the reader's own index doesn't have (`.claude/rules/pipeline.md`).

## Identifier inversion

`_filename_to_canonical` inverts `papers.safe_stem`, and **its output is the `canonical_id` an agent chains back into the paper tools** — a stem that doesn't round-trip is a hit that goes nowhere. Two clauses to invert: the `/` → `_` mapping (per-namespace, because a DOI suffix may legitimately contain `_`, so only a slash a *known prefix* introduced is decidable) and the percent-encoding (unconditional `unquote`, which is exact because `safe_stem` writes a literal `%` as `%25`).

Per namespace: arXiv old-style stems (`_ARXIV_OLDSTYLE_STEM_RE`) restore the slash in **every** archive, not just the hyphenated physics ones, and new-style IDs start with a digit and pass through; bioRxiv/ACL use exact-prefix repairs; `manual` restores the registrant slash via `_MANUAL_DOI_STEM_RE`. That last one is not an edge case — `manual.resolve_target` sends every non-arXiv/bioRxiv/ACL DOI to the `manual` namespace, so most stems there are publisher DOIs, not the freeform labels the name suggests. A suffix carrying further slashes still round-trips imperfectly.

## The FTS5 index

**Persistent incremental index — SQLite FTS5** at `.cache/__search_index__/index.db` (WAL, `synchronous=NORMAL`, opened per call — SQLite connections aren't thread-shareable). A `files` table holds `(ns, stem, mtime_ns, size, unindexable)`; two **contentless** (`content=''`, `contentless_delete=1`) FTS5 tables hold the postings. `contentless_delete=1` is what lets a removed paper be DELETEd. Two tables rather than one because diacritic folding is a build-time tokenizer option in FTS5 while `normalize` is a query-time flag here: `fts` uses `remove_diacritics 0`, `fts_norm` uses `remove_diacritics 2`, and the flag selects between them.

## Staleness and self-healing

`_refresh_index()` (under the module-level `_INDEX_LOCK`, since `search` runs in worker threads) walks the corpus via `_scan_markdown`, compares each file's `(mtime_ns, size)` against the recorded row, re-indexes only what changed, and deletes rows whose file is gone. `search(force_refresh=True)` bypasses the staleness signal for the rare same-mtime-same-size edit. `_sweep_legacy_index` deletes the old `index.json` on the way past; `_connect` self-heals a corrupt or non-database file by unlinking it (plus `-wal`/`-shm`) and rebuilding, and `_ensure_schema` rebuilds on a `_SCHEMA_VERSION` mismatch. The reserved `__search_index__` dir has no `markdown/` subdir, so the scan skips it.

## Ranking

FTS5's `bm25()` is negative, most-relevant-first; `search` flips it so the response keeps "higher is better". **Invariant: every returned hit scores strictly above zero** — don't coarsen the rounding, because a term present in every document of a small corpus scores a few parts in a million and a coarser round reports it as `0.0`. Corpus statistics are **global**: `namespace` selects which documents come back, not how they rank, so a paper scores the same in a filtered and an unfiltered search. Equal scores tie-break on `(ns, stem)`, since FTS5 orders by rank alone and would otherwise fall back to insertion order.

## Query tokenisation

The query must be tokenised by FTS5, not `_tokenize`: `_tokenize`'s ASCII-only regex splits "Gutiérrez" into `guti OR rrez` and reduces a wholly non-Latin query to nothing, disagreeing with an index that stored the term fine. So `_query_words` whitespace-splits, `_fts_query` double-quotes each word (an unquoted `NOT`/`OR`/`*`/`-`/`:` parses as an operator or raises), and `search` gates on the MATCH expression being non-empty — never on `_tokenize`. `_query_words` drops stopwords and single characters itself — `unicode61` strips neither, so an unfiltered "the" ORs in a term that matches the whole corpus. The sole surviving `_tokenize` caller is `_snippet_terms`, which unions tokenised with raw-lowercased words because `_extract_snippet`'s word-boundary scan needs punctuation stripped *and* the accented form intact — otherwise the snippet centres on the document head and reports no `section_index`, leaving a real hit unnavigable.

## `unindexable` and the CJK trade

**`unindexable(namespace, refresh=False)` is a contract, not an optimisation** — `tools/search.py::search_cached_papers` calls `search` first, which already refreshed, then reads the flags in the same `to_thread` hop. Making it refresh unconditionally doubles the corpus walk on every tool call.

It records, per file, why a document can never match (`no_indexable_tokens`, `unreadable`) rather than letting it be silently absent. The probe matches what `unicode61` means by "no terms" — no Unicode letter or digit anywhere (`_ALNUM_RE`), so punctuation-only and empty files are caught while a paper in any script is not. Note `unicode61` does not segment CJK: a run delimited by whitespace or punctuation is one token, so CJK papers are indexed and findable by whole runs but not by sub-phrases — a deliberate trade against `trigram`'s index size (the measurement is in `CHANGELOG.md`).
