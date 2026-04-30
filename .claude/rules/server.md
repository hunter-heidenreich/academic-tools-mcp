---
paths:
  - "src/academic_tools_mcp/server.py"
  - "src/academic_tools_mcp/bibtex.py"
---

# server.py and BibTeX

## server.py

FastMCP tool definitions (19 live tools) plus `_lifespan` async context manager that closes pooled clients via `_clients.aclose_all()` on shutdown. Each tool fetches the full cached object then returns only the relevant slice. Tools use `Annotated` types (`DOI`, `AUTHOR_ID`, `PAPER_ID`) for parameter descriptions.

### Unified paper family

`get_paper_metadata` / `get_paper_authors` / `get_paper_abstract` / `get_paper_bibtex` accept any `PAPER_ID` and dispatch via `manual._resolve_metadata_source()` to arXiv, bioRxiv, or OpenAlex. Every successful response carries:

- `_source` — which provider served it (`"arxiv"` / `"biorxiv"` / `"openalex"`)
- `_canonical_id` — the provider's normalized form of the input (version-stripped lowercased arXiv ID, lowercased bare DOI, etc.)

There is no lowest-common-denominator normalisation — agents branch on `_source` for provider-specific fields. All four also accept `force_refresh: bool = False` — drops both positive and negative cache entries via `cache.invalidate(...)` and re-fetches; useful for stale citation counts, a bioRxiv preprint that just got published, or retrying a previously-404'd identifier.

`get_paper_metadata` additionally accepts `follow_published: bool = False` — when `True` and a bioRxiv paper has a `published_doi`, auto-chains to `openalex.get_work(published_doi, force_refresh=force_refresh)` and returns the journal record with `_source: "openalex_via_biorxiv"`, `_canonical_id` set to the journal DOI, plus a `preprint_doi` field. Falls back to the preprint record if OpenAlex misses (paper too new to index).

### get_paper_authors pagination

Paginated (`page`, `page_size` default 25, cap 25) to bound response size on large-collaboration papers (HEP, biology consortia) that can carry thousands of authors. Every response includes `author_count` (global total), `has_more`, and the current page. Since the upstream paper response is cached per canonical identifier, paging is pure in-memory slicing — zero extra API cost.

The institution roll-up (`page_institutions` / `page_institution_count`) appears on every branch — populated on OpenAlex (derived from the current page only so the cap holds; agents needing a global list dedupe across pages), empty on arxiv/biorxiv. The shape stays symmetric so paginating agents don't have to feature-detect.

The OpenAlex-shaped `get_paper_authors` response includes `openalex_id` per author so agents can chain into `get_author`. arXiv and bioRxiv responses don't carry this.

### Unified PDF pipeline

`download_pdf` → `convert_paper` → `get_paper_sections` → `get_paper_section`. Auto-detects provider via `manual._resolve_target()` and routes to the correct cache namespace — works for arXiv IDs, ACL DOIs, bioRxiv DOIs, and manually imported papers.

`force_refresh: bool = False` on the first three with stage-specific semantics:

- `download_pdf` — unlinks the cached PDF and re-downloads (use when cached file is corrupt or provider replaced it under the same canonical key).
- `convert_paper` — drops both cached markdown and section index so the converter subprocess re-runs (use after replacing source PDF or upgrading converter).
- `get_paper_sections` — drops just the section index so next read re-parses markdown.

`get_paper_section` reads the markdown file directly (no derived cache) so it has no `force_refresh`. Paginated by character offset: `offset` (default 0) + `max_chars` (default 16000, hard cap 200000). Every response carries `total_chars`, `chars_returned`, `has_more`, `next_offset` so agents read long sections by re-calling with `offset=next_offset` rather than asking for an unbounded slice. Carries `anthropic/maxResultSizeChars=200000` meta so Claude Code doesn't persist large results to disk.

### convert_paper error shapes

