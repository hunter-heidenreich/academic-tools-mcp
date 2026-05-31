---
paths:
  - "src/academic_tools_mcp/server.py"
  - "src/academic_tools_mcp/_app.py"
  - "src/academic_tools_mcp/tools/*.py"
  - "src/academic_tools_mcp/bibtex.py"
---

# server, tools, and BibTeX

## Layout: `_app.py` + `tools/` + thin `server.py`

The 21 MCP tools are split across four `tools/` modules, registered against one
shared FastMCP instance:

- **`_app.py`** — the shared core: the `mcp = FastMCP(...)` instance, the
  `_lifespan` context manager (closes pooled clients via `_clients.aclose_all()`
  on shutdown), the full `Annotated` parameter-type vocabulary (`DOI`,
  `AUTHOR_ID`, `PAPER_ID`, the `*_FORCE_REFRESH` flags, …), and the helpers used
  by more than one tool group (`_enrich_error`, `_arxiv_id_from_entry`,
  `_fetch_crossref_work`). It imports infra / providers / content **only** —
  never `tools` — so tool modules import from it without a cycle.
- **`tools/paper.py`** — `get_paper_metadata` / `get_papers_metadata` /
  `get_paper_authors` / `get_paper_abstract` / `get_paper_bibtex` / `get_author`
  plus the `_format_*_metadata` helpers.
- **`tools/pipeline.py`** — `download_pdf` / `convert_paper` /
  `get_paper_sections` / `get_paper_section` / `import_paper` plus
  `_download_pdf_by_provider`, `_strip_internal_paths`.
- **`tools/graph.py`** — `get_paper_references[_count]` / `get_paper_citations[_count]`
  plus the Crossref/OpenCitations page formatters.
- **`tools/search.py`** — `search_arxiv` / `search_crossref_by_title` /
  `find_in_paper` / `search_cached_papers` / `search_wikipedia` /
  `get_wikipedia_summary`.
- **`server.py`** — thin entry point: imports `mcp` from `_app`, imports the four
  tool modules (which registers their `@mcp.tool` decorators), re-exports the tool
  callables + providers + a few helpers under `server.<name>` for callers/tests,
  and registers the optional operator-only `get_server_stats` debug tool. The
  console entry point stays `academic_tools_mcp.server:mcp.run`.

Each tool fetches the full cached object then returns only the relevant slice.
`_fetch_crossref_work` is monkeypatched by tests at `_app`, so its call sites in
`paper.py`/`graph.py` go through the module (`_app._fetch_crossref_work(...)`).

Per-source metadata formatting is factored into helpers (`_format_arxiv_metadata` / `_format_biorxiv_metadata` / `_format_openalex_metadata` / `_format_openalex_via_biorxiv`) so `get_paper_metadata` and `get_papers_metadata` produce identical per-paper payloads without duplicating the field mapping.

### Unified paper family

`get_paper_metadata` / `get_paper_authors` / `get_paper_abstract` / `get_paper_bibtex` accept any `PAPER_ID` and dispatch via `manual._resolve_metadata_source()` to arXiv, bioRxiv, or OpenAlex. Every successful response carries:

- `_source` — which provider served it (`"arxiv"` / `"biorxiv"` / `"openalex"`)
- `_canonical_id` — the provider's normalized form of the input (version-stripped lowercased arXiv ID, lowercased bare DOI, etc.)

There is no lowest-common-denominator normalisation — agents branch on `_source` for provider-specific fields. All four also accept `force_refresh: bool = False` — drops both positive and negative cache entries via `cache.invalidate(...)` and re-fetches; useful for stale citation counts, a bioRxiv preprint that just got published, or retrying a previously-404'd identifier.

`get_paper_metadata` additionally accepts `follow_published: bool = False` — when `True` and a bioRxiv paper has a `published_doi`, auto-chains to `openalex.get_work(published_doi, force_refresh=force_refresh)` and returns the journal record with `_source: "openalex_via_biorxiv"`, `_canonical_id` set to the journal DOI, a `preprint_doi` field, and `followed_published: True`. Falls back to the preprint record if OpenAlex misses (paper too new to index) — that fallback now carries `followed_published: False` so the lag is explicit rather than silent. The field is set by `_format_biorxiv_metadata`'s keyword-only `followed_published` param (and unconditionally in `_format_openalex_via_biorxiv`); it stays **absent** when no chain was attempted (`follow_published=False`, or no `published_doi`) and on the batch path, so the default response shape is unchanged.

