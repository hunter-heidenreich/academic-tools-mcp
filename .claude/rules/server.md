---
paths:
  - "src/academic_tools_mcp/server.py"
  - "src/academic_tools_mcp/_app.py"
  - "src/academic_tools_mcp/tools/*.py"
  - "src/academic_tools_mcp/bibtex.py"
---

# server, tools, and BibTeX

**Per-tool parameters and response keys live in the `@mcp.tool` docstrings**, which are also what agents receive as the tool description — `grep '@mcp.tool' tools/` for the roster. This file covers only what no single docstring can: the wiring between modules, and the invariants that span tools.

## Layout: `_app.py` + `tools/` + thin `server.py`

The MCP tools are split across four `tools/` modules by job — `paper`, `pipeline`, `graph`, `search` — all registered against one shared FastMCP instance.

- **`_app.py` imports infra / providers / content only — never `tools`.** That one-way edge is what lets every tool module import from it without a cycle. It is the home for the `mcp = FastMCP(...)` instance, the `_lifespan` context manager (closes pooled clients via `_clients.aclose_all()` on shutdown), the `Annotated` parameter-type vocabulary, and any helper used by more than one tool group. A helper needed by two tool modules moves here rather than being imported across them.
- **`server.py` imports the four tool modules for their side effect** — that import is what runs the `@mcp.tool` decorators and registers the tools. It also re-exports the tool callables, providers, and helpers under `server.<name>` so existing callers and tests keep one import path, and registers the operator-only `get_server_stats` debug tool.

Each tool fetches the full cached object then returns only the relevant slice. Tool modules call providers directly (`crossref.get_work(...)`, `openalex.get_work(...)`) and tests monkeypatch the provider. **Don't add a passthrough wrapper in `_app` to create a patch point** — patch the provider instead; a wrapper that exists only as a test seam is dead weight the moment nothing patches it.

Per-source metadata formatting is factored into helpers (`_format_arxiv_metadata` / `_format_biorxiv_metadata` / `_format_openalex_metadata` / `_format_openalex_via_biorxiv`) so `get_paper_metadata` and `get_papers_metadata` produce identical per-paper payloads without duplicating the field mapping.

## Cross-tool response contracts

These hold across several tools, so changing one tool alone breaks the set.

- **`_source` and `_canonical_id` on every paper-family response.** `get_paper_metadata` / `_authors` / `_abstract` / `_bibtex` dispatch through `manual.resolve_metadata_source()`, and there is deliberately **no lowest-common-denominator normalisation** — agents branch on `_source` for provider-specific fields, so the four must agree on the tag.
- **Search-list shape.** Every search tool reports `result_count` (= `len(results)`, what this call returned). `search_arxiv` and `search_crossref_by_title` additionally carry `total_results` — the **upstream** match count, so an agent can tell more exist beyond the page. `search_crossref_by_title` must surface Crossref's `message.total-results`, **not** the returned page length. `search_wikipedia` / `search_cached_papers` report only `result_count`; they have no upstream-total concept.
- **`page_institutions` / `page_institution_count` appear on every `get_paper_authors` branch** — populated for OpenAlex (from the current page only, so the page cap holds), empty for arxiv/biorxiv. The shape stays symmetric so paginating agents never feature-detect. Author paging is in-memory slicing of the cached paper, so it costs no extra API calls.
- **The PDF pipeline tools strip cache filesystem paths** from their responses at the MCP boundary, so agents drive the pipeline by identifier through the tools rather than reading files directly.

## `follow_published` and the batch path

`get_paper_metadata(..., follow_published=True)` chains a bioRxiv paper with a `published_doi` to `openalex.get_work(published_doi)` and returns the journal record as `_source: "openalex_via_biorxiv"`, `_canonical_id` set to the journal DOI, plus a `preprint_doi` field.

