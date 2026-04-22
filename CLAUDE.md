# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A FastMCP-based MCP server that wraps the OpenAlex, arXiv, bioRxiv/medRxiv, Crossref, OpenCitations, and Wikipedia APIs to provide lean, focused tools for LLM agents working with academic papers. Designed for verifying paper metadata, authors, institutions, generating BibTeX citations, and exploring reference/citation graphs — primarily in support of a Hugo-based academic notes/blog workflow.

## Commands

```bash
uv sync                          # Install dependencies
uv run pytest -v                 # Run all tests
uv run pytest tests/test_bibtex.py -v                    # Run one test file
uv run pytest tests/test_bibtex.py::TestGenerateKey -v   # Run one test class
uv run pytest -k "test_particle" -v                      # Run tests matching a pattern
uv run python -m academic_tools_mcp.server               # Run the MCP server
```

## Architecture

**Layered design — tools never hit the API directly:**

```
server.py (MCP tools) → openalex.py       (API client) → cache.py (file cache)
                       → arxiv.py          (API client) ↗
                       → biorxiv.py        (API client) ↗
                       → crossref.py       (API client) ↗
                       → opencitations.py  (API client) ↗
                       → acl_anthology.py  (PDF source) ↗
                       → manual.py         (local/URL import) ↗
                       → wikipedia.py      (API client) ↗
                       → papers.py  (PDF → markdown → sections)
                       ↘ bibtex.py (BibTeX generation)
```