### Batch metadata: `get_papers_metadata(identifiers)`

For 30+ identifiers at once (typical reference-graph enrichment after `get_paper_references`). Groups identifiers by source, fans out arXiv / bioRxiv as concurrent singletons (now efficient with `_MAX_CONCURRENT > 1`), and routes OpenAlex DOIs through `openalex.get_works_batch` — one HTTP call per 50 DOIs via `/works?filter=doi:...|...`.

Returns `{count, papers: [...]}`. Each paper entry mirrors the corresponding `get_paper_metadata` payload exactly, plus an `_input` field carrying the original (un-normalised) identifier so an agent can correlate input → output. Order matches the input list. Per-paper failures appear as `{_input, error, suggestion?}` entries; one failure does not affect the others.

Cap is 100 identifiers per call; for larger sets the agent pages. Does NOT support `follow_published` — chain bioRxiv-to-journal explicitly via per-paper `get_paper_metadata` calls.

### get_paper_authors pagination

Paginated (`page`, `page_size` default 25, cap 25) to bound response size on large-collaboration papers (HEP, biology consortia) that can carry thousands of authors. Every response includes `author_count` (global total), `has_more`, and the current page. Since the upstream paper response is cached per canonical identifier, paging is pure in-memory slicing — zero extra API cost.

The institution roll-up (`page_institutions` / `page_institution_count`) appears on every branch — populated on OpenAlex (derived from the current page only so the cap holds; agents needing a global list dedupe across pages), empty on arxiv/biorxiv. The shape stays symmetric so paginating agents don't have to feature-detect.

The OpenAlex-shaped `get_paper_authors` response includes `openalex_id` per author so agents can chain into `get_author`. arXiv and bioRxiv responses don't carry this.

### Unified PDF pipeline

`download_pdf` → `convert_paper` → `get_paper_sections` → `get_paper_section`. Auto-detects provider via `manual._resolve_target()` and routes to the correct cache namespace — works for arXiv IDs, ACL DOIs, bioRxiv DOIs, and manually imported papers.

`force_refresh: bool = False` on the first three with stage-specific semantics:

- `download_pdf` — re-downloads and **atomically replaces** the cached PDF. The existing file is kept if the re-download fails (404, transport error, `MAX_PDF_BYTES` abort), so a flaky network can't leave the agent with no copy: `stream_to_file` writes the new bytes to a sibling temp and only `os.replace`s into the canonical path on success. **Cascades**: when the re-download succeeds (`cached=False` in the result), `_download_pdf_by_provider` also unlinks the cached markdown and invalidates the section index for that paper, and tags the response with `cascaded_invalidated: ["markdown", "sections"]`. The next `convert_paper` therefore picks up the new bytes — no need for the agent to remember to also `convert_paper(force_refresh=True)`. Cache hits (cached=True) do NOT cascade because the existing markdown is still consistent; a *failed* refresh (result carries `error`) doesn't cascade either, so the preserved PDF and its markdown stay consistent.
- `convert_paper` — drops both cached markdown and section index so the converter subprocess re-runs (use after replacing source PDF or upgrading converter).
- `get_paper_sections` — drops just the section index so next read re-parses markdown.

PDF downloads stream chunks (64 KiB) to a sibling temp file via `_pdf_download.stream_to_file` and atomic-rename into place, so peak memory stays at one chunk and a crash mid-download cannot leave a half-written canonical PDF. The `MAX_PDF_BYTES` env var (default 200 MB) caps total bytes; oversize streams abort mid-download with `{error, retryable: False, max_bytes}` rather than filling the disk.

`get_paper_section` reads the markdown file directly (no derived cache) so it has no `force_refresh`. Paginated by character offset: `offset` (default 0) + `max_chars` (default 16000, hard cap 200000). Every response carries `total_chars`, `chars_returned`, `has_more`, `next_offset` so agents read long sections by re-calling with `offset=next_offset` rather than asking for an unbounded slice. Carries `anthropic/maxResultSizeChars=200000` meta so Claude Code doesn't persist large results to disk.

