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
- **`openalex.py`** — Thin async client for OpenAlex singleton endpoints (`/works/{id}`, `/authors/{id}`). Handles ID normalization (DOI formats, OpenAlex URLs, ORCIDs) and cache read/write. Each entity type has a `_normalize_*` and `_canonical_*` pair for API path formatting and cache keying respectively.
- **`arxiv.py`** — Thin async client for arXiv's Atom API (`export.arxiv.org/api/query`). Handles ID normalization (bare IDs, URLs, version suffixes) and XML→dict parsing. Enforces arXiv's rate limit (1 request per 3 seconds, single connection) via an `asyncio.Lock` + monotonic timer. Cache namespace: `arxiv/papers`. No API key or env vars required.
- **`biorxiv.py`** — Thin async client for the bioRxiv/medRxiv API (`api.biorxiv.org`). Handles DOI normalization (bare DOIs, URLs, site content URLs with version suffixes). Tries bioRxiv first, falls back to medRxiv. Selects the latest version from multi-version responses. Parses semicolon-separated author strings into structured dicts. Builds PDF URLs from DOI + version + server (biorxiv.org vs medrxiv.org). Rate-limited to ~2 req/sec (500ms gap) as a courtesy (no documented limit). Cache namespace: `biorxiv/papers`. The `published_doi` field links to the journal DOI when available, enabling chaining into Crossref/OpenAlex. No auth required.
- **`manual.py`** — Manual PDF/markdown import for local files and arbitrary URLs. **Provider-aware routing**: `_resolve_target()` detects the identifier type (arXiv ID, bioRxiv DOI, ACL DOI) and stores PDFs/markdown directly in that provider's cache namespace, so native pipeline tools find them with no duplicates. Unrecognised identifiers fall back to the `manual` namespace. Supports `~/` expansion for local paths, content-type validation for URL downloads (rejects HTML login pages). No API, no auth, no rate limits. When using a DOI as the identifier, enables chaining into Crossref/OpenAlex for metadata.
- **`wikipedia.py`** — Thin async client for the Wikipedia API. Uses MediaWiki OpenSearch (`/w/api.php?action=opensearch`) for title search and the Wikimedia REST API (`/api/rest_v1/page/summary/{title}`) for page summaries and existence verification. Detects disambiguation pages via the `type` field. Rate-limited to ~1 req/sec (1,000ms gap) per Wikimedia's reader tier guidance. Requires a `User-Agent` header (configured via `WIKIPEDIA_MAILTO` env var). Cache namespace: `wikipedia/summaries`. No auth required.
- **`papers.py`** — PDF-to-markdown conversion via MinerU and section-level access. `convert_pdf()` shells out to MinerU (expects `~/.venvs/mineru`), stores markdown under `.cache/<namespace>/markdown/`. `parse_sections()` splits by H2 headings with H3 previews. `get_section_content()` retrieves individual sections by index or title substring. Section indices cached under `.cache/<namespace>/sections/`.
- **`crossref.py`** — Thin async client for the Crossref REST API (`api.crossref.org/works/{doi}`). Handles DOI normalization and cache read/write. Enforces polite pool etiquette via `User-Agent` header with `mailto` (from `CROSSREF_MAILTO` env var). Rate-limited to ~10 req/sec (100ms gap) via `asyncio.Lock` + monotonic timer. Cache namespace: `crossref/works`. The full work object is cached; the tool layer slices out just the `reference` list with pagination.
- **`opencitations.py`** — Thin async client for the OpenCitations Index API v2 (`api.opencitations.net/index/v2`). Fetches outgoing references (`/references/doi:...`) and incoming citations (`/citations/doi:...`). Rate-limited to ~3 req/sec (334ms gap, 180 req/min) per OpenCitations policy. Parses space-delimited multi-ID strings (`omid:... doi:... openalex:... pmid:...`) into structured dicts via `_parse_ids()`. Cache namespaces: `opencitations/references`, `opencitations/citations`. No auth required.
- **`acl_anthology.py`** — PDF source for ACL Anthology papers. Resolves DOIs with the ACL prefix (`10.18653/v1/`) to Anthology IDs by stripping the prefix. Downloads camera-ready PDFs from `https://aclanthology.org/{id}.pdf`. No API, no auth, no rate limits — just direct PDF URLs. Cache namespace: `acl_anthology/pdfs`. Tools feed into the same `papers.py` pipeline as arXiv PDFs.
- **`bibtex.py`** — Generates BibTeX entries from raw OpenAlex work objects or arXiv paper dicts. Maps OpenAlex `type` to BibTeX entry types (`_TYPE_MAP`). Handles surname particles (`van`, `de la`, `von`, etc.) for both citation keys and author formatting. `generate_arxiv_bibtex()` produces `@misc` (preprint) or `@article` (published) with `eprint`/`archiveprefix`/`primaryclass` fields.
- **`server.py`** — FastMCP tool definitions. Each tool fetches the full cached object then returns only the relevant slice. Tools use `Annotated` types (`DOI`, `AUTHOR_ID`, `ARXIV_ID`) for parameter descriptions. Paper pipeline tools (`download_arxiv_pdf` → `convert_paper` → `get_paper_sections` → `get_paper_section`) provide section-level access to full paper content. ACL Anthology pipeline (`download_acl_pdf` → `convert_acl_paper` → `get_acl_paper_sections` → `get_acl_paper_section`) provides the same for ACL venue papers via DOI. bioRxiv/medRxiv tools (`get_biorxiv_paper_metadata`, `get_biorxiv_paper_authors`, `get_biorxiv_paper_abstract`) provide metadata access, plus a PDF pipeline (`download_biorxiv_pdf` → `convert_biorxiv_paper` → `get_biorxiv_paper_sections` → `get_biorxiv_paper_section`). Manual PDF tools (`import_pdf`, `download_pdf_url`, `import_markdown`) accept local files (e.g. from Zotero), arbitrary URLs, or pre-converted markdown with a user-supplied identifier (arXiv ID, DOI, or freeform). **Provider-aware routing** stores the file in the correct provider's cache namespace (arXiv, bioRxiv, ACL Anthology) so that native pipeline tools find it — no duplicates. Unrecognised identifiers fall back to the `manual` namespace with its own convert → sections pipeline (`convert_manual_paper` → `get_manual_paper_sections` → `get_manual_paper_section`). Wikipedia tools (`search_wikipedia`, `get_wikipedia_summary`, `check_wikipedia_page`) support cross-referencing workflows by searching for articles, fetching summaries, and verifying page existence (detecting disambiguation pages). Crossref and OpenCitations tools use a count + paginated pattern so agents can check the size before fetching pages.

