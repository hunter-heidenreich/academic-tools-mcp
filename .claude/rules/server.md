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

`_app.py`'s docstring states the one-way edge (it never imports `tools`). The rule it doesn't state: **a helper needed by two tool modules moves into `_app.py`** rather than being imported across them — `tools/paper.py` importing from `tools/search.py` is the violation to catch, not the cycle.

Tool modules call providers directly (`crossref.get_work(...)`, `openalex.get_work(...)`) and tests monkeypatch the provider. **Don't add a passthrough wrapper in `_app` to create a patch point** — patch the provider instead; a wrapper that exists only as a test seam is dead weight the moment nothing patches it.

**A new field on a paper response has more than one formatter to reach.** `_format_metadata_by_source` covers `get_paper_metadata` and `get_papers_metadata`'s *singleton* closure; the batch closure calls `_format_openalex_metadata` directly, so a field added only to the shared helper silently misses every batched OpenAlex DOI.

## `server.py` — re-exports and the debug gate

Adding a tool to a `tools/*.py` module registers it with FastMCP (the decorator runs on import), but it is not reachable as `server.<name>` until you add it to `server.py`'s import list **and** `__all__` — the test suite drives every tool that way and monkeypatches providers as `server.<provider>`.

`_DEBUG_TOOLS_ENABLED` is read from `config.flag("ENABLE_DEBUG_TOOLS")` **at import**, so the gate needs a restart, and `get_server_stats` is defined *inside* the `if` — an agent must never be able to observe cache/throttle state. Don't hoist the definition out and gate registration instead.

## Cross-tool response contracts

These hold across several tools, so changing one tool alone breaks the set.

- **`_source` and `_canonical_id` on every paper-family response.** All four dispatch through `manual.resolve_metadata_source()` and there is deliberately **no lowest-common-denominator normalisation** — agents branch on `_source` for provider-specific fields, so the three shared tags (`arxiv` / `biorxiv` / `openalex`) must mean the same thing in all four. `crossref` and `openalex_via_biorxiv` are `get_paper_metadata`-only, because `fallback_crossref` and `follow_published` are parameters of that one tool.
- **Search-list shape.** Every search tool reports `result_count` (= `len(results)`, what this call returned). `search_arxiv` and `search_crossref_by_title` additionally carry `total_results`, which must be the provider's own upstream count (`opensearch:totalResults`, `message.total-results`), never `len(results)` — that is what tells an agent more exist beyond the page. `search_wikipedia` / `search_cached_papers` have no upstream total and report only `result_count`; `find_in_paper` reports `truncated` instead. Every search tool owes the agent *some* "more exist" signal — pick one of the three, don't ship a tool with none.
- **`page_institutions` / `page_institution_count` appear on every `get_paper_authors` branch** — populated for OpenAlex (from the current page only, so the page cap holds), empty for arxiv/biorxiv. The shape stays symmetric so paginating agents never feature-detect.
- **No cache filesystem path crosses the MCP boundary.** `download_pdf`, `convert_paper` (success *and* error paths) and `import_paper` filter their result through `_strip_internal_paths`; a new response key holding a path must be added to `_INTERNAL_PATH_KEYS`. Agents drive the pipeline by identifier, not by reading files.

## `follow_published` and the batch path

`get_paper_metadata(..., follow_published=True)` chains a bioRxiv paper with a `published_doi` to `openalex.get_work(published_doi)` and returns the journal record as `_source: "openalex_via_biorxiv"`, `_canonical_id` set to the journal DOI, plus a `preprint_doi` field.

