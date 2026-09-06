---
paths:
  - "src/academic_tools_mcp/papers/*.py"
  - "src/academic_tools_mcp/_stems.py"
  - "src/academic_tools_mcp/manual.py"
  - "src/academic_tools_mcp/_fast_extract.py"
---

# PDF + content pipeline

## Layout

Four modules, and the split is load-bearing, not cosmetic:

- **`_stems.py`** — artifact naming. `safe_stem`, the three path builders, the two checksum helpers, the startup sweep. It sits **below** the pipeline and depends only on `cache` + stdlib, so a provider that needs to name a PDF does not import a converter. Three providers and `manual` import it; none of them imports `papers`. Putting `safe_stem` back in `papers` re-closes that edge.
- **`papers/sections.py`** — pure markdown structure. No filesystem, no cache, no asyncio.
- **`papers/index.py`** — the on-disk section index and the per-paper lock.
- **`papers/convert.py`** — converter subprocesses and the global conversion gate.

`papers/__init__.py` re-exports the public surface. **Patch the owning submodule, never the facade** — it re-exports by value, so `monkeypatch.setattr(papers, "_section_locks", ...)` rebinds an alias nothing reads.

## _stems.py

### Naming

- **Every derived path routes through `safe_stem()`** — PDF, markdown, sections key, extraction-dir prefix — so they can never disagree about which file belongs to which paper. It percent-encodes rather than collapsing, because collapsing would merge `a b` and `a_b` onto one file; `/` → `_` is the one deliberate exception, unreachable for real identifiers.
- **`safe_stem` is not idempotent** (`a%20b` → `a%2520b`), so anything re-deriving a stem must gate on `_MIGRATED_STEM_RE` first. `migrate_legacy_stems()`, run once at startup, is the idempotent wrapper. **Invariant: `_MIGRATED_STEM_RE` is exactly `safe_stem`'s output alphabet** — a character the writer emits but the gate rejects makes startup re-encode a correct name (`notes~draft%202024` → `notes~draft%25202024`) and orphan the file it just renamed. `~` is why the class is not simply `_SAFE_STEM_KEEP`: `quote` leaves the RFC 3986 unreserved set alone.
- **`pdf_path(namespace, canonical)` is the sibling of `markdown_path`, and every provider delegates to it.** Each keeps only its own canonicalization step. A local `safe_stem(x) + ".pdf"` is a fifth copy of a rule that has one home.
- **The sweep renames `.pdf` and `.md` only.** `atomic._new_temp` names an in-flight write `<dst.name>.<rand>.tmp`, whose stem still carries the destination's legacy characters; renaming it makes the writer's `os.replace` raise.

### Checksums

**A writer checksums the string it parsed (`checksum_text`), never the file it just wrote.** The two are separated by a window another writer fits through, and an index stamped with the *other* document's checksum matches disk forever — so `_reparse_sections_locked` accepts it and never re-parses. This is what makes the entry correct by construction rather than by lock discipline: a losing writer's entry simply mismatches and self-heals. It rests on `atomic.write_text` pinning `newline=""`, so the bytes on disk are exactly the UTF-8 encoding of the payload.

## papers/convert.py

### Converter command

- **Placeholders are substituted `shlex.quote`d, so templates carry bare `{input}` / `{output_dir}` / `{python}`** — a template that quotes them itself double-quotes an already-quoted value and breaks the path. That quoting is the trust boundary: a canonical-derived path cannot inject into the `bash -c` command.
- **A malformed template surfaces as `{error, retryable: False}`, never a raised exception.** `str.format` raises `KeyError` / `IndexError` / `ValueError`, none of them `OSError`, so `_format_template` narrows them to `ConverterTemplateError` — and **both** builders must stay inside their caller's `try`, on the fast path too.
- `_resolve_convert_timeout()` reads `PDF_CONVERT_TIMEOUT` through `config.number`, which owns the disable vocabulary. It passes `on_nonpositive="disable"` — unlike `MAX_PDF_BYTES`, a non-positive timeout is a second way to say "off" rather than a typo.

### `_run_command` — the one subprocess driver

Both modes spawn through it, so a change to the cancellation or timeout discipline cannot land in one and miss the other. It returns `_Completed | _SpawnFailed | _TimedOut`; **what a mode says about an outcome stays with that mode**, because the messages differ and so does how the two streams are combined. Don't push the error dicts into the driver to "finish" the DRY — that is where the two genuinely disagree.

### Full conversion — `convert_pdf()`