**Key design decisions:**
- Tool responses are intentionally small — an LLM agent should not receive the full OpenAlex response. Each tool returns only what's needed for its purpose.
- All tools for a given DOI or arXiv ID share one cached API response. Multiple tool calls = one API hit.
- OpenAlex singleton lookups (works, authors) are free and unlimited. Search/filter endpoints have daily limits and are not currently implemented.
- arXiv lookups are free but rate-limited (3-second gap enforced in code). Search is also supported with a 50-result cap per call.
- The `get_paper_authors` tool includes `openalex_id` per author so agents can chain into `get_author_profile`/`get_author_affiliations`.
- Crossref and OpenCitations reference/citation tools follow a count-then-page pattern: agents call the `_count` tool first to see the total, then page through results with `page` and `page_size` parameters. This prevents token blowouts on highly-cited papers.
- Crossref provides structured reference metadata (author, title, year, journal, DOI). OpenCitations provides DOI-to-DOI links with cross-referenced IDs (OMID, OpenAlex, PMID) and self-citation flags. OpenCitations may have references Crossref lacks (aggregates from PubMed, DataCite, OpenAIRE, JaLC).
- `search_crossref_by_title` enables DOI discovery by bibliographic query — useful when you only have a title or arXiv ID and need the published DOI (e.g., to find the ACL Anthology DOI for a paper known only by its arXiv ID). Year filtering is optional but note that Crossref publication dates may differ from arXiv preprint dates. This also serves as the de facto search for bioRxiv papers, since the bioRxiv API has no title search endpoint — Crossref indexes all bioRxiv DOIs.
- **bioRxiv → journal chaining**: When a bioRxiv/medRxiv paper has been formally published, `get_biorxiv_paper_metadata` returns a `published_doi` field containing the journal DOI. Use this DOI with OpenAlex tools (`get_paper_metadata`, `get_paper_authors`, etc.) or Crossref tools to access the published version's full metadata, citation counts, and reference lists.
- **Manual import deduplication**: `import_pdf`, `download_pdf_url`, and `import_markdown` auto-detect the identifier type and store in the matching provider's cache namespace. For example, `import_pdf("paper.pdf", "2301.00001")` writes to `.cache/arxiv/pdfs/`, and `import_pdf("paper.pdf", "10.1101/2024.01.01.573838")` writes to `.cache/biorxiv/pdfs/`. This means a subsequent `download_arxiv_pdf("2301.00001")` will find the cached PDF — no duplicate downloads or conversions. The `convert_manual_paper` / `get_manual_paper_sections` / `get_manual_paper_section` tools also route to the correct namespace, so you can use either the manual tools or the native provider tools interchangeably after import.
- Manual PDF imports intentionally have no BibTeX generation — the manual pipeline has no structured metadata. When the identifier is a DOI, chain into `get_paper_bibtex` (OpenAlex) for BibTeX instead.

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