`convert_paper(..., mode=...)` — `CONVERT_MODE` is `Literal["full", "fast"]`, default `"full"`. `"full"` is the heavy MinerU/Marker path (high quality, slow, serialised under the global lock). `"fast"` runs a lightweight stdout-capturing text extractor (`PDF_FAST_CONVERTER`, default `pdftotext`; `pymupdf` via the `[fast]` extra) *outside* the lock — seconds, never `busy`, but **degraded** (plain text, no tables/equations/figures/headings). Both write the same cache slot, so a later `mode="full"` + `force_refresh` upgrades a fast conversion. Every successful response echoes `conversion_mode` (may be `null` for papers converted before the field existed); the tool passes `mode` straight through to `papers.convert_pdf`.

### convert_paper error shapes

- `{error, retryable: False}` for permanent failures (missing PDF, converter crash). Fast mode tags these `conversion_mode: "fast"` and the spawn-failure suggestion points at installing poppler-utils / the `[fast]` extra.
- `{error, retryable: False, timed_out: True, timeout_seconds, pdf_size_mb}` on `PDF_CONVERT_TIMEOUT` (full) or `PDF_FAST_CONVERT_TIMEOUT` (fast). On a **full-mode** timeout the tool's suggestion points the agent at retrying with `mode="fast"`.
- `{error, retryable: True, busy: True, in_progress: {...}}` when another conversion is already in flight — **full mode only**; the busy suggestion now also offers `mode="fast"` (which skips the lock). Fast mode never produces this.

### Pipeline tool boundary

The PDF pipeline tools (`download_pdf`, `convert_paper`, `import_paper`) deliberately strip cache filesystem paths from their responses at the MCP boundary so agents drive the pipeline by identifier through the tools rather than reading files directly.

### import_paper

Single tool that auto-detects `.pdf` vs `.md`/`.markdown` by extension. PDFs are validated by their `%PDF-` magic bytes (rejects mis-extension files before they reach the converter); markdown is read as UTF-8 with a clean error on decode failure. The MCP-layer response slims the markdown branch to `section_count` only — the agent calls `get_paper_sections` if it wants the full index.

`force_refresh: bool = False` (`IMPORT_FORCE_REFRESH` in `_app.py`) re-imports a file even when one is already cached under the identifier, replacing the cached copy and — for a PDF — cascading the markdown + section-index invalidation (response gains `cascaded_invalidated: ["markdown", "sections"]`). It's the supported way to swap in a corrected PDF or a better manual conversion; default `False` returns the existing cached copy as `cached: True` untouched. Writes are atomic (temp + `os.replace`) at the `manual.py` layer, and a 0-byte / non-`%PDF-` leftover at the canonical PDF path is treated as a miss rather than served as cached.

### Reference / citation graph tools

- `get_paper_references_count` — surveys both Crossref and OpenCitations in parallel, returns per-source counts.
- `get_paper_references(doi, source, page, page_size)` — defaults `source="auto"`, fires both providers in parallel via `asyncio.gather`, picks whichever has more references (tie → Crossref for richer per-entry metadata), falls back to surviving source if one errors. Both errors → response carries both error messages. Explicit `source="crossref"` or `source="opencitations"` skips the survey (important for paginating page=2..N).
- `get_paper_citations_count` / `get_paper_citations` — incoming citations (OpenCitations only today). `get_paper_citations` accepts `source: Literal["auto", "opencitations"] = "auto"` so a future second source can ship without a breaking change.

All four take `force_refresh: FORCE_REFRESH = False` (the shared `_app.FORCE_REFRESH` type) — drops the cached entry and re-fetches, since the citation graph grows continuously. The reference tools thread it into **both** sources (`_app._fetch_crossref_work(doi, force_refresh=...)` + `opencitations.get_references(..., force_refresh=...)`); `get_paper_references_count` routes Crossref through `_app._fetch_crossref_work` (not `crossref.get_work` directly) for monkeypatch-seam parity with `get_paper_references`. Pass `force_refresh` on the first page only — omit it when paginating so page 2..N reuse the warmed cache.