- **Global single-conversion lock** (`_global_convert_lock`): at most one PDF→markdown subprocess across the whole server. A second concurrent caller gets a structured `busy` error immediately rather than queueing — a caller that wanted to wait could have waited itself.
- The already-converted early-return is **not** under that lock, so agents keep reading sections of converted papers while a different one converts.
- Spawned with `start_new_session=True` so a timeout can `os.killpg(SIGKILL)` the whole process tree — the converter, not just the bash wrapper.
- Converter output goes to a fresh `mkdtemp` dir (`_make_extraction_dir`, removed in a `finally` on every exit path) — never a predictable `/tmp/pdf-convert-<canonical>` path, which invites symlink/pre-creation attacks and cross-instance collision.
- **Cancellation kills the tree, then re-raises** (in `_run_command`) — never swallowed, never turned into an error payload. Neither caller's `finally` signals the child, so a converter would otherwise keep pinning CPU/GPU with its output dir deleted underneath it.
- **Never merge stderr into stdout** (`2>&1`): full mode appends stderr *last* so a chatty converter can't push the real error out of the truncated tail, and fast mode captures stdout as the document. That is why the driver hands both streams back rather than combining them.
- **The full-conversion write path holds only the global lock, not `sections_lock`.** That is why `store_markdown_and_index` may not re-read the file to checksum it (see `_stems.py` § Checksums above); it is the one markdown writer outside the per-paper lock discipline.
- Markdown lands under `.cache/<namespace>/markdown/`. The cached-markdown early-return re-reads under the per-paper sections lock and treats a file unlinked in the gap as a cache miss rather than raising. **`drop_derived()` is the only markdown unlinker, and every caller holds that lock** — `convert_pdf`'s own `force_refresh` branch, and `tools/pipeline`'s `download_pdf` and `import_paper` cascades. It drops the markdown and the section index together: dropping one leaves a reader matching a checksum against bytes that no longer exist. Best-effort, so a file that can't be unlinked still loses its index and re-parses. Don't unlink markdown anywhere else.

### Fast conversion — `convert_pdf(..., mode="fast")`

A lightweight, **degraded** fallback (`_convert_fast()`): plain text, no tables, equations, figures, or headings.

- Backend from `PDF_FAST_CONVERTER` (`pdftotext` default; `pymupdf` via the `[fast]` extra, routed through `_fast_extract.py`), timeout from `PDF_FAST_CONVERT_TIMEOUT`.
- **Contract for every fast backend: document text to stdout, diagnostics to stderr, non-zero exit on failure.** `_convert_fast` captures stdout as the document, so anything logged there corrupts it. `_fast_extract.py` is the bundled pymupdf runner — a module rather than an inline `python -c` so `{python}` resolves it against the env where the optional `[fast]` extra is installed.
- Runs **outside** `_global_convert_lock`, so it never queues behind a heavy conversion and can never return `busy`. Serialisation is per-paper via `sections_lock`, re-checking the markdown cache before spawning so two concurrent fast calls don't both spawn.
- Writes the **same cache slot** as the full path, so a later `mode="full"` + `force_refresh` upgrades it. Both modes share `_finalize_markdown()`, which post-processes converter output and then delegates to `store_markdown_and_index()`.

### The sections-cache entry

Two sites write one: `store_markdown_and_index()` (both conversion modes via `_finalize_markdown()`, and `manual.import_markdown` directly) and `_reparse_sections_locked()`. **`_convert_fast`'s cached-markdown branch delegates to the latter rather than assembling a third.** That is not tidiness: a local `recorded_mode or "fast"` stamps an entry predating the field — `null`, meaning nobody knows — as degraded, and writes that claim to disk permanently, while `convert_pdf`'s cached branch answers `null` for the identical state.

**Invariant: every entry carries all four of `sections`, `sections_detected`, `markdown_checksum`, `conversion_mode`.** A new writer goes through `store_markdown_and_index`. A missing `sections_detected` costs a re-parse (the entry is treated as stale, and a file read plus a regex pass is cheaper than a guess); a *wrong* one is reported to the agent as truth — the exact reading `sections_note` exists to prevent.

`conversion_mode` is provenance — `_reparse_sections_locked` must preserve a recorded mode, since a re-parse produces no new evidence about what converted the file. The agent-facing value vocabulary is in `.claude/rules/server.md`.

**Post-processing is the caller's, not the writer's.** `_finalize_markdown` rstrips lines and rewrites `![cap](path)` → `![cap]()` because a converter's image paths point into an extraction dir deleted on return. `import_markdown` passes its markdown through verbatim — that file is the operator's own text and its links may resolve. (The verbatim-import side is pinned; the converter-side rstrip and image rewrite are unguarded.)

