---
paths:
  - "src/academic_tools_mcp/papers.py"
  - "src/academic_tools_mcp/manual.py"
  - "src/academic_tools_mcp/_fast_extract.py"
---

# PDF + content pipeline

## papers.py

Converter-agnostic PDF-to-markdown pipeline and section-level access.

### Converter command

- **Placeholders are substituted `shlex.quote`d, so templates carry bare `{input}` / `{output_dir}` / `{python}`** — a template that quotes them itself double-quotes an already-quoted value and breaks the path. That quoting is the trust boundary: a canonical-derived path cannot inject into the `bash -c` command.
- **A malformed template surfaces as `{error, retryable: False}`, never a raised exception.** `str.format` raises `KeyError` / `IndexError` / `ValueError`, none of them `OSError`, so `_format_template` narrows them to `ConverterTemplateError` — and **both** builders must stay inside their caller's `try`, on the fast path too.
- **Every derived path routes through `papers.safe_stem()`** — PDF, markdown, sections key, extraction-dir prefix — so they can never disagree about which file belongs to which paper. It percent-encodes rather than collapsing, because collapsing would merge `a b` and `a_b` onto one file; `/` → `_` is the one deliberate exception, unreachable for real identifiers.
- **`safe_stem` is not idempotent** (`a%20b` → `a%2520b`), so anything re-deriving a stem must gate on `_MIGRATED_STEM_RE` first. `migrate_legacy_stems()`, run once at startup, is the idempotent wrapper.
- `_resolve_convert_timeout()` reads `PDF_CONVERT_TIMEOUT` (same disable vocabulary as `MAX_PDF_BYTES`; see its docstring).

### Full conversion — `convert_pdf()`

- **Global single-conversion lock** (`_global_convert_lock`): at most one PDF→markdown subprocess across the whole server. A second concurrent caller gets a structured `busy` error immediately rather than queueing — a caller that wanted to wait could have waited itself.
- The already-converted early-return is **not** under that lock, so agents keep reading sections of converted papers while a different one converts.
- Spawned with `start_new_session=True` so a timeout can `os.killpg(SIGKILL)` the whole process tree — the converter, not just the bash wrapper.
- Converter output goes to a fresh `mkdtemp` dir (`_make_extraction_dir`, removed in a `finally` on every exit path) — never a predictable `/tmp/pdf-convert-<canonical>` path, which invites symlink/pre-creation attacks and cross-instance collision.
- **Cancellation kills the tree, then re-raises.** Both modes catch `asyncio.CancelledError`, `await _kill_process_group(proc)`, and re-raise — never swallow it, never turn it into an error payload. The `finally` removes the extraction dir but does not signal the child, so a converter would otherwise keep pinning CPU/GPU with its output dir deleted underneath it.
- **Never merge stderr into stdout** (`2>&1`) on either path: full mode appends stderr *last* so a chatty converter can't push the real error out of the truncated tail, and fast mode captures stdout as the document.
- Markdown lands under `.cache/<namespace>/markdown/`. The cached-markdown early-return re-reads under the per-paper sections lock and treats a file unlinked in the gap as a cache miss rather than raising. **Every markdown unlinker holds that lock** — `convert_pdf`'s own `force_refresh` branch, and `tools/pipeline`'s `download_pdf` and `import_paper` cascades. A new one must take it too.

### Fast conversion — `convert_pdf(..., mode="fast")`

A lightweight, **degraded** fallback (`_convert_fast()`): plain text, no tables, equations, figures, or headings.

- Backend from `PDF_FAST_CONVERTER` (`pdftotext` default; `pymupdf` via the `[fast]` extra, routed through `_fast_extract.py`), timeout from `PDF_FAST_CONVERT_TIMEOUT`.
- **Contract for every fast backend: document text to stdout, diagnostics to stderr, non-zero exit on failure.** `_convert_fast` captures stdout as the document, so anything logged there corrupts it. `_fast_extract.py` is the bundled pymupdf runner — a module rather than an inline `python -c` so `{python}` resolves it against the env where the optional `[fast]` extra is installed.
- Runs **outside** `_global_convert_lock`, so it never queues behind a heavy conversion and can never return `busy`. Serialisation is per-paper via `sections_lock`, re-checking the markdown cache before spawning so two concurrent fast calls don't both spawn.
- Writes the **same cache slot** as the full path, so a later `mode="full"` + `force_refresh` upgrades it. Both modes share `_finalize_markdown()`, which post-processes converter output and then delegates to `store_markdown_and_index()`.

### The sections-cache entry

Three sites write one: `store_markdown_and_index()` (both conversion modes via `_finalize_markdown()`, and `manual.import_markdown` directly), `_reparse_sections_locked()`, and `_convert_fast()`'s cached-markdown branch — which also preserves an existing `"full"` tag, so re-reading a full conversion through `mode="fast"` never relabels it degraded.

**Invariant: every entry carries all four of `sections`, `sections_detected`, `markdown_checksum`, `conversion_mode`.** A new writer goes through `store_markdown_and_index`. A missing `sections_detected` costs a re-parse (the entry is treated as stale, and a file read plus a regex pass is cheaper than a guess); a *wrong* one is reported to the agent as truth — the exact reading `sections_note` exists to prevent.

`conversion_mode` is provenance — `_reparse_sections_locked` must preserve a recorded mode, since a re-parse produces no new evidence about what converted the file. The agent-facing value vocabulary is in `.claude/rules/server.md`.

**Post-processing is the caller's, not the writer's.** `_finalize_markdown` rstrips lines and rewrites `![cap](path)` → `![cap]()` because a converter's image paths point into an extraction dir deleted on return. `import_markdown` passes its markdown through verbatim — that file is the operator's own text and its links may resolve. (The verbatim-import side is pinned; the converter-side rstrip and image rewrite are unguarded.)