- **`cache.py`** — Generic file-based JSON cache under `.cache/<provider>/<entity>/`. Namespaced so it scales to future providers (arxiv, etc.). Files are SHA-256 hashed by identifier. No expiration.
- **`config.py`** — Loads `.env` from project root. All API credentials come from environment variables, never from tool parameters.
- **`_http.py`** — Shared HTTP error normalization for every API client. Exposes `HTTPX_ERRORS` (the tuple of `httpx.HTTPStatusError`, `TimeoutException`, `RequestError`) and `error_dict(provider, exc)` which converts those into structured `{error, retry_after_seconds?}` dicts with provider-aware messages. Every client wraps its request block in `try/except _http.HTTPX_ERRORS` so 5xx, 429 (with `Retry-After` surfaced when present), timeouts, and network failures all return the same dict shape that "not found" lookups already use — agents stay on a single error contract regardless of why the call failed.
- **`openalex.py`** — Thin async client for OpenAlex singleton endpoints (`/works/{id}`, `/authors/{id}`). Handles ID normalization (DOI formats, OpenAlex URLs, ORCIDs) and cache read/write. Each entity type has a `_normalize_*` and `_canonical_*` pair for API path formatting and cache keying respectively.
- **`arxiv.py`** — Thin async client for arXiv's Atom API (`export.arxiv.org/api/query`). Handles ID normalization (bare IDs, URLs, version suffixes) and XML→dict parsing. Enforces arXiv's rate limit (1 request per 3 seconds, single connection) via an `asyncio.Lock` + monotonic timer. Cache namespace: `arxiv/papers`. No API key or env vars required.
- **`biorxiv.py`** — Thin async client for the bioRxiv/medRxiv API (`api.biorxiv.org`). Handles DOI normalization (bare DOIs, URLs, site content URLs with version suffixes). Tries bioRxiv first, falls back to medRxiv. Selects the latest version from multi-version responses. Parses semicolon-separated author strings into structured dicts. Builds PDF URLs from DOI + version + server (biorxiv.org vs medrxiv.org). Rate-limited to ~2 req/sec (500ms gap) as a courtesy (no documented limit). Cache namespace: `biorxiv/papers`. The `published_doi` field links to the journal DOI when available, enabling chaining into Crossref/OpenAlex. No auth required.
- **`manual.py`** — Manual PDF/markdown import for local files, plus the two identifier dispatchers. **Provider-aware routing for PDF storage**: `_resolve_target()` detects the identifier type (arXiv ID, bioRxiv DOI, ACL DOI) and stores PDFs/markdown directly in that provider's cache namespace, so native pipeline tools find them with no duplicates. Unrecognised identifiers fall back to the `manual` namespace. **Metadata dispatch**: `_resolve_metadata_source()` returns `"arxiv" | "biorxiv" | "openalex" | None` — ACL DOIs and generic DOIs route to OpenAlex (ACL has no metadata API); unknown identifiers return `None` so tools can surface a clear error. Supports `~/` expansion for local paths. The module deliberately does **not** download arbitrary URLs — agents are expected to fetch non-native PDFs themselves (browser, curl, institutional proxy) and hand the local file to `import_pdf`. No API, no auth, no rate limits.
- **`wikipedia.py`** — Thin async client for the Wikipedia API. Uses MediaWiki OpenSearch (`/w/api.php?action=opensearch`) for title search and the Wikimedia REST API (`/api/rest_v1/page/summary/{title}`) for page summaries and existence verification. Detects disambiguation pages via the `type` field. Rate-limited to ~1 req/sec (1,000ms gap) per Wikimedia's reader tier guidance. Requires a `User-Agent` header (configured via `WIKIPEDIA_MAILTO` env var). Cache namespace: `wikipedia/summaries`. No auth required.
- **`papers.py`** — Converter-agnostic PDF-to-markdown pipeline and section-level access. `_build_converter_command()` reads `PDF_CONVERTER` (named backend or custom command template) and `PDF_CONVERTER_VENV` (optional venv to activate) from env. Built-in backends: `mineru` (default), `marker`. `convert_pdf()` shells out to the configured converter, stores markdown under `.cache/<namespace>/markdown/`. `parse_sections()` splits by H2 headings with H3 previews (adaptive — detects H1 vs H2 documents). `get_section_content()` retrieves individual sections by index or title substring. Section indices cached under `.cache/<namespace>/sections/`.
- **`crossref.py`** — Thin async client for the Crossref REST API (`api.crossref.org/works/{doi}`). Handles DOI normalization and cache read/write. Enforces polite pool etiquette via `User-Agent` header with `mailto` (from `CROSSREF_MAILTO` env var). Rate-limited to ~10 req/sec (100ms gap) via `asyncio.Lock` + monotonic timer. Cache namespace: `crossref/works`. The full work object is cached; the tool layer slices out just the `reference` list with pagination.
- **`opencitations.py`** — Thin async client for the OpenCitations Index API v2 (`api.opencitations.net/index/v2`). Fetches outgoing references (`/references/doi:...`) and incoming citations (`/citations/doi:...`). Rate-limited to ~3 req/sec (334ms gap, 180 req/min) per OpenCitations policy. Parses space-delimited multi-ID strings (`omid:... doi:... openalex:... pmid:...`) into structured dicts via `_parse_ids()`. Cache namespaces: `opencitations/references`, `opencitations/citations`. No auth required.
- **`acl_anthology.py`** — PDF source for ACL Anthology papers. Resolves DOIs with the ACL prefix (`10.18653/v1/`) to Anthology IDs by stripping the prefix. Downloads camera-ready PDFs from `https://aclanthology.org/{id}.pdf`. No API, no auth, no rate limits — just direct PDF URLs. Cache namespace: `acl_anthology/pdfs`. Tools feed into the same `papers.py` pipeline as arXiv PDFs.
- **`bibtex.py`** — Generates BibTeX entries for three provider shapes. `generate_bibtex()` takes a raw OpenAlex work object and maps `type` → BibTeX entry type via `_TYPE_MAP`. `generate_arxiv_bibtex()` produces `@misc` (preprint) or `@article` (published) with `eprint`/`archiveprefix`/`primaryclass` fields. `generate_biorxiv_bibtex()` produces `@article` when `published_doi` is set, else `@misc` with the preprint DOI, server name, and `howpublished` URL. All three share helpers for surname particles (`van`, `de la`, `von`, etc.) in citation keys and author formatting.
- **`server.py`** — FastMCP tool definitions (18 live tools). Each tool fetches the full cached object then returns only the relevant slice. Tools use `Annotated` types (`DOI`, `AUTHOR_ID`, `PAPER_ID`) for parameter descriptions. **Unified paper family** (`get_paper_metadata`, `get_paper_authors`, `get_paper_abstract`, `get_paper_bibtex`) accepts any `PAPER_ID` and dispatches via `manual._resolve_metadata_source()` to arXiv, bioRxiv, or OpenAlex. Every response carries a `"_source"` tag so agents can branch on provider-specific fields; there is no lowest-common-denominator normalisation. Two OpenAlex-only functions — `get_paper_topics` and `get_paper_citations_summary` — are currently **disabled** (their `@mcp.tool` decorators are commented out) but the function bodies remain; re-enable by uncommenting the decorator. **Unified PDF pipeline** (`download_pdf` → `convert_paper` → `get_paper_sections` → `get_paper_section`) auto-detects the provider via `manual._resolve_target()` and routes to the correct cache namespace — works for arXiv IDs, ACL DOIs, bioRxiv DOIs, and manually imported papers. `get_paper_section` is paginated by character offset: `offset` (default 0) + `max_chars` (default 16000, hard cap 200000). Every response carries `total_chars`, `chars_returned`, `has_more`, and `next_offset` so agents read long sections by re-calling with `offset=next_offset` rather than asking for an unbounded slice. The tool also carries `anthropic/maxResultSizeChars=200000` meta so Claude Code doesn't persist large results to disk. The PDF pipeline tools (`download_pdf`, `convert_paper`, `import_paper`) deliberately strip cache filesystem paths from their responses at the MCP boundary so agents drive the pipeline by identifier through the tools rather than reading files directly. Manual import is a single tool `import_paper(file_path, identifier)` that auto-detects `.pdf` vs `.md`/`.markdown` by extension and routes accordingly (PDF → cache for `convert_paper`; markdown → cache with sections pre-parsed, skipping conversion). Arbitrary-URL download was intentionally removed — agents fetch non-native PDFs themselves and hand the file to `import_paper`. Wikipedia tools (`search_wikipedia`, `get_wikipedia_summary`) support cross-referencing workflows — `get_wikipedia_summary` already reports page type (`"standard"` / `"disambiguation"`) and errors on not-found, so a separate existence-check tool was dropped as redundant. The `wikipedia.page_exists()` client function remains as tested-but-unexposed utility code. **Reference/citation graph tools** are four unified tools using a count-then-page pattern: `get_paper_references_count` surveys both Crossref and OpenCitations in parallel and returns per-source counts so the agent can compare coverage; `get_paper_references(doi, source, page, page_size)` paginates the chosen source (response carries `_source`); `get_paper_citations_count` / `get_paper_citations` cover incoming citations (OpenCitations-only, no source param).