Crossref provides structured reference metadata (author, title, year, journal, DOI) when publishers deposit it; quality varies. OpenCitations aggregates from Crossref/PubMed/DataCite/OpenAIRE/JaLC and returns DOI-to-DOI links with cross-referenced IDs (OMID, OpenAlex, PMID) and self-citation flags — broader coverage, no bibliographic metadata.

### Search tools

- `search_arxiv` — `{total_results, results: [...]}`; each hit `{arxiv_id, title, first_author, author_count, published_year}`. Full-author lists balloon on HEP/biology papers, so search drops everything beyond triage. Each entry opportunistically cached — follow-up `get_paper_metadata(arxiv_id)` is free.
- `search_crossref_by_title` — DOI discovery by bibliographic query. Useful when you only have a title or arXiv ID and need the published DOI (e.g. ACL Anthology DOI for an arXiv paper). Year filtering is optional but Crossref publication dates may differ from arXiv preprint dates. De facto search for bioRxiv (no title search endpoint upstream — Crossref indexes all bioRxiv DOIs). Hits return `{doi, title, first_author, author_count, year}` (parallel to `search_arxiv`). Each hit also opportunistically warms `crossref/works`.
- `search_cached_papers` — BM25 over locally-converted markdown across all namespaces (or filtered to one). Use case: "I read this paper a few weeks ago, what was its identifier?" or "which of my imported PDFs talked about X?" — neither answerable by upstream search APIs. Returns `{namespace, canonical_id, score, title, snippet, section, char_count}`; chain `get_paper_section(canonical_id, section)`. Pure keyword match — won't bridge synonyms, doesn't see un-converted PDFs.
- `find_in_paper` — substring (or whole-word) search inside one converted paper. Returns `{query, paper_identifier, result_count, results: [{section_index, section, char_offset, match, snippet}, ...]}`. `char_offset` aligns with `get_paper_section`'s stripped section text so an agent can chain straight to the surrounding context. Pairs with `search_cached_papers`: that one tells you *which* paper mentions X, this one tells you *where in the paper*. Errors out cleanly if the paper isn't converted yet.

## bibtex.py

Generates BibTeX entries for three provider shapes:

- `generate_bibtex()` — raw OpenAlex work object, maps `type` → BibTeX entry type via `_TYPE_MAP`.
- `generate_arxiv_bibtex()` — `@misc` (preprint) or `@article` (published) with `eprint`/`archiveprefix`/`primaryclass` fields.
- `generate_biorxiv_bibtex()` — `@article` when `published_doi` set, else `@misc` with preprint DOI, server name, `howpublished` URL.

All three share helpers for surname particles (`van`, `de la`, `von`, etc.) in citation keys and author formatting (`_format_names` + `_format_one_name`, parameterised by a `name_of` accessor so OpenAlex's nested `author.display_name` and arXiv/bioRxiv's flat `name` reuse one code path).

Output-correctness contracts (so generated entries always compile):

- **Citation keys are ASCII `[a-z0-9]`.** `_key_token` transliterates the common non-decomposable characters (`ø ł ß đ æ œ þ ð ı`, via `_TRANSLIT`) that `_textnorm.fold` can't strip, then folds diacritics, lowercases, and drops everything else (apostrophes, hyphens, periods, spaces, surviving non-ASCII). `_extract_last_name` and `_first_key_word` both route through it.
- **Escaping treats field text as literal.** `_escape_bibtex` neutralises the full LaTeX special set — strips literal `{ }` first (so they can't unbalance the field), then escapes `\` → `\textbackslash{}`, `& % $ # _`, and `~ ^` → `\textascii*{}`. It does **not** preserve braces for case-protection. DOIs use the narrower `_escape_doi` (`& % # _` only — no backslash mangling of the DOI string).
- **Organisational authors are brace-wrapped.** `_format_one_name` detects consortium/collaboration names (`_ORG_RE`) and emits `{The ATLAS Collaboration}` so BibTeX treats them atomically instead of splitting off a fake surname.
- **Cross-entry key disambiguation is out of scope.** These functions are stateless (one paper per call) and cannot see sibling entries, so two papers sharing author+year+title-word collide on one key. A caller concatenating many entries into a single `.bib` must deduplicate keys itself.
