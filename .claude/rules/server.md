---
paths:
  - "src/academic_tools_mcp/server.py"
  - "src/academic_tools_mcp/app.py"
  - "src/academic_tools_mcp/tools/*.py"
---

# server and tools

**Per-tool parameters and response keys live in the `@mcp.tool` docstrings**,
which are also what agents receive as the tool description —
`grep -rn '@mcp.tool' src/academic_tools_mcp/tools/` for the roster. This file
covers only what no single docstring can: the wiring between modules, and the
invariants that span tools.

## Layout: `app.py` + `tools/` + thin `server.py`

`app.py` never imports `tools`. The rule that follows and isn't stated there:
**a helper needed by two tool modules moves into `app.py`** rather than being
imported across them — `tools/paper.py` importing from `tools/search.py` is the
violation to catch, not the cycle.

**Don't add a passthrough wrapper in `app` to create a patch point.** Tool
modules call providers directly and tests monkeypatch the provider; a wrapper
that exists only as a test seam is dead weight the moment nothing patches it.

**A new field on a paper response has more than one formatter to reach.**
`_format_metadata_by_source` covers `get_paper_metadata` and
`get_papers_metadata`'s *singleton* closure; the batch closure calls
`_format_openalex_metadata` directly, so a field added only to the shared helper
silently misses every batched OpenAlex DOI.

## `server.py` — re-exports

**Invariant: `__all__` holds tool callables, the provider modules and `mcp` —
never a tool module's internals.** A test that needs `_format_crossref_metadata`
or `_download_pdf_by_provider` imports `tools.paper` / `tools.pipeline` directly.
Re-exporting one gives a private helper a second import name and puts an
underscore in the list that *defines* the module's public surface.

Nothing is lost by importing the owning module:
`monkeypatch.setattr(server.arxiv, ...)` mutates the provider *module object*,
which every importer shares, so a provider stub reaches a helper called through
`tools.<group>` exactly as it did through `server`.

## Cross-tool response contracts

These hold across several tools, so changing one tool alone breaks the set.

- **Every response echoes the canonical cache key, not the caller's spelling.**
  `_canonical_id` for the paper family, `doi` for the graph tools,
  `target["canonical"]` for `find_in_paper`. One markdown file, one identity — so
  `10.1234/X`, `doi:10.1234/x` and the resolver URL, already one cache key, also
  correlate to one value across calls.
- **`_source` carries no lowest-common-denominator normalisation.** Agents branch
  on it for provider-specific fields, so the three shared tags (`arxiv` /
  `biorxiv` / `openalex`) must mean the same thing in all four paper tools.
  `crossref` and `openalex_via_biorxiv` are `get_paper_metadata`-only, because
  `fallback_crossref` and `follow_published` are parameters of that one tool.