**Key design decisions:**
- Tool responses are intentionally small — an LLM agent should not receive the full OpenAlex response. Each tool returns only what's needed for its purpose.
- All tools for a given DOI or arXiv ID share one cached API response. Multiple tool calls = one API hit.
- **One paper tool per job, not one per provider.** Rather than `get_paper_*` / `get_arxiv_paper_*` / `get_biorxiv_paper_*` families, the four core paper tools take any identifier and dispatch internally. Responses are tagged with `_source` ("arxiv" / "biorxiv" / "openalex") so agents can handle provider-specific fields without pre-guessing which family to call. Dispatch is by identifier shape, not by which provider has more data — `get_paper_metadata("2301.00001")` returns arXiv's native response even if the paper is also in OpenAlex. Agents that want OpenAlex-specific data (topics, citations, venue) call the dedicated OpenAlex-only tools with the paper's DOI.
- OpenAlex singleton lookups (works, authors) are free and unlimited. Search/filter endpoints have daily limits and are not currently implemented.
- arXiv lookups are free but rate-limited (3-second gap enforced in code). Search is also supported with a 50-result cap per call. `search_arxiv` returns a slim triage shape per hit (`{arxiv_id, title, first_author, published_year}`) — full-author lists balloon to tens of KB on HEP/biology papers, so the search tool drops everything beyond what's needed for triage. Each entry is opportunistically cached, so a follow-up `get_paper_metadata(arxiv_id)` is free.
- The OpenAlex-shaped `get_paper_authors` response includes `openalex_id` per author so agents can chain into `get_author`. arXiv and bioRxiv responses do not carry this because those APIs don't expose author IDs.
- `get_paper_authors` is paginated (`page`, `page_size`, default 25, cap 25) to bound response size on large-collaboration papers (HEP, biology consortia) that can carry thousands of authors. Every response includes `author_count` (global total), `has_more`, and the current page. Since the upstream paper response is cached per canonical identifier, paging is pure in-memory slicing — zero extra API cost. On the OpenAlex branch the institution roll-up (`page_institutions` / `page_institution_count`) is derived from the current page only so the cap actually holds; agents needing a global institution list dedupe across pages.
- **Reference/citation graph tools** follow a count-then-page pattern: agents call `_count` first, then page through with `page` and `page_size`. This prevents token blowouts on highly-cited papers. Three providers collapse into four tools: `get_paper_references_count` surveys **both** Crossref and OpenCitations in parallel (one call, per-source counts, partial-failure tolerant via `asyncio.gather`); `get_paper_references(doi, source, page, page_size)` paginates the chosen source and tags the response with `_source` since the two shapes differ; `get_paper_citations_count` and `get_paper_citations` cover incoming citations (OpenCitations only — no source parameter).
- Crossref provides structured reference metadata (author, title, year, journal, DOI) when publishers deposit it; quality varies. OpenCitations aggregates from Crossref/PubMed/DataCite/OpenAIRE/JaLC and returns DOI-to-DOI links with cross-referenced IDs (OMID, OpenAlex, PMID) and self-citation flags — broader coverage, no bibliographic metadata. The count survey lets agents compare coverage before committing to a source.
- `search_crossref_by_title` enables DOI discovery by bibliographic query — useful when you only have a title or arXiv ID and need the published DOI (e.g., to find the ACL Anthology DOI for a paper known only by its arXiv ID). Year filtering is optional but note that Crossref publication dates may differ from arXiv preprint dates. This also serves as the de facto search for bioRxiv papers, since the bioRxiv API has no title search endpoint — Crossref indexes all bioRxiv DOIs. Hits return a slim triage shape `{doi, title, first_author, year}` (parallel to `search_arxiv`); agents chain to `get_paper_metadata(doi)` for the full record.
- **bioRxiv → journal chaining**: When a bioRxiv/medRxiv paper has been formally published, `get_paper_metadata(biorxiv_doi)` returns a `published_doi` field containing the journal DOI. Calling `get_paper_metadata(published_doi)` routes through the OpenAlex branch of the dispatcher and yields the published version's full metadata; Crossref tools give reference lists for the published version.
- **Manual import deduplication**: `import_paper(file_path, identifier)` auto-detects the identifier type and stores the file in the matching provider's cache namespace. For example, `import_paper("paper.pdf", "2301.00001")` writes to `.cache/arxiv/pdfs/`, and `import_paper("paper.pdf", "10.1101/2024.01.01.573838")` writes to `.cache/biorxiv/pdfs/`. This means a subsequent `download_pdf("2301.00001")` will find the cached PDF — no duplicate downloads or conversions. The unified pipeline tools (`convert_paper`, `get_paper_sections`, `get_paper_section`) also route to the correct namespace automatically. PDFs are validated by their `%PDF-` magic bytes (rejects mis-extension files before they reach the converter); markdown is read as UTF-8 with a clean error on decode failure. The MCP-layer response slims the markdown branch to `section_count` only — the agent calls `get_paper_sections` if it wants the full index.
- Manual imports intentionally have no BibTeX generation — the manual pipeline has no structured metadata. When the identifier is a DOI, chain into `get_paper_bibtex` (which dispatches to OpenAlex for arbitrary DOIs) for BibTeX instead.

