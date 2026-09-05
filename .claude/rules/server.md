---
paths:
  - "src/academic_tools_mcp/server.py"
  - "src/academic_tools_mcp/_app.py"
  - "src/academic_tools_mcp/tools/*.py"
  - "src/academic_tools_mcp/bibtex.py"
---

# server, tools, and BibTeX

**Per-tool parameters and response keys live in the `@mcp.tool` docstrings**, which are also what agents receive as the tool description — `grep -rn '@mcp.tool' src/academic_tools_mcp/tools/` for the roster. This file covers only what no single docstring can: the wiring between modules, and the invariants that span tools.

## Layout: `_app.py` + `tools/` + thin `server.py`

The MCP tools are split across four `tools/` modules by job — `paper`, `pipeline`, `graph`, `search` — all registered against one shared FastMCP instance. Both module docstrings state the layering; the rule they don't state is that **a helper needed by two tool modules moves into `_app.py`** rather than being imported across them, and `_app.py` must never import `tools` — that one-way edge is what prevents the cycle.

Tool modules call providers directly (`crossref.get_work(...)`, `openalex.get_work(...)`) and tests monkeypatch the provider. **Don't add a passthrough wrapper in `_app` to create a patch point** — patch the provider instead; a wrapper that exists only as a test seam is dead weight the moment nothing patches it.

`_format_metadata_by_source` is the shared success path for both `get_paper_metadata` and the `get_papers_metadata` closures, so the per-source field mapping is written once. The special cases sit beside it (`_format_openalex_via_biorxiv`, `_format_crossref_metadata`) and are reachable from the single-paper tool only. `_format_openalex_authors` follows the same factoring for `get_paper_authors`.

## Cross-tool response contracts

These hold across several tools, so changing one tool alone breaks the set.

- **`_source` and `_canonical_id` on every paper-family response.** `get_paper_metadata` / `_authors` / `_abstract` / `_bibtex` dispatch through `manual.resolve_metadata_source()`, and there is deliberately **no lowest-common-denominator normalisation** — agents branch on `_source` for provider-specific fields, so the four must agree on the tag.
- **Search-list shape.** Every search tool reports `result_count` (= `len(results)`, what this call returned). `search_arxiv` and `search_crossref_by_title` additionally carry `total_results`, which must be the provider's own upstream count (`opensearch:totalResults`, `message.total-results`), never `len(results)` — that is what tells an agent more exist beyond the page. `search_wikipedia` / `search_cached_papers` / `find_in_paper` report only `result_count`; no upstream total exists for them.
- **`page_institutions` / `page_institution_count` appear on every `get_paper_authors` branch** — populated for OpenAlex (from the current page only, so the page cap holds), empty for arxiv/biorxiv. The shape stays symmetric so paginating agents never feature-detect. Author paging is in-memory slicing of the cached paper, so it costs no extra API calls.
- **The PDF pipeline tools strip cache filesystem paths** from their responses at the MCP boundary, so agents drive the pipeline by identifier through the tools rather than reading files directly.

## `follow_published` and the batch path

`get_paper_metadata(..., follow_published=True)` chains a bioRxiv paper with a `published_doi` to `openalex.get_work(published_doi)` and returns the journal record as `_source: "openalex_via_biorxiv"`, `_canonical_id` set to the journal DOI, plus a `preprint_doi` field.