The `followed_published` flag is the part to keep consistent: `_format_biorxiv_metadata` sets it via a keyword-only param, `_format_openalex_via_biorxiv` sets it unconditionally, and it stays **absent** when no chain was attempted (`follow_published=False`, or no `published_doi`) and on the batch path — so the default response shape is unchanged. A fallback to the preprint record (OpenAlex hasn't indexed the journal version yet) carries `followed_published: False`, making the lag explicit rather than silent.

`get_papers_metadata` groups identifiers by source, fans arXiv / bioRxiv out as concurrent singletons, and routes OpenAlex DOIs through `openalex.get_works_batch` — one HTTP call per 50 DOIs. Each entry mirrors the `get_paper_metadata` payload exactly plus an `_input` field carrying the original identifier, so an agent can correlate input to output. It does **not** support `follow_published`; chain bioRxiv-to-journal per paper.

## `force_refresh` cascade semantics

Stage-specific, and the cascade rules are the subtle part:

- `download_pdf` — re-downloads and atomically replaces the cached PDF; the existing file survives a failed re-download (`stream_to_file` only `os.replace`s on success). **On success only** (`cached=False`), `_download_pdf_by_provider` also unlinks the cached markdown, invalidates the section index, and tags the response `cascaded_invalidated: ["markdown", "sections"]` — so the next `convert_paper` picks up the new bytes without the agent remembering to refresh it too. A cache hit does **not** cascade (existing markdown is still consistent); a *failed* refresh does **not** cascade either, keeping the preserved PDF and its markdown consistent.
- `convert_paper` — drops both cached markdown and section index so the converter subprocess re-runs.
- `get_paper_sections` — drops just the section index so the next read re-parses markdown.
- `import_paper` — re-imports over an existing cached copy and, for a PDF, cascades the same markdown + section-index invalidation. The MCP-layer response slims the markdown branch to `section_count`; the agent calls `get_paper_sections` for the full index.

`get_paper_section` reads the markdown file directly with no derived cache, so it has no `force_refresh`. Its read and `get_paper_sections`'s re-parse both run off the event loop (`asyncio.to_thread`) with explicit UTF-8, and both degrade to the `{error}` "not converted" contract when a concurrent cascade unlinks the file between the existence check and the read, rather than raising. `get_paper_section` carries `anthropic/maxResultSizeChars=200000` meta so Claude Code doesn't persist large results to disk.

Streaming, chunk size, and the `MAX_PDF_BYTES` cap belong to `_pdf_download` — see `.claude/rules/pdf-download.md`.

## Conversion modes and error shapes

`CONVERT_MODE` stays `Literal["full", "fast"]`: `"imported"` is provenance you can *receive* (a pre-converted file handed to `import_paper`), not a backend you can request. `null` appears only for papers converted before the field existed. Both modes write the same cache slot, so a later `mode="full"` + `force_refresh` upgrades a fast conversion; the tool passes `mode` straight through to `papers.convert_pdf`.

Three error shapes, distinguished by what the suggestion should tell the agent to do next:

- Permanent failure (missing PDF, converter crash) → `{error, retryable: False}`. In fast mode the spawn-failure suggestion points at installing poppler-utils or the `[fast]` extra.
- Timeout → adds `timed_out: True, timeout_seconds, pdf_size_mb`. On a **full-mode** timeout the suggestion points at retrying with `mode="fast"`.
- Another conversion in flight → `{error, retryable: True, busy: True, in_progress: {...}}`, **full mode only** — fast mode runs outside the global lock and can never produce it. The busy suggestion also offers `mode="fast"`.

## Reference / citation graph tools

**`source="auto"` is biased toward Crossref, and not by a simple max.** `_CROSSREF_HYSTERESIS = 1.2`: OpenCitations wins only when `oc_count > cr_count * 1.2`. Crossref entries carry structured author/title/year/journal metadata where OpenCitations returns bare DOI-to-DOI links, so a near-tie on raw count must not flip `auto` to the metadata-poor source for the sake of a row or two. Do not "simplify" this to `oc_count > cr_count`.

**A single-source failure is surfaced, not swallowed.** Both providers fire in parallel via `asyncio.gather`; an errored source counts as `-1` so the survivor wins automatically. When exactly one failed, the response gains `partial_failure: {source, ...}` (built by `_source_error`) so a short or empty result isn't read as a confident "no references." Both failing → both error messages.

`get_paper_citations` deliberately has **no** `source` parameter: OpenCitations is the only provider of incoming citations, and a knob with one value is noise in the agent's context. Add one when a second source actually exists.

All four graph tools take `force_refresh: FORCE_REFRESH = False` and thread it into **both** sources; `get_paper_references_count` and `get_paper_references` each call `crossref.get_work` directly, so a test patching the provider covers both. Explicit `source=` skips the survey — which is what paginating past page 1 should do, alongside dropping `force_refresh` so pages 2..N reuse the warmed cache.

## Search tools

- **Opportunistic cache warming.** Each `search_arxiv` hit warms `arxiv/papers` and each `search_crossref_by_title` hit warms `crossref/works`, so a follow-up `get_paper_metadata(id)` is a free cache hit. `search_arxiv` returns triage hits only — full author lists balloon on HEP/biology papers.
- **Year extraction is single-homed.** `_crossref_year` walks `issued` → `published-print` → `published-online` → `published` → `posted` in the order held by `_app._CROSSREF_DATE_KEYS`, and guards malformed `date-parts` (`null` / `[]` / `[[null]]`) so a bad record degrades to `year: None` instead of crashing the page. `posted` is what covers preprints, including every bioRxiv DOI. `first_author` falls back to a consortium `name` field when given/family are absent.
- **`search_cached_papers` surfaces `unindexable_*` only when non-empty**, and `unindexable_note` is built per-reason from `cache_search.unindexable()`'s `reason` field — it must not assert one cause for all of them. The docstring states the CJK limitation because that one produces a *silent* empty result with no `unindexable` entry to explain it: those papers are indexed. Engine internals live in `.claude/rules/search.md`.
- Chain a `search_cached_papers` hit on `section_index`, never the title — titles are not unique.

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