## Adding a New OpenAlex Entity

1. Add `_normalize_*` and `_canonical_*` functions in `openalex.py`
2. Add an async `get_*` function that checks cache, fetches, and stores
3. Add focused tool(s) in `server.py` that extract lean slices from the cached object
4. Add unit tests for normalization logic in `tests/test_openalex.py`

## Adding a New API Provider

1. Create a new module (e.g., `arxiv.py`) that imports `cache` and `config`
2. Use a distinct cache namespace (e.g., `cache.get("arxiv", "papers", key)`)
3. Add env vars to `.env.example` and load them via `config.get()`
4. Add tools in `server.py` that call the new module

## OpenAlex API Limits

- **Singleton lookups** (get by ID/DOI/ORCID): Free, unlimited
- **Search**: 1,000 calls/day — not currently used
- **List+filter**: 10,000 calls/day — not currently used
- **Content download**: 100/day — not currently used

## arXiv API Limits

- **Rate limit**: Max 1 request every 3 seconds, single connection. Enforced by `asyncio.Lock` + `time.monotonic()` in `arxiv.py`.
- **No authentication required** — no API key, no email, nothing in `.env`.
- **Search cap**: `max_results` capped at 50 per call in the MCP tool layer to keep responses lean.

## bioRxiv/medRxiv API Limits