The `followed_published` flag is the part to keep consistent: `_format_biorxiv_metadata` sets it via a keyword-only param, `_format_openalex_via_biorxiv` sets it unconditionally, and it stays **absent** when no chain was attempted (`follow_published=False`, or no `published_doi`) and on the batch path — so the default response shape is unchanged. A fallback to the preprint record (OpenAlex hasn't indexed the journal version yet) carries `followed_published: False`, making the lag explicit rather than silent; a *transient* chain failure additionally carries `published_lookup_retryable: True`, distinguishing "not indexed" from "lookup blipped".

`get_papers_metadata` groups identifiers by source, fans arXiv / bioRxiv out as concurrent singletons, and routes OpenAlex DOIs through `openalex.get_works_batch` — one HTTP call per `_BATCH_CHUNK_SIZE` *uncached* DOIs. Each entry mirrors the `get_paper_metadata` payload exactly plus an `_input` field carrying the original identifier, so an agent can correlate input to output.

## `force_refresh` cascade semantics

Stage-specific, and the cascade rules are the subtle part:

- `download_pdf` — re-downloads and atomically replaces the cached PDF. **On success only** (`cached=False`), `_download_pdf_by_provider` also unlinks the cached markdown, invalidates the section index, and tags the response `cascaded_invalidated: ["markdown", "sections"]` — so the next `convert_paper` picks up the new bytes without the agent remembering to refresh it too. A cache hit does **not** cascade (existing markdown is still consistent); a *failed* refresh does **not** cascade either, keeping the preserved PDF and its markdown consistent.
- `convert_paper` — drops both cached markdown and section index so the converter subprocess re-runs.
- `get_paper_sections` — drops just the section index so the next read re-parses markdown.
- `import_paper` — same PDF cascade via `manual._invalidate_derived`; the MCP layer additionally slims the markdown branch to `section_count`, so the agent calls `get_paper_sections` for the full index.

`get_paper_section` reads the markdown file directly with no derived cache, so it has no `force_refresh`. **Every markdown read is off the event loop (`asyncio.to_thread`) and explicit UTF-8** — `get_paper_section`, `find_in_paper`, `papers._reparse_sections_locked`, `manual.import_markdown`. The two tools that read *outside* the lock (`get_paper_section`, `find_in_paper`) additionally catch `FileNotFoundError` and degrade to the shared "not converted" error, because a concurrent cascade can unlink between their `exists()` check and the read; `_reparse_sections_locked` instead relies on holding `papers.sections_lock`, which every unlinker also takes. Don't drop either guard. `get_paper_section` also carries an `anthropic/maxResultSizeChars` meta pinned to `_SECTION_HARNESS_CAP`, the same constant `SECTION_MAX_CHARS` is capped at.

Streaming, the size cap, and the download protocol belong to `_pdf_download` — see `.claude/rules/pdf-download.md`.

## Conversion modes and error shapes

`CONVERT_MODE` stays `Literal["full", "fast"]`: `"imported"` is provenance you can *receive* (a pre-converted file handed to `import_paper`), not a backend you can request. `null` appears only for papers converted before the field existed. Both modes write the same cache slot, so a later `mode="full"` + `force_refresh` upgrades a fast conversion.

Error shapes, distinguished by what the suggestion should tell the agent to do next:

- A missing or unusable PDF short-circuits before `papers.convert_pdf` into `_app.pdf_not_cached_error` — `{error, suggestion}` with **no `retryable` key**.
- Converter crash / empty output → `{error, retryable: False, pdf_size_mb}`. In fast mode the spawn-failure suggestion points at poppler-utils or the `[fast]` extra.
- Timeout → adds `timed_out: True, timeout_seconds, pdf_size_mb`. On a **full-mode** timeout the suggestion points at retrying with `mode="fast"`.
- Another conversion in flight → `{error, retryable: True, busy: True, in_progress: {...}, pdf_size_mb}`, **full mode only** — fast mode runs outside the global lock and can never produce it. The busy suggestion also offers `mode="fast"`.

## Reference / citation graph tools

**`auto` is biased toward Crossref by `_CROSSREF_HYSTERESIS`, not a plain max.** Crossref entries carry structured author/title/year/journal metadata where OpenCitations returns bare DOI-to-DOI links, so it must win by a margin, not by a row or two. Do not "simplify" this to `oc_count > cr_count`.

**`source="auto"` resolves on page 1 only** — `page > 1` with `auto` returns an error telling the agent to pin the `_source` from page 1. Re-surveying mid-walk could pick a different provider and silently shift `total` and the slice offsets. Pages 2..N should also drop `force_refresh` so they reuse the warmed cache.

**A single-source failure is surfaced, not swallowed.** An errored source counts as `-1` so the survivor wins automatically. When exactly one failed, the response gains `partial_failure: {source, ...}` (built by `_source_error`) so a short or empty result isn't read as a confident "no references." Both failing → both error messages.

All four graph tools thread `force_refresh` into every source they touch — both providers for the references pair, OpenCitations alone for the citations pair, which has no `source` parameter because OpenCitations is the only provider of incoming citations and a one-value knob is noise. Add one when a second source ships.

## Search tools

- **Search hits warm the *provider's own* cache, not the dispatcher's.** `arxiv.search_papers` warms the arXiv namespace, so `search_arxiv` → `get_paper_metadata(arxiv_id)` really is free. `crossref.search_works` warms the Crossref namespace — but `manual.resolve_metadata_source()` sends every plain DOI to **OpenAlex**, so a `search_crossref_by_title` hit is free only for the reference tools and the `fallback_crossref` path, never for `get_paper_metadata`. Don't promise otherwise in a docstring, in `README.md`, or in `_app.py`'s `instructions=` string.
- **Date extraction is single-homed** in `_app._crossref_date` / `_CROSSREF_DATE_KEYS` (the comment there says why `posted` is last). `paper._format_crossref_metadata` takes both elements, `search_crossref_by_title` takes `[0]`; don't add a second walker. `first_author`'s consortium-`name` fallback in `search_crossref_by_title` is the matching quirk.
- **`unindexable_note` is built per-reason** from `cache_search.unindexable()`'s `reason` field — it must never assert one cause for all of them. Engine internals, including the CJK trade the docstring warns about, live in `.claude/rules/search.md`.

## bibtex.py

Three entry points, one per provider shape (`generate_bibtex` / `generate_arxiv_bibtex` / `generate_biorxiv_bibtex`); entry-type selection per source is in `get_paper_bibtex`'s docstring. All three share helpers for surname particles (`van`, `de la`, `von`, etc.) in citation keys, author formatting (`_format_names` + `_format_one_name`, parameterised by a `name_of` accessor so OpenAlex's nested `author.display_name` and arXiv/bioRxiv's flat `name` reuse one code path), key generation for the flat providers (`_flat_key`), and entry assembly (`_render_entry` — the single site that turns a `(name, value)` list into `@type{key, ...}`).

Output-correctness contracts (so generated entries always compile):

- **Citation keys are ASCII `[a-z0-9]`.** `_key_token` gates the word components (`_extract_last_name`, `_first_key_word`) and `_key_year` gates the year — digits or nothing, so a null or malformed upstream year drops out instead of printing `None` into the key. A new key component routes through one of the two.
- **Every value reaching a field is escaped**, including the ones that look numeric: `biblio`'s volume / issue / pages arrive from Crossref as freeform strings and occasionally as numbers. `_escape_bibtex` treats field text as literal (braces stripped, whitespace runs collapsed — an Atom-wrapped `journal_ref` would otherwise split the one-field-per-line layout) and `_escape_doi` does not, because a DOI must stay resolvable, so braces are escaped rather than stripped. `_escape_doi` also guards the identifier-shaped fields, `eprint` and `primaryclass`. A URL inside `\url{}` takes neither: url.sty gives it verbatim catcodes, so `_url_field` percent-encodes the fatal characters instead — a backslash escape would land in the link target. Both are single-pass, never chained `str.replace`: escaping `\` first would emit braces a later brace-pass re-escapes.
- **`_TYPE_MAP`'s keys are OpenAlex's `type` vocabulary, not Crossref's.** `type_crossref` no longer exists on the work object, so `proceedings-article` / `posted-content` / `monograph` can never arrive and must not be re-added as keys; a conference paper is `conference-paper`. Re-derive the list from `api.openalex.org/works?group_by=type` when adding a type, and let anything unlisted fall through to `@misc`. The preprint-only `eprint` / `howpublished` block keys on the *work type*, not on `@misc` — datasets and software land in `@misc` too.
- **Titles are double-braced** (`_title_field`), so no `.bst` can case-fold `NaCl` to `nacl`.
- **Surname particles have two detectors, and the split is deliberate.** `_PARTICLES` holds only the particles publishers *capitalize*; `_is_particle`'s case rule — BibTeX's own "a lowercase word before the last one is the von part" — covers the rest, gated on the surname being capitalized so an all-lowercase display name doesn't collapse into one particle run. Don't grow the wordlist to chase the long tail: in real OpenAlex records a capitalized `Du`, `Den`, `Bin`, `E.` or `I.` is a Chinese given name or an initial, not a particle, and the lowercase spellings (`da Costa`, `do Nascimento`, `ter Braak`) are already handled.
- **`_TITLE_SKIP` is closed-class only**, English plus the articles and prepositions of the major publication languages, because OpenAlex carries the original-language title. `_first_key_word` keeps hyphenated and apostrophized compounds whole (`Pre-exposure` → `preexposure`) and strips a one-letter Romance elision (`L'exil` → `exil`); a wholly numeric token is skipped, a digit inside a word is not.
- **Organisational authors are brace-wrapped.** `_format_one_name` detects consortium/collaboration names (`_ORG_RE`) and emits `{The ATLAS Collaboration}` so BibTeX treats them atomically instead of splitting off a fake surname.
- **An arXiv DOI is recognised by its prefix** (`_ARXIV_DOI_RE`), never by splitting on `/`: the id of an old-style work *contains* a slash (`10.48550/arXiv.hep-th/9901001`), and a DOI merely containing "arxiv" in its suffix is not an arXiv DOI. Any other preprint gets a `howpublished` URL built from the **normalized** DOI, falling back to the OpenAlex landing page, and omitted when there is neither — never a bare `\url{}`.
- **OpenAlex nulls are load-bearing.** It emits `"author": null` / `"display_name": null` / `"authorships": null` rather than dropping the key, so every read is `or`-defaulted (`_author_display_name` is the accessor) and no `.get(k, default)` alone is trusted.
- **Cross-entry key disambiguation is out of scope.** These functions are stateless (one paper per call) and cannot see sibling entries, so two papers sharing author+year+title-word collide on one key. A caller concatenating many entries into a single `.bib` must deduplicate keys itself.