### Sections and in-paper search

- Section indices are cached under `.cache/<namespace>/sections/`.
- **`_scan()` is the only heading scan.** `section_boundaries`, `has_detected_sections`, `parse_sections`, `parse_sections_and_detect`, `find_in_markdown`, `get_section_content` and `cache_search.search` all reach it. A private copy that drops the empty-section filter names a section the reader's index doesn't have; one that returns a title instead of an index dead-ends on "Ambiguous section title".
- **`SECTION_LEVELS` is the policy, and `first_section_heading()` is how another module asks about it.** `cache_search._extract_title` delegates rather than re-spelling `level <= 2`; a hit that names a heading the section index does not open on is agent-visible. It is not `section_boundaries(md)[0].title`, which is `"Preamble"` for the pre-heading span.
- **`Section.body(lines)` is the one body recipe.** `find_in_markdown` and `get_section_content` slice through it, which is what makes a hit's `char_offset` an offset into the text the reader returns. Two hand-spelled `"\n".join(lines[s:e]).strip()` cannot be relied on to stay equal.
- **Title lookup folds diacritics only as a fallback.** `_match_section_title` runs the exact lowercased substring pass first and the `_textnorm.fold` pass only when it returns nothing, so folding can widen a miss into a hit (`"Resume"` → `"Résumé"`) but can never turn a query that already resolves into an "Ambiguous section title" error. Fold both passes unconditionally and a paper carrying both spellings stops resolving either.
- **Per-paper sections lock** (`_section_locks`, keyed by `(namespace, canonical)`) serialises concurrent re-parse attempts on one paper. The dict is an `OrderedDict` capped at `_SECTION_LOCKS_MAX`, evicting least-recently-used first; currently-held locks are skipped on eviction, so mutual exclusion can't be silently dropped out from under a writer. **Invariant: the map is the sole owner of a lock across an await.** Every caller writes `async with sections_lock(...)` as one expression, and `Lock.acquire` on an uncontended lock returns without yielding — that is the whole reason eviction cannot race a caller. Bind the lock to a variable, await something, *then* enter it, and the key can be evicted and recreated, handing two callers two different `Lock` objects.
- `find_in_markdown` returns `(hits, truncated)` — `truncated` is what lets `find_in_paper` say "more matches exist" instead of silently capping at `max_results`. With `normalize=True` it matches folded text but slices `char_offset` / `match` / `snippet` from the original via `_textnorm.fold_with_map` + `_textnorm.original_span`, so a chained `get_paper_section` still lands on the match.
- `char_offset` is computed against the same `"\n".join(lines[s:e]).strip()` recipe `get_section_content` uses, so an agent chains straight into `get_paper_section(identifier, section_index, offset=char_offset)` with no further bookkeeping.

## manual.py

Manual PDF/markdown import for local files, plus the two identifier dispatchers.

