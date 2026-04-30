# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A FastMCP-based MCP server that wraps the OpenAlex, arXiv, bioRxiv/medRxiv, Crossref, OpenCitations, ACL Anthology, and Wikipedia APIs to provide lean, focused tools for LLM agents working with academic papers. Designed for verifying paper metadata, authors, institutions, generating BibTeX citations, and exploring reference/citation graphs — primarily in support of a Hugo-based academic notes/blog workflow.

## Commands

```bash
uv sync                                                  # Install dependencies
uv run pytest -v                                         # Run all tests
uv run pytest tests/test_bibtex.py -v                    # Run one test file
uv run pytest tests/test_bibtex.py::TestGenerateKey -v   # Run one test class
uv run pytest -k "test_particle" -v                      # Run tests matching a pattern
uv run python -m academic_tools_mcp.server               # Run the MCP server
```

## Architecture

**Layered design — tools never hit the API directly. Every API client uses every shared module.**

```
server.py (MCP tools, FastMCP lifespan)
  │
  ├── API clients (seven, all share the same shape)
  │     openalex.py / arxiv.py / biorxiv.py
  │     crossref.py / opencitations.py / wikipedia.py / acl_anthology.py
  │
  ├── PDF + content modules
  │     manual.py           (local file import + identifier dispatchers)
  │     papers.py           (PDF → markdown → sections, global convert lock,
  │                          find_in_markdown for in-paper search)
  │     bibtex.py           (BibTeX generation)
  │     cache_search.py     (BM25 keyword search across cached markdown)
  │     _pdf_download.py    (streaming download helper: chunked write,
  │                          atomic rename, MAX_PDF_BYTES cap)
  │
  └── Shared infrastructure (every API client routes through these)
        _http.py            (retry helper, error normalization, backpressure error)
        _clients.py         (pooled httpx.AsyncClient per provider)
        _singleflight.py    (request coalescing)
        cache.py            (positive + negative file cache, atomic writes, TTL eviction)
        _stats.py           (per-provider counters + DEBUG_REQUESTS logging)
        config.py           (env vars)
```

Per-module deep detail (atomic writes, throttle/backpressure semantics, single-flight slot rules, per-provider quirks, PDF subprocess gating, server tool shapes and error contracts) lives in `.claude/rules/` and loads only when Claude touches the matching file:

- `.claude/rules/infrastructure.md` — `cache.py`, `_http.py`, `_clients.py`, `_singleflight.py`, `_stats.py`, `config.py`
- `.claude/rules/providers.md` — all seven API clients
- `.claude/rules/pipeline.md` — `papers.py`, `manual.py`, `cache_search.py`
- `.claude/rules/server.md` — `server.py`, `bibtex.py`

## Cross-cutting design decisions

- **Uniform robustness primitives across providers.** Every API client (arxiv, openalex, biorxiv, crossref, opencitations, wikipedia, acl_anthology) has the same shape: persistent `httpx.AsyncClient`, two-stage gating (`_request_sem` of size `_MAX_CONCURRENT` caps simultaneous in-flight requests, `_request_lock` briefly serialises the inter-start gap update), 5-deep burst cap (`LocalBackpressureError` past that), single-flight by canonical identifier, one transparent retry on transient failure honouring `Retry-After`, negative caching on definitive 404s (default 24h TTL; arxiv/biorxiv override to 1h because preprint identifiers go live mid-session), positive cache TTL eviction, `_stats` counters.
- **Per-provider concurrency caps**, not a single global serial lock. `_MAX_CONCURRENT` per module: arxiv=1 (single-connection rule), openalex=4 and acl_anthology=4, crossref=3 (polite-pool concurrency budget), biorxiv/opencitations/wikipedia=2. Multiple GETs run in flight up to the cap; the gap-lock just enforces inter-start spacing. Reference-graph traversals are dramatically faster than the previous serialise-everything model. The PDF-downloading providers (arxiv, biorxiv, acl_anthology) expose a `_request_slot` async context manager so streaming downloads can hold the slot open for the lifetime of the stream — preventing fan-out from exceeding documented limits while slow streams flush.
- **Streaming PDF downloads.** `_pdf_download.stream_to_file` writes chunks (64 KiB) to a sibling temp file and atomic-renames into place — peak memory stays at one chunk, not the whole PDF, and a crash mid-download cannot leave a half-written canonical file. The `MAX_PDF_BYTES` env var caps total bytes (default 200 MB; set to `none`/`off`/`disabled`/`0` to disable) so a misrouted URL can't fill the disk.
- **One paper tool per job, not one per provider.** The four core paper tools (`get_paper_metadata` / `_authors` / `_abstract` / `_bibtex`) take any identifier and dispatch internally via `manual._resolve_metadata_source()`. Responses tag `_source` and `_canonical_id` so agents can branch on provider-specific fields and reuse the canonical form. Dispatch is by identifier shape, not by which provider has more data — `get_paper_metadata("2301.00001")` returns arXiv's native response even if the paper is also in OpenAlex. Agents wanting OpenAlex-specific data (topics, citations, venue) call dedicated OpenAlex-only tools with the paper's DOI.
- **Batch metadata.** `get_papers_metadata(identifiers: list[str])` collapses N parallel singletons into ⌈N/50⌉ HTTP calls for OpenAlex DOIs (`/works?filter=doi:...|...`) plus concurrent fan-out for arXiv / bioRxiv. Designed for reference-graph enrichment where N is 30–200. Each batch hit warms the singleton cache so a follow-up `get_paper_metadata` is free.
- **Tool responses are intentionally small.** Each tool fetches the full cached object then returns only the relevant slice — an LLM agent should not receive the full OpenAlex response.
- **Single shared cache across tools.** All tools for a given DOI or arXiv ID share one cached API response. Multiple tool calls = one API hit. Concurrent same-key callers are coalesced by single-flight to one outbound fetch.
- **`force_refresh=True`** on the four unified paper tools and the first three pipeline tools (`download_pdf`, `convert_paper`, `get_paper_sections`) drops cache entries via `cache.invalidate(...)` and re-fetches/re-runs. Stage-specific semantics for the pipeline tools — see `.claude/rules/server.md`. **`download_pdf(force_refresh=True)` cascades**: when the PDF is actually re-downloaded (`cached=False`), the cached markdown + section index for that paper are dropped automatically so the next `convert_paper` picks up the new bytes.
- **Manual import is deduplicated by provider routing.** `import_paper(file, identifier)` detects identifier type and stores under the matching provider's namespace, so a subsequent `download_pdf(identifier)` finds the cached PDF — no duplicate downloads or conversions.
- **bioRxiv → journal chaining.** `get_paper_metadata(biorxiv_doi, follow_published=True)` auto-chains to OpenAlex when `published_doi` is set; falls back to the preprint record if OpenAlex misses.
- **In-paper search.** `find_in_paper(identifier, query)` scans a converted paper's markdown for substring (or whole-word) matches and returns `[{section, section_index, char_offset, snippet}, ...]`. Char offsets align with `get_paper_section`'s stripped section text so an agent can chain straight to the surrounding context. Pairs with `search_cached_papers` (BM25 across the corpus): "which paper mentioned X?" + "where in the paper does it say X?".

