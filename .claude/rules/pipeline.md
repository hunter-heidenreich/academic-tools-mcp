---
paths:
  - "src/academic_tools_mcp/papers.py"
  - "src/academic_tools_mcp/manual.py"
  - "src/academic_tools_mcp/cache_search.py"
  - "src/academic_tools_mcp/_fast_extract.py"
  - "src/academic_tools_mcp/oa_download.py"
---

# PDF + content pipeline

## papers.py

Converter-agnostic PDF-to-markdown pipeline and section-level access.

### Converter command

- `_build_converter_command()` reads `PDF_CONVERTER` (named backend or custom command template) and `PDF_CONVERTER_VENV` (optional venv to activate) from env. Built-in backends: `mineru` (default), `marker`.
- **Placeholders are substituted shell-quoted** (`shlex.quote`), so templates use **bare** `{input}` / `{output_dir}` (and the fast path's `{python}`). A custom template that wraps them in quotes double-quotes an already-quoted value and breaks the path.
- That quoting is the security boundary: a canonical-derived path can't inject into the `bash -c` command. Belt-and-suspenders, `manual._pdf_filename` restricts canonical→filename to `[A-Za-z0-9._-]` (via `papers._safe_stem`), so metacharacters never reach the filename either.
- `_resolve_convert_timeout()` reads `PDF_CONVERT_TIMEOUT` (default 1800s = 30 min; `none`/`off`/`disabled`/`0`/any value ≤ 0 disables; unset/empty/garbage falls back to the default).

### Full conversion — `convert_pdf()`

- **Global single-conversion lock** (`_global_convert_lock`): at most one PDF→markdown subprocess across the whole server. A second concurrent caller gets `{busy: True, retryable: True, in_progress: {namespace, canonical, elapsed_seconds}, pdf_size_mb}` immediately rather than queueing.
- The already-converted early-return is **not** under that lock, so agents keep reading sections of converted papers while a different one converts.
- Spawned with `start_new_session=True` so a timeout can `os.killpg(SIGKILL)` the whole process tree — the converter, not just the bash wrapper.
- Converter output goes to a fresh private `tempfile.mkdtemp` dir (`_make_extraction_dir`, mode 0700, removed in a `finally`) rather than a predictable `/tmp/pdf-convert-<canonical>` path: no symlink or pre-creation risk, no cross-instance collision, and no `rm -rf` pre-step.
- Markdown lands under `.cache/<namespace>/markdown/`. The cached-markdown early-return re-reads under the per-paper sections lock and treats a file unlinked mid-read (a concurrent `force_refresh` / `download_pdf` cascade, which also holds the lock) as a cache miss rather than raising.

### Fast conversion — `convert_pdf(..., mode="fast")`

A lightweight, **degraded** fallback (`_convert_fast()`): plain text, no tables, equations, figures, or headings.

- Backend from `PDF_FAST_CONVERTER` (`pdftotext` default; `pymupdf` via the `[fast]` extra, routed through `_fast_extract.py`; or a custom template), timeout from `PDF_FAST_CONVERT_TIMEOUT` (default 120s).
- Captures **stdout** as the document, stderr separately — see `_fast_extract.py` for the contract every backend follows.
- Runs **outside** `_global_convert_lock`, so it never serialises and never returns `busy`. It is serialised per-paper via `sections_lock` instead, re-checking the markdown cache before spawning so two concurrent fast calls don't both spawn.
- Writes the **same cache slot** as the full path, so a later `mode="full"` + `force_refresh` upgrades it. Both modes share `_finalize_markdown()` (post-process → write → parse → cache), and the sections cache records `conversion_mode` (`"full"` / `"fast"` / `None` for entries predating the field), which `convert_pdf` echoes.

### Sections and in-paper search

- `parse_sections()` splits by H2 headings with H3 previews (adaptive — detects H1 vs H2 documents). `get_section_content()` retrieves individual sections by index or title substring. Section indices cached under `.cache/<namespace>/sections/`.
- **Per-paper sections lock** (`_section_locks`, keyed by `(namespace, canonical)`) serialises concurrent re-parse attempts on one paper. The dict is an `OrderedDict` capped at `_SECTION_LOCKS_MAX` with FIFO eviction; currently-held locks are skipped on eviction, so mutual exclusion can't be silently dropped out from under a writer.
- `find_in_markdown(markdown, query, *, max_results=20, case_sensitive=False, whole_words=False)` — substring scan (regex with `\b…\b` when `whole_words=True`) returning `[{section_index, section, char_offset, match, snippet}, ...]` in document order, with ~120-char snippet windows centred on each match and newlines collapsed.
- `char_offset` is computed against the same `"\n".join(lines[s:e]).strip()` recipe `get_section_content` uses, so an agent chains straight into `get_paper_section(identifier, section_index, offset=char_offset)` with no further bookkeeping. Wired through the `find_in_paper` MCP tool inside `asyncio.to_thread`, so heavy match counts don't pin the event loop.

## _fast_extract.py

The bundled pymupdf runner behind `PDF_FAST_CONVERTER=pymupdf`, invoked as `python -m academic_tools_mcp._fast_extract <pdf>` by `papers._convert_fast`.

It exists as a module rather than an inline `-c` script so `sys.executable` resolves it against the environment where the optional `[fast]` extra is installed. **The contract every fast backend follows: extracted text to stdout, diagnostics to stderr, non-zero exit on failure.** Keep them separate — `_convert_fast` captures stdout as the document, so anything logged there corrupts the conversion. A missing `pymupdf` import or an extraction error exits non-zero with a clear stderr message, which the caller surfaces as a permanent (non-retryable) error rather than caching an empty file.

## manual.py

Manual PDF/markdown import for local files, plus the two identifier dispatchers.

- **Provider-aware routing for PDF storage** — `resolve_target()` detects identifier type (arXiv ID, bioRxiv DOI, ACL DOI) and stores PDFs/markdown directly in that provider's cache namespace, so native pipeline tools find them with no duplicates. Unrecognised identifiers fall back to the `manual` namespace.
- **Metadata dispatch** — `resolve_metadata_source()` returns `"arxiv" | "biorxiv" | "openalex" | None`. ACL DOIs and generic DOIs route to OpenAlex (ACL has no metadata API); unknown identifiers return `None` so tools can surface a clear error.
- **Atomic writes.** `import_local_pdf` copies the PDF via `cache._atomic_copy` (sibling temp + `os.replace`) and `import_markdown` writes via `cache._atomic_write_text` — a crash mid-write can't leave a torn canonical file the way a direct `shutil.copy2` / `write_text` to the destination could. Markdown is read/written UTF-8 explicitly (cached-hit re-read included) so non-ASCII pre-converted papers survive a non-UTF-8 host locale.
- **`force_refresh`.** Both `import_local_pdf` and `import_markdown` take a keyword-only `force_refresh=False`. Default returns an existing cached file as `cached: True`; `force_refresh=True` (or replacing an existing PDF) rewrites it. When a PDF is (re)written over a prior one, `_invalidate_derived()` drops the cached markdown + section index and the result carries `cascaded_invalidated: ["markdown", "sections"]`, mirroring the `download_pdf` force_refresh cascade in `tools/pipeline.py`. A 0-byte / non-`%PDF-` file at the canonical path (`_looks_like_cached_pdf`) is treated as a miss and overwritten, so an interrupted earlier import can't be served forever.
- Supports `~/` expansion for local paths.
- Module deliberately does **not** download arbitrary URLs — agents fetch non-native PDFs themselves and hand the local file to `import_paper`.
- No API, no auth, no rate limits.

Manual imports intentionally have no BibTeX generation — the manual pipeline has no structured metadata. When the identifier is a DOI, chain into `get_paper_bibtex` (which dispatches to OpenAlex for arbitrary DOIs).

## oa_download.py

The gated open-access path for generic publisher DOIs. `download_pdf` natively handles only arXiv / bioRxiv / ACL, which build a known CDN URL from the identifier; a generic DOI has no such URL, so `download_pdf(doi, allow_oa_url=True)` opts into this module instead.

**This is a trust boundary — treat it as one.** It fetches *only* the open-access URL OpenAlex already surfaces (`best_oa_location.pdf_url` → `primary_location.pdf_url` → `open_access.oa_url`, via `openalex.best_pdf_url`), never a caller-supplied one. That is what keeps the server a metadata-gated fetcher rather than a general scraper. Do not add a parameter that accepts a URL, and do not widen the resolution to a search or a redirect chase.

- **Its own client and cap, not OpenAlex's.** OA URLs hit arbitrary publisher domains rather than one API with a documented budget, so it is provider-shaped in its own right — pooled client, `_request_slot`, single-flight — with a conservative `_MAX_CONCURRENT` and no inter-start gap (every URL is a different host). Borrowing OpenAlex's api-tuned slot would be wrong in both directions.
- **`require_pdf=True`.** The stream is validated as an actual PDF (Content-Type advisory + `%PDF-` magic-byte sniff on the first chunk) so a publisher landing page is rejected before anything is written.
- **The PDF lands in the `manual` namespace**, so `convert_paper` and the `force_refresh` cascade treat it like any imported paper — no duplicate download, no separate pipeline.
- **Deliberately not `cache.cached_lookup`.** The positive artifact is a file on disk, not a JSON record, so the module pairs `manual._looks_like_cached_pdf` (0-byte / pre-header leftovers count as a miss) with a *negative*-only cache, re-checking both inside the single-flight slot the way `cached_lookup` does.
- **Only definitive failures are negative-cached** (24h, entity `downloads`), so a retrying agent doesn't re-resolve OpenAlex and re-fetch the same non-PDF every call. `_is_definitive_failure` deliberately excludes a `retryable` transport error and a `MAX_PDF_BYTES` abort — the latter is a config choice a cap bump fixes, not a fact about the paper, and caching it would strand the caller behind a stale miss.
- **A transient OpenAlex lookup error is surfaced as-is**, without the import_paper suggestion. Telling an agent to go fetch the PDF by hand because a lookup blipped is wrong advice; only a definitive miss gets the escape hatch.

## cache_search.py

BM25 keyword search across every cached markdown file (`.cache/<namespace>/markdown/*.md`), ranked by SQLite FTS5's built-in `bm25()` (k1=1.2, b=0.75 — FTS5's defaults). `_tokenize` no longer builds the index; FTS5 does. It survives for snippet centring and for the stopword list `_query_words` reuses — the classic 50 minus content-bearing terms like "all", "no", "not", "very" that matter in academic prose — and it preserves intra-word hyphens / dots so `self-attention` and `BM25` survive intact.