- **Rate limit**: No documented limits. Enforced conservatively at ~2 req/sec (500ms gap) in `biorxiv.py`.
- **No authentication required** — no API key, no email, nothing in `.env`.
- **DOI prefix** `10.1101/` identifies all bioRxiv and medRxiv papers.
- **Multi-version responses**: The `/details` endpoint returns all versions; code selects the latest automatically.
- **medRxiv fallback**: If a DOI isn't found on bioRxiv, the code automatically tries medRxiv.

## Crossref API Limits

- **Polite pool** (with `CROSSREF_MAILTO`): 10 req/sec single records, 3 req/sec search/query, 3 concurrent. Enforced conservatively at ~10 req/sec (100ms gap) in `crossref.py`.
- **Public pool** (no mailto): 5 req/sec single records, 1 req/sec search/query, 1 concurrent.
- **No API key required** — just `mailto` in `User-Agent` for polite pool access.
- **Search**: Uses `query.bibliographic` parameter on `/works` endpoint. Results not cached (ad-hoc queries). Capped at 20 rows per request.

## OpenCitations API Limits

- **Rate limit**: 180 req/min per IP. Enforced at ~3 req/sec (334ms gap) in `opencitations.py`.
- **No authentication required** — no API key, no email, nothing in `.env`.

## Wikipedia API Limits

- **Rate limit**: 1,000 req/hour for identified clients (with `User-Agent`). Enforced conservatively at ~1 req/sec (1,000ms gap) in `wikipedia.py`.
- **No authentication required** — just a `User-Agent` header with `mailto` (from `WIKIPEDIA_MAILTO` env var). Requests without a `User-Agent` may be blocked.
- **Page summaries are cached** under `wikipedia/summaries` to avoid redundant lookups.

## ACL Anthology

- **No API** — PDFs are served directly at `https://aclanthology.org/{anthology_id}.pdf`. No authentication, no rate limits documented.
- **DOI prefix** `10.18653/v1/` identifies ACL Anthology papers. The Anthology ID is the DOI suffix (e.g., `10.18653/v1/2023.acl-long.1` → `2023.acl-long.1`).
- **Coverage**: All ACL-affiliated venues — ACL, EMNLP, NAACL, EACL, AACL, CoNLL, TACL, CL journal, *SEM, Findings, workshops.

## APIs NOT to Use

- **Semantic Scholar** — Do not suggest or integrate. Their API keys are not granted to individuals. The shared global pool is unreliable and practically unusable. Not a viable option.
- **Google Scholar** — No official API exists. Scraping is fragile and against ToS. Do not suggest.

## Future Possibilities

- **OpenReview** — Has an API (v1: `api.openreview.net`, v2: `api2.openreview.net`) that could provide venue/decision metadata, review scores, and forum data for ML/AI conference papers. However, after a November 2025 security incident (reviewer identity leak), all endpoints now return 403 without authentication. Would require storing `OPENREVIEW_USERNAME`/`OPENREVIEW_PASSWORD` in `.env` and managing token refresh. Revisit if they reopen public access or if the auth overhead becomes worthwhile. We already have 5+ papers with OpenReview forum IDs (e.g., `openreview_n8hGHUfZ3Sy`).