- **Provider-aware routing for PDF storage** — `resolve_target()` walks `_ROUTES`, an ordered tuple of `(claims, namespace, canonical_key, pdf_path)` per provider, and stores PDFs/markdown directly in the claiming provider's cache namespace, so native pipeline tools find them with no duplicates. Unrecognised identifiers fall back to the `manual` namespace. **The order is load-bearing**: an arXiv id is not a DOI, an ACL DOI is one, and the generic-DOI fallback is last. It returns a `Target` TypedDict (`namespace`, `canonical`, `pdf_path: Path`), so the ~10 call sites that subscript it are type-checked and `pdf_path` stays a `Path` rather than decaying to `Any`.
  - **The arXiv shape test is `arxiv.is_arxiv_id`**, which both `resolve_target` and `resolve_metadata_source` call — storage and metadata must not disagree about which ids are arXiv's. It lives in the provider beside `biorxiv.is_biorxiv_doi` and `acl_anthology.is_acl_doi`, so this module routes on three predicates of the same shape and owns none of them.
  - **Invariant: an id's routing does not depend on how it was typed.** `arxiv.is_arxiv_id` tests the *canonical* id, so case, the `arXiv:` prefix and an abs/pdf URL are all resolved before the shape is looked at; the old-style archive class carries `.` (`math.GT/0309136`, `cond-mat.stat-mech/0501001`) — those ids are dotted and vary in case upstream — (`.claude/rules/providers.md` § arxiv.py). An id the test rejects still gets a canonical key identical to arXiv's, so it lands in `manual` and the *same paper* caches, downloads and converts twice. `manual.migrate_misrouted_arxiv()` re-files what a narrower test left behind; it runs at startup beside `papers.migrate_legacy_stems`, is idempotent and best-effort, and moves across namespaces — so unlike that sweep it must create the destination namespace dir, which `cache.cache_dir` does not. **It renames as it moves.** `_misrouted_arxiv_id` inverts `safe_stem` (restore the slash, then `unquote`, both with and without the repair since the stem alone doesn't say), **asks the router** rather than matching a shape of its own — the criterion is exactly "`resolve_target` would file this elsewhere now", so the sweep cannot drift from the routing it exists to catch up with — and returns the recovered *key*, which is what names the destination. Reusing the source filename holds only where the legacy `manual` key equalled the arXiv one: that key is `_doi.canonical`, which strips `doi:` and not `arXiv:`, so `arxiv%3A2301.00001` would land in a namespace that only ever looks for `2301.00001`. A moved markdown's `manual` section index is dropped (the index is namespaced, so nothing would read it again) and re-parses under `arxiv`. **Deliberately not `cache_search._filename_to_canonical`**, despite being the same shape of operation: that one repairs the slash with each namespace's own *anchored* grammar, which is right for a stem that namespace wrote and wrong here — these stems carry an `arXiv:` prefix the legacy `manual` key kept, and `_ARXIV_OLDSTYLE_STEM_RE` (`^archive_number$`) can never match one. Sharing the grammar makes the sweep miss the prefixed spellings it exists for.
- **Metadata dispatch** — `resolve_metadata_source()` returns the `MetadataSource` literal `"arxiv" | "biorxiv" | "openalex"`, or `None` so tools can surface a clear error. **It is derived from `resolve_target`**, not a second pass over the shapes: the namespace maps through `_METADATA_SOURCE_BY_NAMESPACE`, and only the `manual` namespace re-tests the key (a publisher DOI is OpenAlex's, a label is nobody's). Two parallel if-chains here is the bug this shape prevents — ACL is the one namespace that changes hands, its PDFs from the Anthology and its metadata from OpenAlex.
- **The identifier a response echoes is the canonical cache key**, not the caller's spelling — `target["canonical"]`, so `arXiv:2301.00001v2` comes back as `2301.00001v2` and a DOI comes back folded. It is the key the file is filed under, so an agent can route on it. A blank identifier (`""`, whitespace, a bare `doi:`) is rejected by `_identifier_error`: it keys the empty string, which `safe_stem` maps to an empty stem, so the paper caches as `.pdf` / `.md` and every later blank import is served the first one as `cached`.
- **The force_refresh cascade is `papers.drop_derived`**, not a local unlink — see `papers.py` above. `import_local_pdf` calls it whenever it lands a PDF over an existing one.
- **Atomic writes only** — `import_local_pdf` → `atomic.copy`, `import_markdown` → `papers.store_markdown_and_index` → `atomic.write_text`; never `shutil.copy2` / `write_text` straight to the destination (`.claude/rules/cache.md`). Every markdown read and write is explicit UTF-8, the cached-hit re-read included.
- **Both import functions stay synchronous; the async boundary is the tool layer.** `tools/pipeline.import_paper` wraps each in `asyncio.to_thread` — an arbitrarily large copy or parse run inline stalls every concurrent tool call — and holds `papers.sections_lock` across **both** branches, since each replaces the markdown + section-index pair `convert_pdf` and the `force_refresh` cascade mutate under it.
- **`force_refresh`.** Both `import_local_pdf` and `import_markdown` take a keyword-only `force_refresh=False`. Default returns an existing cached file as `cached: True`; `force_refresh=True` (or replacing an existing PDF) rewrites it. The cascade condition is `existed or force_refresh`, so any forced import — including a first-ever one — runs `papers.drop_derived()` and returns `cascaded_invalidated: ["markdown", "sections"]`, mirroring the `download_pdf` cascade in `tools/pipeline.py`. The cached-hit branch returns `_pdf_download.cached_hit(dest)` decorated with `identifier` / `namespace`; the freshness rule and the check-then-`stat` race it absorbs live in `.claude/rules/pdf-download.md` and must not be re-implemented here.
- Module deliberately does **not** download arbitrary URLs — agents fetch non-native PDFs themselves and hand the local file to `import_paper`.

Manual imports intentionally have no BibTeX generation — the manual pipeline has no structured metadata. When the identifier is a DOI, chain into `get_paper_bibtex` (which dispatches to OpenAlex for arbitrary DOIs).