- `{error, retryable: False}` for permanent failures (missing PDF, converter crash).
- `{error, retryable: False, timed_out: True, timeout_seconds, pdf_size_mb}` on `PDF_CONVERT_TIMEOUT`.
- `{error, retryable: True, busy: True, in_progress: {...}}` when another conversion is already in flight.

### Pipeline tool boundary

The PDF pipeline tools (`download_pdf`, `convert_paper`, `import_paper`) deliberately strip cache filesystem paths from their responses at the MCP boundary so agents drive the pipeline by identifier through the tools rather than reading files directly.

### import_paper

Single tool that auto-detects `.pdf` vs `.md`/`.markdown` by extension. PDFs are validated by their `%PDF-` magic bytes (rejects mis-extension files before they reach the converter); markdown is read as UTF-8 with a clean error on decode failure. The MCP-layer response slims the markdown branch to `section_count` only — the agent calls `get_paper_sections` if it wants the full index.

### Reference / citation graph tools

- `get_paper_references_count` — surveys both Crossref and OpenCitations in parallel, returns per-source counts.
- `get_paper_references(doi, source, page, page_size)` — defaults `source="auto"`, fires both providers in parallel via `asyncio.gather`, picks whichever has more references (tie → Crossref for richer per-entry metadata), falls back to surviving source if one errors. Both errors → response carries both error messages. Explicit `source="crossref"` or `source="opencitations"` skips the survey (important for paginating page=2..N).
- `get_paper_citations_count` / `get_paper_citations` — incoming citations (OpenCitations only today). `get_paper_citations` accepts `source: Literal["auto", "opencitations"] = "auto"` so a future second source can ship without a breaking change.

Crossref provides structured reference metadata (author, title, year, journal, DOI) when publishers deposit it; quality varies. OpenCitations aggregates from Crossref/PubMed/DataCite/OpenAIRE/JaLC and returns DOI-to-DOI links with cross-referenced IDs (OMID, OpenAlex, PMID) and self-citation flags — broader coverage, no bibliographic metadata.

### Search tools

- `search_arxiv` — `{total_results, results: [...]}`; each hit `{arxiv_id, title, first_author, author_count, published_year}`. Full-author lists balloon on HEP/biology papers, so search drops everything beyond triage. Each entry opportunistically cached — follow-up `get_paper_metadata(arxiv_id)` is free.
- `search_crossref_by_title` — DOI discovery by bibliographic query. Useful when you only have a title or arXiv ID and need the published DOI (e.g. ACL Anthology DOI for an arXiv paper). Year filtering is optional but Crossref publication dates may differ from arXiv preprint dates. De facto search for bioRxiv (no title search endpoint upstream — Crossref indexes all bioRxiv DOIs). Hits return `{doi, title, first_author, author_count, year}` (parallel to `search_arxiv`). Each hit also opportunistically warms `crossref/works`.
- `search_cached_papers` — BM25 over locally-converted markdown across all namespaces (or filtered to one). Use case: "I read this paper a few weeks ago, what was its identifier?" or "which of my imported PDFs talked about X?" — neither answerable by upstream search APIs. Returns `{namespace, canonical_id, score, title, snippet, section, char_count}`; chain `get_paper_section(canonical_id, section)`. Pure keyword match — won't bridge synonyms, doesn't see un-converted PDFs.

## bibtex.py

Generates BibTeX entries for three provider shapes:

- `generate_bibtex()` — raw OpenAlex work object, maps `type` → BibTeX entry type via `_TYPE_MAP`.
- `generate_arxiv_bibtex()` — `@misc` (preprint) or `@article` (published) with `eprint`/`archiveprefix`/`primaryclass` fields.
- `generate_biorxiv_bibtex()` — `@article` when `published_doi` set, else `@misc` with preprint DOI, server name, `howpublished` URL.

All three share helpers for surname particles (`van`, `de la`, `von`, etc.) in citation keys and author formatting.