### Sections and in-paper search

- Section indices are cached under `.cache/<namespace>/sections/`.
- **`section_boundaries()` and `section_at_offset()` are the only implementations of the heading scan.** `parse_sections`, `find_in_markdown`, `get_section_content` and `cache_search.search` all route through them. A private copy that drops the empty-section filter names a section the reader's index doesn't have; one that returns a title instead of an index dead-ends on "Ambiguous section title".
- **Title lookup folds diacritics only as a fallback.** `_match_section_title` runs the exact lowercased substring pass first and the `_textnorm.fold` pass only when it returns nothing, so folding can widen a miss into a hit (`"Resume"` → `"Résumé"`) but can never turn a query that already resolves into an "Ambiguous section title" error. Fold both passes unconditionally and a paper carrying both spellings stops resolving either.
- **Per-paper sections lock** (`_section_locks`, keyed by `(namespace, canonical)`) serialises concurrent re-parse attempts on one paper. The dict is an `OrderedDict` capped at `_SECTION_LOCKS_MAX`, evicting least-recently-used first; currently-held locks are skipped on eviction, so mutual exclusion can't be silently dropped out from under a writer.
- `find_in_markdown` returns `(hits, truncated)` — `truncated` is what lets `find_in_paper` say "more matches exist" instead of silently capping at `max_results`. With `normalize=True` it matches folded text but slices `char_offset` / `match` / `snippet` from the original via `_textnorm.fold_with_map` + `_textnorm.original_span`, so a chained `get_paper_section` still lands on the match.
- `char_offset` is computed against the same `"\n".join(lines[s:e]).strip()` recipe `get_section_content` uses, so an agent chains straight into `get_paper_section(identifier, section_index, offset=char_offset)` with no further bookkeeping.

## manual.py

Manual PDF/markdown import for local files, plus the two identifier dispatchers.

- **Provider-aware routing for PDF storage** — `resolve_target()` detects identifier type (arXiv ID, bioRxiv DOI, ACL DOI) and stores PDFs/markdown directly in that provider's cache namespace, so native pipeline tools find them with no duplicates. Unrecognised identifiers fall back to the `manual` namespace.
  - **The arXiv shape test is single-homed in `_is_arxiv_identifier`**, which both `resolve_target` and `resolve_metadata_source` call — storage and metadata must not disagree about which ids are arXiv's.
  - **Invariant: an id's routing does not depend on how it was typed.** `_ARXIV_OLD_RE` matches the *lowercased* normalized id and its archive class carries `.` (`math.GT/0309136`, `cond-mat.stat-mech/0501001`) — old-style ids are dotted and vary in case upstream — and `arxiv._normalize_arxiv_id` strips the `arXiv:` prefix before the shape test ever runs (`.claude/rules/providers.md` § arxiv.py). An id the test rejects still gets a canonical key identical to arXiv's, so it lands in `manual` and the *same paper* caches, downloads and converts twice. `manual.migrate_misrouted_arxiv()` moves the files a narrower test left behind; it runs at startup beside `papers.migrate_legacy_stems`, is idempotent and best-effort, and moves rather than renames — so unlike that sweep it must create the destination namespace dir, which `cache.cache_dir` does not. Its predicate inverts `safe_stem` (restore the slash, then `unquote`, both with and without the repair since the stem alone doesn't say) and then **asks the router** rather than matching a shape of its own: the criterion is exactly "`resolve_target` would file this elsewhere now", so the sweep cannot drift from the routing it exists to catch up with.
- **Metadata dispatch** — `resolve_metadata_source()` returns `"arxiv" | "biorxiv" | "openalex" | None`. ACL DOIs and generic DOIs route to OpenAlex (ACL has no metadata API); unknown identifiers return `None` so tools can surface a clear error.
- **Atomic writes only** — `import_local_pdf` → `cache._atomic_copy`, `import_markdown` → `papers.store_markdown_and_index` → `cache._atomic_write_text`; never `shutil.copy2` / `write_text` straight to the destination (`.claude/rules/cache.md`). Every markdown read and write is explicit UTF-8, the cached-hit re-read included.
- **Both import functions stay synchronous; the async boundary is the tool layer.** `tools/pipeline.import_paper` wraps each in `asyncio.to_thread` — an arbitrarily large copy or parse run inline stalls every concurrent tool call — and holds `papers.sections_lock` across **both** branches, since each replaces the markdown + section-index pair `convert_pdf` and the `force_refresh` cascade mutate under it.
- **`force_refresh`.** Both `import_local_pdf` and `import_markdown` take a keyword-only `force_refresh=False`. Default returns an existing cached file as `cached: True`; `force_refresh=True` (or replacing an existing PDF) rewrites it. The cascade condition is `existed or force_refresh`, so any forced import — including a first-ever one — runs `_invalidate_derived()` and returns `cascaded_invalidated: ["markdown", "sections"]`, mirroring the `download_pdf` cascade in `tools/pipeline.py`. The cached-hit branch returns `_pdf_download.cached_hit(dest)` decorated with `identifier` / `namespace`; the freshness rule and the check-then-`stat` race it absorbs live in `.claude/rules/pdf-download.md` and must not be re-implemented here.
- Module deliberately does **not** download arbitrary URLs — agents fetch non-native PDFs themselves and hand the local file to `import_paper`.

Manual imports intentionally have no BibTeX generation — the manual pipeline has no structured metadata. When the identifier is a DOI, chain into `get_paper_bibtex` (which dispatches to OpenAlex for arbitrary DOIs).