For each top hit, returns the document title (first H1/H2), a ~200-char snippet centred on the position with the most distinct query terms cooccurring nearby, and the H1/H2 section the snippet falls under — as `section_index` (chainable) alongside the human-readable `section`, plus the hit's `char_offset`. Chain on the **index**: titles are not unique (10.9% of a real 2,493-paper corpus has duplicates), so `get_paper_section(id, title)` dead-ends on "Ambiguous section title" roughly one time in nine. Snippet/section offsets are located via `_textnorm.lower_with_map` (lowercase, optionally NFKD-fold, returning an offset map back to the original text) — required even on the default `normalize=False` path because `str.lower()` is not length-preserving (U+0130 'İ' → 2 chars), so a raw lowercased-string offset would drift the snippet window and section attribution off the real match. Results are ordered by score then `(namespace, stem)` so equal-scoring hits are deterministic across sessions even as the incremental index reorders entries; `top_k<=0` returns `[]`.

Filename → canonical-ID inversion is per-namespace. arXiv inverts old-style IDs in **every** archive via regex (`_ARXIV_OLDSTYLE_STEM_RE`: `archive[.subj]_NNNNNNN` → `archive[.subj]/NNNNNNN`, e.g. `hep-th_9901001` → `hep-th/9901001`, `cs_0501001` → `cs/0501001`, `math.gt_0309136` → `math.gt/0309136`); new-style IDs start with a digit and pass through. bioRxiv/ACL use exact-prefix repairs (`10.1101_X` → `10.1101/X`, `10.18653_v1_X` → `10.18653/v1/X`); manual identifiers pass through unchanged.