## Cache TTLs

Positive cache entries have per-provider TTLs (`_POSITIVE_TTL_SECONDS` in each module) so an agent re-reading a paper after a long session sees fresh data without a manual cache wipe.

| Provider         | Positive TTL | Negative TTL | Why |
|------------------|--------------|--------------|-----|
| arxiv            | 14d          | 1h           | New versions land under the same canonical key (version stripped); preprint IDs go live mid-session. |
| biorxiv          | 7d           | 1h           | `published_doi` appears asynchronously when preprint becomes journal article. |
| openalex/works   | 30d          | 24h          | Citation count and topic classifications drift; DOIs are stable. |
| openalex/authors | 30d          | 24h          | h_index / works_count drift on the same timescale. |
| crossref         | 30d          | 24h          | Reference list grows as publishers re-deposit metadata. |
| opencitations    | 7d           | 24h          | Citation graph (especially incoming) grows continuously. |
| wikipedia        | 30d          | 24h          | Articles change as edits are made. |

Eviction is mtime-based and self-healing — `cache.get(..., max_age_seconds=N)` unlinks an over-age entry and returns `None`. **`force_refresh=True`** drops both halves via `cache.invalidate(...)` and re-fetches.

## Observability

`_stats.py` collects per-provider counters (`cache_hits`, `cache_misses`, `negative_hits`, `http_calls`, `http_retries`, `backpressure_refusals`) plus a live `in_flight` sample. Counters are cumulative since process start (or last `_stats.reset()`). **Not exposed as an MCP tool** — operational data is for the operator, not the agent.

- **`DEBUG_REQUESTS=1`** (also `true` / `yes` / `on`) — logs each throttled GET to **stderr** as `[academic-tools] {provider} GET {url} (throttle wait Xs)`. MCP servers speak JSON-RPC on stdout, so anything written there would corrupt the protocol stream. Re-read every call so an operator can flip the flag without restarting.
- **`ENABLE_DEBUG_TOOLS=1`** — registers a `get_server_stats` MCP tool returning `_stats.snapshot()`. Read at module import time, so flipping it requires a server restart. **Off by default** — agents would otherwise see operational data and might branch on it. Use `ENABLE_DEBUG_TOOLS=1 uv run python -m academic_tools_mcp.server` when you want to inspect counters from inside Claude Code without dropping into a Python REPL.

## Adding a New OpenAlex Entity

1. Add `_normalize_*` and `_canonical_*` functions in `openalex.py`.
2. Add an async `get_*` function that checks cache, fetches, stores.
3. Add focused tool(s) in `server.py` that extract lean slices.
4. Add unit tests for normalization in `tests/test_openalex.py`.

## Adding a New API Provider

Mirror `arxiv.py` or `crossref.py` — they're the canonical examples. The shape (pooled client, `_throttled_get` + burst cap, `_single_flight`, cache → negative cache → fetch with re-checks inside the slot, 404 → negative cache) is documented in `.claude/rules/providers.md` and `.claude/rules/infrastructure.md`. After mirroring it:

1. Add the module name to `_reset_pooled_state` in `tests/conftest.py` and to `_PROVIDER_MODULES` in `_stats.py`.
2. Add env vars to `.env.example` and load via `config.get()`.
3. Add tools in `server.py`.
4. Tests covering normalization, parsing, backpressure, 404 negative-cache, and TTL eviction / `force_refresh` if relevant.

## APIs NOT to Use

- **Semantic Scholar** — API keys are not granted to individuals; the shared global pool is unreliable and practically unusable. Not viable.
- **Google Scholar** — no official API; scraping is fragile and against ToS.

## Future Possibilities

- **OpenReview** — has an API (`api.openreview.net` v1, `api2.openreview.net` v2) for venue/decision metadata, review scores, forum data on ML/AI conference papers. After the November 2025 security incident (reviewer identity leak), all endpoints now return 403 without authentication. Would require `OPENREVIEW_USERNAME`/`OPENREVIEW_PASSWORD` and token refresh management. Revisit if they reopen public access. We already have papers with OpenReview forum IDs (e.g. `openreview_n8hGHUfZ3Sy`).
