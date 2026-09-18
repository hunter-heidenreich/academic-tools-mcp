---
paths:
  - "src/academic_tools_mcp/server.py"
  - "src/academic_tools_mcp/app.py"
  - "src/academic_tools_mcp/tools/*.py"
---

# app, tools and the server entry

Per-tool parameters and response keys live in the `@mcp.tool` docstrings, which
are what agents receive as the tool description. This file holds the invariants
that span tools.

## Layout

**`app.py` never imports `tools`, and no `tools/*` module imports another**, so a
helper two tool modules need moves into `app.py`. `tools/paper.py` importing from
`tools/search.py` is the violation to catch, not the cycle.

**Don't add a passthrough wrapper in `app` to create a patch point.** Tool modules
call providers directly and tests monkeypatch the provider module object, which
every importer shares.

**A new field on a paper response has more than one formatter to reach.**
`_format_metadata_by_source` covers `get_paper_metadata` and
`get_papers_metadata`'s *singleton* closure; the batch closure calls
`_format_openalex_metadata` directly, so a field added only to the shared helper
silently misses every batched OpenAlex DOI.

## Cross-tool response contracts

These hold across several tools, so changing one tool alone breaks the set.

- **Every response echoes the canonical cache key, not the caller's spelling** —
  `_canonical_id` for the paper family, `doi` for the graph tools,
  `target["canonical"]` for `find_in_paper`. One markdown file, one identity.
- **`_source` carries no lowest-common-denominator normalisation.** The five
  shared tags (`arxiv` / `biorxiv` / `acl_anthology` / `openalex` / `crossref`)
  must mean the same thing in all four paper tools, and a paper reachable through
  one must be reachable through all of them. Each precondition lives exactly once
  — `paper._follow_published`, `paper._crossref_fallback`, `paper._biorxiv_fallback`,
  `paper._flag_fallback`. A tool that spells any of it itself is how the four
  start disagreeing.
- **Every search tool owes the agent *some* "more exist" signal** — `total_results`
  (the provider's own upstream count, never `len(results)`), `result_count` where
  there is no upstream total, or `truncated`. Pick one; don't ship a tool with none.
- **No cache filesystem path crosses the MCP boundary.** `download_pdf`,
  `convert_paper` (success *and* error paths) and `import_paper` filter through
  `_strip_internal_paths`, which filters key *names* — **a new response key holding
  a path must be added to `_INTERNAL_PATH_KEYS`.**
- **Every error a pipeline tool returns carries a `suggestion`.** `net/http`'s
  vocabulary is a retry verdict, not advice, and providers add none, so the tool
  layer is where a download failure learns that `import_paper` exists.
  `enrich_error` fills a gap and never overwrites.
- **Verdicts read `net/http`'s three-state vocabulary, never the absence of another
  key.** `retryable is True` is the only test meaning "a retry might work";
  inverting `not_found` collapses three states into two and sends an agent back at
  a call that cannot succeed.
- **Search parameters bind to the provider's own constant, never a transcribed
  number.** The provider's `MAX_*` is the `le=` of its `Field`.

## `force_refresh` cascade

**The `download_pdf` cascade is keyed on what happened, not on what the caller
asked for.** Whenever new bytes land — `cached is False`, never falsiness, since
an absent flag is not a claim of freshness — the cached markdown and section index
are dropped. `force_refresh` is **not** part of the condition: a PDF evicted and
refilled would otherwise leave markdown describing a file that is gone. A cache
hit doesn't cascade, and neither does a *failed* refresh.

**The exceptions are markdown the PDF did not produce** (`pipeline._NOT_FROM_PDF`):
no converter reproduces an operator's own markdown, and provider markup does not
change with the PDF bytes. An explicit `force_refresh=True` still replaces them.

## Conversion modes

**`CONVERT_MODE` stays `Literal["full", "fast"]`.** `"imported"`, `"html"` and
`"jats"` are provenance you can *receive*, not backends you can request. **The
markup attempt runs before the PDF gate**, so an arXiv or bioRxiv paper converts
with no PDF.

**Invariant: every `convert_pdf` error carries `retryable` and `conversion_mode`,
plus `pdf_size_mb` once the PDF has been sized.** Two exceptions, both because the
key would be a lie: `app.pdf_not_cached_error` has no `retryable` (nothing was
tried), and an unknown `mode` is rejected with no `conversion_mode`.

## DOI-only tools

**`app.resolve_doi_identifier` is the one front door**, so the non-DOI trades, the
Anthology trade, the rejection and canonicalisation keep one order across every
DOI-only tool. **`_trade_non_doi` holds both non-DOI trades** — the point of
trading an id the graph tools *emit* is that those tools take it back. A third one
goes there, not into a caller.

## Graph tools

- **`auto` is biased toward Crossref by `_CROSSREF_HYSTERESIS`, not a plain max.**
  Crossref rows carry structured metadata where OpenCitations returns bare
  DOI-to-DOI links, so it must win by a margin. **Do not "simplify" this to
  `oc_count > cr_count`.**
- **Crossref reference rows are type-checked, not just the list.** `_message_of`
  only checks the `message` is a dict, so `_crossref_refs` filters `reference` to
  dicts — the one list both the count tool and the page tool read.
- **A single-source failure is surfaced, not swallowed.** An errored source counts
  as `-1` so the survivor wins, and `partial_failure` keeps a short result from
  reading as a confident "no references".

## Pagination

**`app.page_bounds` is the one home for the page arithmetic**, so `tools/graph._page`
and `get_paper_authors` cannot drift on where a page begins or on
`has_more = end < total`. Only the arithmetic is shared; each tool keeps its own
envelope keys and its own `le=` bound. `get_paper_section` pages by character
offset and shares none of this.

**Bounds are enforced at the MCP boundary**, not in Python — an in-process caller
can pass `page=0`. Don't add defensive clamping for inputs an agent cannot send;
constrain the test domain instead.

## Search tools

- **Search hits warm the *provider's own* cache, not the dispatcher's.**
  `manual.resolve_metadata_source()` sends every plain DOI to **OpenAlex**, so a
  `search_crossref_by_title` hit is free only for the reference tools and the
  `fallback_crossref` path — never for `get_paper_metadata`. `openalex.search_works`
  is the one that lands where the dispatcher looks, which is why it must never send
  `select=`. **This file is the authority**; a docstring, `README.md` or
  `app.py`'s `instructions=` string that says otherwise is the one to fix.
- **Date extraction is single-homed** in `app.crossref_date` / `_CROSSREF_DATE_KEYS`.
  Don't add a second walker.
- **Nothing below a Crossref item is typed, so every read of one is shape-guarded**
  — `app.as_dict` / `app.dict_list` are the shared guards, in `app.py` because both
  the OpenAlex tree and the Crossref hits need them. **OpenAlex nulls are
  load-bearing**: it emits `"author": null` rather than dropping the key, so no
  `.get(k, default)` alone is trusted. **`author_count` counts the filtered list**,
  the one `_crossref_first_author` chose from.
- **`total_results` is an `int` on every tool that reports it**, defaulted to `0`
  where the provider omits its total — a key meaning two things across the set is a
  key an agent cannot branch on.
- **`_UNINDEXABLE_REASONS`' keys equal `corpus.UNINDEXABLE_REASONS`**, pinned in CI
  because each explanation is hand-written. That is what stops a reason added to
  the engine falling through to the residual.