- **Every search tool owes the agent *some* "more exist" signal** —
  `total_results` (the provider's own upstream count, never `len(results)`),
  `result_count` alone where there is no upstream total, or `truncated`. Pick one
  of the three; don't ship a tool with none.
- **Response-shape keys stay symmetric across branches** so paginating agents
  never feature-detect — `page_institutions` / `page_institution_count` are
  emitted empty for arxiv/biorxiv rather than omitted.
- **Every response key a tool returns is named in its `@mcp.tool` docstring.**
  The docstring *is* the agent's tool description, so a key it omits is a key no
  agent will look for. Add a key, add it to the docstring in the same edit.
- **A docstring does not restate its own parameters.** The `Annotated` `Field`
  descriptions ship to the agent beside it, so a docstring paragraph re-explaining
  `mode` or `force_refresh` is duplicated in the agent's context, not just in the
  file — and the two spellings then drift. Parameter semantics live in the
  `app.py` alias; the docstring carries what no single parameter owns: the tool's
  job, its response keys, its error shapes, the next step.
- **No cache filesystem path crosses the MCP boundary.** `download_pdf`,
  `convert_paper` (success *and* error paths) and `import_paper` filter their
  result through `_strip_internal_paths`; **a new response key holding a path must
  be added to `_INTERNAL_PATH_KEYS`.** The helper filters key *names*, so it is
  only as correct as that tuple is current. Agents drive the pipeline by
  identifier, not by reading files.
- **Every error a pipeline tool returns carries a `suggestion`.** `net/http`'s
  error vocabulary is a retry verdict, not advice, and the providers add none, so
  the tool layer is where a download failure learns that `import_paper` exists.
  `enrich_error` fills a gap and never overwrites — a provider that shipped its
  own advice (`openaccess`) keeps it.
- **One suggestion per cause; the residual asserts none.** `_convert_suggestion`
  and `_UNINDEXABLE_REASONS` both hold this. A single catch-all contradicts the
  `error` string it rides beside: "the PDF is corrupted, do not retry" sends the
  agent to abandon a paper `mode="full"` converts, and a timeout must point at
  the *other* mode, in both directions.
- **Verdicts read `net/http`'s three-state vocabulary, never the absence of
  another key** (`.claude/rules/net.md`). `retryable is True` is the only test
  that means "a retry might work"; inverting `not_found` collapses three states
  into two and sends an agent back at a call that cannot succeed. This binds
  `follow_published`'s `published_lookup_retryable`, `search_arxiv`'s
  rewrite-vs-wait branch, and every graph error.

## `force_refresh` cascade semantics

Stage-specific, and the cascade rule is the subtle part:

**The `download_pdf` cascade is keyed on what happened, not on what the caller
asked for.** Whenever new bytes land — `cached is False`, `is False` and never
falsiness, since an absent flag is not a claim of freshness — the cached markdown
and section index are dropped. `force_refresh` is **not** part of the condition:
a PDF that was evicted and refilled would otherwise leave markdown describing a
file that is gone. A cache hit does not cascade (the markdown is still
consistent); a *failed* refresh does not either, keeping the preserved PDF and
its markdown consistent.

**The one exception is `conversion_mode == "imported"`**: no converter can
reproduce an operator's own markdown, so an implicit cascade must not destroy it.
An explicit `force_refresh=True` still replaces it — that is what the flag means.

`sections_note` is what stops `sections_detected: false` being read as "this
paper has one section". The distinction matters most on the largest documents,
where every 100 KB+ single-section paper in a real corpus turned out to be a
headingless thesis and blind paging is the worst available strategy.

**Every markdown read is off the event loop (`asyncio.to_thread`) and explicit
UTF-8**, and `app.read_markdown` is the one home for a read taken *outside*
`papers.sections_lock` — `find_in_paper` and `get_paper_section` both go through
it, so its three guards can't drift apart: off the loop, explicit UTF-8, and
`FileNotFoundError` degrading to the shared "not converted" error for the cascade
that unlinks between the `exists()` check and the read. `_reparse_sections_locked`
instead relies on holding the lock, which every unlinker also takes. Don't drop
either guard.

## Conversion modes

**`CONVERT_MODE` stays `Literal["full", "fast"]`.** `"imported"` is provenance
you can *receive* — a pre-converted file handed to `import_paper` — not a backend
you can request. `null` appears only for papers converted before the field existed.

**Invariant: every `convert_pdf` error carries `retryable` and `conversion_mode`,
plus `pdf_size_mb` once the PDF has been sized.** Two deliberate exceptions, both
because the key would be a lie: `app.pdf_not_cached_error` has no `retryable`
(nothing was tried), and an unknown `mode` is rejected with no `conversion_mode`
(the requested value is not in the published vocabulary).

## Reference / citation graph tools

- **`auto` is biased toward Crossref by `_CROSSREF_HYSTERESIS`, not a plain max.**
  Crossref entries carry structured author/title/year/journal metadata where
  OpenCitations returns bare DOI-to-DOI links, so it must win by a margin, not by
  a row or two. **Do not "simplify" this to `oc_count > cr_count`.**
- **Crossref reference rows are type-checked, not just the list.**
  `crossref.get_work` returns the upstream `message` verbatim and `_message_of`
  only checks it is a dict, so `_crossref_refs` filters `reference` to dicts. It
  is the one list both the count tool and the page tool read, which is what stops
  the survey from sending an agent to page a source that then raises.
  `_format_crossref_reference` falls back to Crossref's own `key` when no
  recognized field matched, so a bookkeeping-only deposit never renders as `{}`.
- **A single-source failure is surfaced, not swallowed.** An errored source counts
  as `-1` so the survivor wins automatically, and the response gains
  `partial_failure` so a short or empty result isn't read as a confident "no
  references". The both-sources-failed envelope carries a top-level `retryable`
  that is the disjunction of the nested ones.
- **The citations pair has no `source` parameter** because OpenCitations is the
  only provider of incoming citations and a one-value knob is noise. Add one when
  a second source ships.

## Pagination

**`app.page_bounds` is the one home for the page/page_size arithmetic.**
`tools/graph._page` and `get_paper_authors` both take their `start`/`end` from
it, so they cannot drift on where a page begins or on the `has_more = end < total`
rule. Only the arithmetic is shared — each tool keeps its own envelope keys.
`get_paper_section` pages by character offset and shares none of this.

**Bounds are enforced at the MCP boundary** by `PAGE` (`ge=1`) and `PAGE_SIZE`
(`ge=1, le=50`), not in Python — an in-process caller can pass `page=0`. Don't
add defensive clamping for inputs an agent cannot send; constrain the test domain
instead.

## Search tools

- **Search hits warm the *provider's own* cache, not the dispatcher's.**
  `arxiv.search_papers` warms the arXiv namespace, so `search_arxiv` →
  `get_paper_metadata(arxiv_id)` really is free. `crossref.search_works` warms the
  Crossref namespace — but `manual.resolve_metadata_source()` sends every plain
  DOI to **OpenAlex**, so a `search_crossref_by_title` hit is free only for the
  reference tools and the `fallback_crossref` path, never for
  `get_paper_metadata`. This file is the authority; a docstring, `README.md` or
  `app.py`'s `instructions=` string that says otherwise is the one to fix.
- **Date extraction is single-homed** in `app.crossref_date` /
  `_CROSSREF_DATE_KEYS`. `paper._format_crossref_metadata` takes both elements,
  `search_crossref_by_title` takes `[0]`; don't add a second walker.
- **Nothing below a Crossref item is typed, so every read of one is
  shape-guarded** — `author` through `app.dict_list`, its `given`/`family`/`name`
  values through `isinstance`, and `crossref_date` shape-checks the date value,
  the `date-parts` list *and* its first element. **`author_count` counts the
  filtered list**, the one `_crossref_first_author` chose from, so the count and
  the name can never describe different lists.
- **`app.as_dict` / `dict_list` are the shared shape guards**, in `app.py`
  because `tools/paper.py` (the OpenAlex tree) and `tools/search.py` (Crossref
  hits) both need them and may not import each other. **OpenAlex nulls are
  load-bearing**: it emits `"author": null` / `"authorships": null` rather than
  dropping the key, so no `.get(k, default)` alone is trusted.
- **Search parameters bind to the provider's own constant, never a transcribed
  number**: `arxiv.MAX_SEARCH_RESULTS`, `crossref.MAX_SEARCH_ROWS`,
  `wikipedia.MAX_SEARCH_LIMIT`, `corpus.MAX_TOP_K` are the `le=` of their `Field`.
  A docstring that spells the cap out instead drifts the moment the provider
  moves it.
- **`total_results` is an `int` on both tools that report it.** Crossref omits
  `total-results` on some responses, so the tool defaults it to `0` — a key that
  means two things across the pair is a key an agent cannot branch on.
- **`_UNINDEXABLE_REASONS`' keys equal `corpus.UNINDEXABLE_REASONS`**, pinned in
  CI. Each explanation is hand-written, so the key set is a duplicate CI keeps
  honest rather than a derivation — which is what stops a reason added to the
  engine falling through to the residual. Each reported entry carries the
  `canonical_id` the note tells the agent to hand to `find_in_paper`; a bare
  `stem` is not an identifier any tool resolves.

Streaming, the size cap and the download protocol belong to `streaming` —
`.claude/rules/download.md`. BibTeX generation is `.claude/rules/bibtex.md`.