The `followed_published` flag is the part to keep consistent: `_format_biorxiv_metadata` sets it via a keyword-only param, `_format_openalex_via_biorxiv` sets it unconditionally, and it stays **absent** when no chain was attempted (`follow_published=False`, or no `published_doi`) and on the batch path — so the default response shape is unchanged. A fallback to the preprint record (OpenAlex hasn't indexed the journal version yet) carries `followed_published: False`, making the lag explicit rather than silent; a *transient* chain failure additionally carries `published_lookup_retryable: True`, distinguishing "not indexed" from "lookup blipped".

`get_papers_metadata` groups identifiers by source, fans arXiv / bioRxiv out as concurrent singletons, and routes OpenAlex DOIs through `openalex.get_works_batch` — one HTTP call per 50 DOIs. Each entry mirrors the `get_paper_metadata` payload exactly plus an `_input` field carrying the original identifier, so an agent can correlate input to output. It does **not** support `follow_published`; chain bioRxiv-to-journal per paper.

## `force_refresh` cascade semantics

Stage-specific, and the cascade rules are the subtle part:

- `download_pdf` — re-downloads and atomically replaces the cached PDF. **On success only** (`cached=False`), `_download_pdf_by_provider` also unlinks the cached markdown, invalidates the section index, and tags the response `cascaded_invalidated: ["markdown", "sections"]` — so the next `convert_paper` picks up the new bytes without the agent remembering to refresh it too. A cache hit does **not** cascade (existing markdown is still consistent); a *failed* refresh does **not** cascade either, keeping the preserved PDF and its markdown consistent.
- `convert_paper` — drops both cached markdown and section index so the converter subprocess re-runs.
- `get_paper_sections` — drops just the section index so the next read re-parses markdown.
- `import_paper` — same PDF cascade via `manual._invalidate_derived`; the MCP layer additionally slims the markdown branch to `section_count`, so the agent calls `get_paper_sections` for the full index.

`get_paper_section` reads the markdown file directly with no derived cache, so it has no `force_refresh`. **Every markdown read is off the event loop (`asyncio.to_thread`), explicit UTF-8, and degrades to the shared "not converted" error rather than raising** when a concurrent cascade unlinks the file — `get_paper_section`, `find_in_paper`, and `papers._reparse_sections_locked` behind `get_paper_sections`. `get_paper_section` also carries an `anthropic/maxResultSizeChars` meta pinned to `_SECTION_HARNESS_CAP`, the same constant `SECTION_MAX_CHARS` is capped at.

Streaming, the size cap, and the download protocol belong to `_pdf_download` — see `.claude/rules/pdf-download.md`.

## Conversion modes and error shapes

`CONVERT_MODE` stays `Literal["full", "fast"]`: `"imported"` is provenance you can *receive* (a pre-converted file handed to `import_paper`), not a backend you can request. `null` appears only for papers converted before the field existed. Both modes write the same cache slot, so a later `mode="full"` + `force_refresh` upgrades a fast conversion; the tool passes `mode` straight through to `papers.convert_pdf`.

Error shapes, distinguished by what the suggestion should tell the agent to do next:

- A missing or unusable PDF short-circuits before `papers.convert_pdf` into `_app.pdf_not_cached_error` — `{error, suggestion}` with **no `retryable` key**.
- Converter crash / empty output → `{error, retryable: False, pdf_size_mb}`. In fast mode the spawn-failure suggestion points at poppler-utils or the `[fast]` extra.
- Timeout → adds `timed_out: True, timeout_seconds, pdf_size_mb`. On a **full-mode** timeout the suggestion points at retrying with `mode="fast"`.
- Another conversion in flight → `{error, retryable: True, busy: True, in_progress: {...}}`, **full mode only** — fast mode runs outside the global lock and can never produce it. The busy suggestion also offers `mode="fast"`.

## Reference / citation graph tools

**`auto` is biased toward Crossref by `_CROSSREF_HYSTERESIS`, not a plain max.** Crossref entries carry structured author/title/year/journal metadata where OpenCitations returns bare DOI-to-DOI links, so it must win by a margin, not by a row or two. Do not "simplify" this to `oc_count > cr_count`.

**`source="auto"` resolves on page 1 only** — `page > 1` with `auto` returns an error telling the agent to pin the `_source` from page 1. Re-surveying mid-walk could pick a different provider and silently shift `total` and the slice offsets. Pages 2..N should also drop `force_refresh` so they reuse the warmed cache.

**A single-source failure is surfaced, not swallowed.** Both providers fire in parallel via `asyncio.gather`; an errored source counts as `-1` so the survivor wins automatically. When exactly one failed, the response gains `partial_failure: {source, ...}` (built by `_source_error`) so a short or empty result isn't read as a confident "no references." Both failing → both error messages.

All four graph tools thread `force_refresh` into every source they touch — both providers for the references pair, OpenCitations alone for the citations pair, which has no `source` parameter because OpenCitations is the only provider of incoming citations and a one-value knob is noise. Add one when a second source ships.

## Search tools

- **Search hits warm the paper cache in the provider layer**, so both search tools can promise a follow-up `get_paper_metadata(id)` is free — keep that promise in the docstrings if the warming changes (`.claude/rules/providers.md`).
- **Date extraction is single-homed.** `_app._crossref_date` returns `(year, ISO-date)` off `_CROSSREF_DATE_KEYS`; `paper._format_crossref_metadata` takes both, `search_crossref_by_title` takes `[0]`. `posted` is last in that order and is what covers preprints, including every bioRxiv DOI — a second copy would let the two disagree about whether `posted` counts. `first_author` falls back to a consortium `name` field when given/family are absent.
- **`unindexable_note` is built per-reason** from `cache_search.unindexable()`'s `reason` field — it must never assert one cause for all of them. Engine internals, including the CJK trade the docstring warns about, live in `.claude/rules/search.md`.

## bibtex.py

Three entry points, one per provider shape (`generate_bibtex` / `generate_arxiv_bibtex` / `generate_biorxiv_bibtex`); entry-type selection per source is in `get_paper_bibtex`'s docstring. All three share helpers for surname particles (`van`, `de la`, `von`, etc.) in citation keys and author formatting (`_format_names` + `_format_one_name`, parameterised by a `name_of` accessor so OpenAlex's nested `author.display_name` and arXiv/bioRxiv's flat `name` reuse one code path).

Output-correctness contracts (so generated entries always compile):

- **Citation keys are ASCII `[a-z0-9]`.** `_key_token` transliterates the common non-decomposable characters (`ø ł ß đ æ œ þ ð ı`, via `_TRANSLIT`) that `_textnorm.fold` can't strip, then folds diacritics, lowercases, and drops everything else (apostrophes, hyphens, periods, spaces, surviving non-ASCII). `_extract_last_name` and `_first_key_word` both route through it.
- **Escaping treats field text as literal.** `_escape_bibtex` neutralises the full LaTeX special set — strips literal `{ }` first (so they can't unbalance the field), then escapes `\` → `\textbackslash{}`, `& % $ # _`, and `~ ^` → `\textascii*{}`. It does **not** preserve braces for case-protection.
- **`_escape_doi` escapes the same fatal set per-character in one pass instead of stripping braces** — a DOI must stay resolvable, so `{` `}` are escaped rather than dropped. Single pass, not chained `str.replace`: escaping `\` first would emit braces a later brace-pass re-escapes.
- **Organisational authors are brace-wrapped.** `_format_one_name` detects consortium/collaboration names (`_ORG_RE`) and emits `{The ATLAS Collaboration}` so BibTeX treats them atomically instead of splitting off a fake surname.
- **Cross-entry key disambiguation is out of scope.** These functions are stateless (one paper per call) and cannot see sibling entries, so two papers sharing author+year+title-word collide on one key. A caller concatenating many entries into a single `.bib` must deduplicate keys itself.
