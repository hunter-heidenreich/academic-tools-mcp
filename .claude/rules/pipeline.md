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

- `_build_converter_command()` reads `PDF_CONVERTER` (named backend or custom command template) and `PDF_CONVERTER_VENV` (optional venv to activate) from env. Built-in backends: `mineru` (default), `marker`.
- **Placeholders are substituted shell-quoted** (`shlex.quote`), so templates use **bare** `{input}` / `{output_dir}` (and the fast path's `{python}`). A custom template that wraps them in quotes double-quotes an already-quoted value and breaks the path.
- That quoting is the security boundary: a canonical-derived path can't inject into the `bash -c` command. Belt-and-suspenders, `papers.safe_stem()` — the single sanitizer for the PDF, markdown and sections-key paths, so the three cannot disagree about which file is which paper — percent-encodes anything outside `[A-Za-z0-9.-]` (`/` → `_`), so metacharacters never reach a filename. The encoding is injective on purpose: collapsing to `_` would map `a b` and `a_b` onto one file. It is therefore **not idempotent** (`a%20b` → `a%2520b`); `migrate_legacy_stems()`, run once at startup, is the idempotent migration and gates on `_MIGRATED_STEM_RE` first.
- `_resolve_convert_timeout()` reads `PDF_CONVERT_TIMEOUT` (same disable vocabulary as `MAX_PDF_BYTES`; see its docstring).

### Full conversion — `convert_pdf()`

- **Global single-conversion lock** (`_global_convert_lock`): at most one PDF→markdown subprocess across the whole server. A second concurrent caller gets a structured `busy` error immediately rather than queueing — a caller that wanted to wait could have waited itself.
- The already-converted early-return is **not** under that lock, so agents keep reading sections of converted papers while a different one converts.
- Spawned with `start_new_session=True` so a timeout can `os.killpg(SIGKILL)` the whole process tree — the converter, not just the bash wrapper.
- Converter output goes to a fresh `mkdtemp` dir (`_make_extraction_dir`, removed in a `finally` on every exit path) — never a predictable `/tmp/pdf-convert-<canonical>` path, which invites symlink/pre-creation attacks and cross-instance collision.
- Markdown lands under `.cache/<namespace>/markdown/`. The cached-markdown early-return re-reads under the per-paper sections lock and treats a file unlinked mid-read (a concurrent `force_refresh` / `download_pdf` cascade, which also holds the lock) as a cache miss rather than raising.

### Fast conversion — `convert_pdf(..., mode="fast")`

A lightweight, **degraded** fallback (`_convert_fast()`): plain text, no tables, equations, figures, or headings.

- Backend from `PDF_FAST_CONVERTER` (`pdftotext` default; `pymupdf` via the `[fast]` extra, routed through `_fast_extract.py`), timeout from `PDF_FAST_CONVERT_TIMEOUT`.
- Captures **stdout** as the document, stderr separately — see `_fast_extract.py` for the contract every backend follows.
- Runs **outside** `_global_convert_lock`, so it never serialises and never returns `busy`. It is serialised per-paper via `sections_lock` instead, re-checking the markdown cache before spawning so two concurrent fast calls don't both spawn.
- Writes the **same cache slot** as the full path, so a later `mode="full"` + `force_refresh` upgrades it. Both modes share `_finalize_markdown()`, which post-processes converter output and then delegates to `store_markdown_and_index()`.

### The sections-cache entry

Three sites write one: `store_markdown_and_index()` (both conversion modes via `_finalize_markdown()`, and `manual.import_markdown` directly), `_reparse_sections_locked()`, and `_convert_fast()`'s cached-markdown branch — which also preserves an existing `"full"` tag, so re-reading a full conversion through `mode="fast"` never relabels it degraded.

**Invariant: every entry carries all four of `sections`, `sections_detected`, `markdown_checksum`, `conversion_mode`.** A new writer goes through `store_markdown_and_index`. A missing `sections_detected` costs a re-parse (the entry is treated as stale, and a file read plus a regex pass is cheaper than a guess); a *wrong* one is reported to the agent as truth — the exact reading `sections_note` exists to prevent. Guarded by `tests/test_manual.py::TestMarkdownImportSectionsIndex`.

`conversion_mode` is provenance — `_reparse_sections_locked` must preserve a recorded mode, since a re-parse produces no new evidence about what converted the file. The agent-facing value vocabulary is in `.claude/rules/server.md`.

**Post-processing is the caller's, not the writer's.** `_finalize_markdown` rstrips lines and rewrites `![cap](path)` → `![cap]()` because a converter's image paths point into an extraction dir deleted on return. `import_markdown` passes its markdown through verbatim — that file is the operator's own text and its links may resolve.

### Sections and in-paper search

- `parse_sections()` splits at H1 **and** H2 (converters disagree on which level is the document title), collects H3 as previews, ignores H4+, and drops empty-bodied sections. `get_section_content()` retrieves individual sections by index or title substring. Section indices cached under `.cache/<namespace>/sections/`.
- `section_boundaries()` is the single home for that scan, and `cache_search._section_for_offset` is one of its cross-module readers — a fourth dialect drifts the two indexes apart.
- **Per-paper sections lock** (`_section_locks`, keyed by `(namespace, canonical)`) serialises concurrent re-parse attempts on one paper. The dict is an `OrderedDict` capped at `_SECTION_LOCKS_MAX`, evicting least-recently-used first; currently-held locks are skipped on eviction, so mutual exclusion can't be silently dropped out from under a writer.
- `find_in_markdown` returns `(hits, truncated)` — `truncated` is what lets `find_in_paper` say "more matches exist" instead of silently capping at `max_results`. With `normalize=True` it matches folded text but slices `char_offset` / `match` / `snippet` from the original via `_textnorm.fold_with_map`, so a chained `get_paper_section` still lands on the match.
- `char_offset` is computed against the same `"\n".join(lines[s:e]).strip()` recipe `get_section_content` uses, so an agent chains straight into `get_paper_section(identifier, section_index, offset=char_offset)` with no further bookkeeping. Wired through the `find_in_paper` MCP tool inside `asyncio.to_thread`, so heavy match counts don't pin the event loop.

## _fast_extract.py

The bundled pymupdf runner behind `PDF_FAST_CONVERTER=pymupdf`, invoked as `python -m academic_tools_mcp._fast_extract <pdf>` by `papers._convert_fast`.

A module, not an inline `-c` script, so `{python}` resolves it against the env where the optional `[fast]` extra is installed. **Contract for every fast backend: text to stdout, diagnostics to stderr, non-zero exit on failure** — `_convert_fast` captures stdout as the document, so anything logged there corrupts the conversion.

## manual.py

Manual PDF/markdown import for local files, plus the two identifier dispatchers.

- **Provider-aware routing for PDF storage** — `resolve_target()` detects identifier type (arXiv ID, bioRxiv DOI, ACL DOI) and stores PDFs/markdown directly in that provider's cache namespace, so native pipeline tools find them with no duplicates. Unrecognised identifiers fall back to the `manual` namespace.
- **Metadata dispatch** — `resolve_metadata_source()` returns `"arxiv" | "biorxiv" | "openalex" | None`. ACL DOIs and generic DOIs route to OpenAlex (ACL has no metadata API); unknown identifiers return `None` so tools can surface a clear error.
- **Atomic writes.** `import_local_pdf` copies the PDF via `cache._atomic_copy` (sibling temp + `os.replace`) and `import_markdown` writes through `papers.store_markdown_and_index`, which uses `cache._atomic_write_text` — a crash mid-write can't leave a torn canonical file the way a direct `shutil.copy2` / `write_text` to the destination could. Markdown is read/written UTF-8 explicitly (cached-hit re-read included) so non-ASCII pre-converted papers survive a non-UTF-8 host locale.
- **Both import functions are synchronous, and the tool layer keeps them off the event loop.** `import_local_pdf` copies an arbitrarily large file and `import_markdown` parses a whole document; run inline from `tools/pipeline.import_paper` either stalls every concurrent tool call for its duration. The tool wraps them in `asyncio.to_thread` — the async boundary is at the tool layer, not here. `import_paper` also holds `papers.sections_lock` across the markdown branch, since `import_markdown` replaces the same markdown + section-index pair that `convert_pdf` and the `force_refresh` cascade mutate under that lock.
- **`force_refresh`.** Both `import_local_pdf` and `import_markdown` take a keyword-only `force_refresh=False`. Default returns an existing cached file as `cached: True`; `force_refresh=True` (or replacing an existing PDF) rewrites it. When a PDF is (re)written over a prior one, `_invalidate_derived()` drops the cached markdown + section index and the result carries `cascaded_invalidated: ["markdown", "sections"]`, mirroring the `download_pdf` force_refresh cascade in `tools/pipeline.py`. A 0-byte / non-`%PDF-` file at the canonical path (`_looks_like_cached_pdf`) is treated as a miss and overwritten, so an interrupted earlier import can't be served forever.
- Module deliberately does **not** download arbitrary URLs — agents fetch non-native PDFs themselves and hand the local file to `import_paper`.

Manual imports intentionally have no BibTeX generation — the manual pipeline has no structured metadata. When the identifier is a DOI, chain into `get_paper_bibtex` (which dispatches to OpenAlex for arbitrary DOIs).