**Persistent incremental index — SQLite FTS5** at `.cache/__search_index__/index.db` (WAL, `synchronous=NORMAL`; opened per call, since connections aren't shareable across the `asyncio.to_thread` worker threads `search` runs in, and opening costs microseconds). A `files` table holds `(ns, stem, mtime_ns, size, unindexable)`; two **contentless** (`content=''`, `contentless_delete=1`) FTS5 tables hold the postings. Contentless is what makes the database *smaller* than the JSON it replaced rather than larger — the markdown is already on disk and the top-`k` winners are re-read for snippets anyway, so the text is never stored twice. Two tables rather than one because `normalize` is a query-time flag in this API but diacritic folding is a **build-time** tokenizer option in FTS5: `fts` uses `unicode61 remove_diacritics 0`, `fts_norm` uses `remove_diacritics 2`, and the flag selects between them, preserving the parameter's exact meaning.

`_refresh_index()` (under the module-level `_INDEX_LOCK`, since `search` runs in worker threads) walks the corpus with `os.scandir` — each entry's stat comes from the dirent rather than a second syscall — compares each file's `(mtime_ns, size)` against the recorded row, re-indexes only what changed, and deletes rows whose file is gone. `search(force_refresh=True)` bypasses the staleness signal for the rare same-mtime-same-size edit. `_sweep_legacy_index` deletes the old `index.json` on the way past; `_connect` self-heals a corrupt or non-database file by unlinking it (plus `-wal`/`-shm`) and rebuilding, and `_ensure_schema` rebuilds on a `_SCHEMA_VERSION` mismatch. The reserved `__search_index__` dir has no `markdown/` subdir, so the scan skips it.

**Ranking** is FTS5's built-in `bm25()`, which returns a negative score most-relevant-first; it is flipped so the response keeps "higher is better", and rounded to 6 decimals (3 crushed a degenerate-IDF hit to `0.0` and broke the "every hit scores above zero" invariant). Corpus statistics are **global**: `namespace` selects which documents come back, not how they rank, so a paper scores the same in a filtered and an unfiltered search. Equal scores tie-break on `(ns, stem)`, since FTS5 orders by rank alone and would otherwise fall back to insertion order.

**Query handling is deliberately split from `_tokenize`.** FTS5 tokenises the documents, so the query must be tokenised by FTS5 too: `_query_words` whitespace-splits and `_fts_query` double-quotes each word (an unquoted `NOT`/`OR`/`*`/`-`/`:` would be parsed as an operator, or raise). Running the query through `_tokenize`'s ASCII-only regex instead made the two sides disagree — "Gutiérrez" became `guti OR rrez` and matched nothing, and a wholly non-Latin query was gated out entirely before FTS5 ever saw it, even though the index held the term. What `_query_words` *does* keep from `_tokenize` is the stopword and single-character filter: the index stores raw markdown under `unicode61`, which strips neither, so an unfiltered "the" would OR in a term matching the whole corpus. `search` gates on the MATCH expression being non-empty (an empty one is an FTS5 syntax error) and returns `[]` before touching the index. `_snippet_terms` unions `_tokenize`'s output with the folded/lowercased raw words, because `_extract_snippet`'s word-boundary scan needs punctuation stripped (`_tokenize`'s job) *and* needs the accented or non-Latin form intact (which `_tokenize` destroys) — otherwise a hit the index found came back centred on the document head with no `section_index`, unnavigable.

**`unindexable`** records, per file, why a document can never match (`no_indexable_tokens`, `unreadable`) rather than letting it be silently absent; `search_cached_papers` surfaces it as `unindexable_count` / `unindexable` / `unindexable_note` only when non-empty. The probe matches what `unicode61` means by "no terms" — no Unicode letter or digit anywhere (`_ALNUM_RE`), so punctuation-only and empty files are caught while a paper in any script is not. Note `unicode61` does not segment CJK: a run delimited by whitespace or punctuation is one token, so CJK papers are indexed and findable by whole runs but not by sub-phrases — a deliberate trade (`trigram` would have cost 842 MB against 165 MB). Wrapped in `asyncio.to_thread` at the tool layer so a large-corpus search doesn't pin the event loop. Pure keyword recall — won't surface "scaled dot-product attention" for a "self-attention" query — natural follow-up if recall ever bites is sentence-transformer embedding rerank.
