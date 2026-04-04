# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A FastMCP-based MCP server that wraps the OpenAlex and arXiv APIs to provide lean, focused tools for LLM agents working with academic papers. Designed for verifying paper metadata, authors, institutions, and generating BibTeX citations — primarily in support of a Hugo-based academic notes/blog workflow.

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
server.py (MCP tools) → openalex.py (API client) → cache.py (file cache)
                       → arxiv.py   (API client) ↗
                       → papers.py  (PDF → markdown → sections)
                       ↘ bibtex.py (BibTeX generation)
```

- **`cache.py`** — Generic file-based JSON cache under `.cache/<provider>/<entity>/`. Namespaced so it scales to future providers (arxiv, etc.). Files are SHA-256 hashed by identifier. No expiration.
- **`config.py`** — Loads `.env` from project root. All API credentials come from environment variables, never from tool parameters.
- **`openalex.py`** — Thin async client for OpenAlex singleton endpoints (`/works/{id}`, `/authors/{id}`). Handles ID normalization (DOI formats, OpenAlex URLs, ORCIDs) and cache read/write. Each entity type has a `_normalize_*` and `_canonical_*` pair for API path formatting and cache keying respectively.
- **`arxiv.py`** — Thin async client for arXiv's Atom API (`export.arxiv.org/api/query`). Handles ID normalization (bare IDs, URLs, version suffixes) and XML→dict parsing. Enforces arXiv's rate limit (1 request per 3 seconds, single connection) via an `asyncio.Lock` + monotonic timer. Cache namespace: `arxiv/papers`. No API key or env vars required.
- **`papers.py`** — PDF-to-markdown conversion via MinerU and section-level access. `convert_pdf()` shells out to MinerU (expects `~/.venvs/mineru`), stores markdown under `.cache/<namespace>/markdown/`. `parse_sections()` splits by H2 headings with H3 previews. `get_section_content()` retrieves individual sections by index or title substring. Section indices cached under `.cache/<namespace>/sections/`.
- **`bibtex.py`** — Generates BibTeX entries from raw OpenAlex work objects or arXiv paper dicts. Maps OpenAlex `type` to BibTeX entry types (`_TYPE_MAP`). Handles surname particles (`van`, `de la`, `von`, etc.) for both citation keys and author formatting. `generate_arxiv_bibtex()` produces `@misc` (preprint) or `@article` (published) with `eprint`/`archiveprefix`/`primaryclass` fields.
- **`server.py`** — FastMCP tool definitions. Each tool fetches the full cached object then returns only the relevant slice. Tools use `Annotated` types (`DOI`, `AUTHOR_ID`, `ARXIV_ID`) for parameter descriptions. Paper pipeline tools (`download_arxiv_pdf` → `convert_paper` → `get_paper_sections` → `get_paper_section`) provide section-level access to full paper content.

**Key design decisions:**
- Tool responses are intentionally small — an LLM agent should not receive the full OpenAlex response. Each tool returns only what's needed for its purpose.
- All tools for a given DOI or arXiv ID share one cached API response. Multiple tool calls = one API hit.
- OpenAlex singleton lookups (works, authors) are free and unlimited. Search/filter endpoints have daily limits and are not currently implemented.
- arXiv lookups are free but rate-limited (3-second gap enforced in code). Search is also supported with a 50-result cap per call.
- The `get_paper_authors` tool includes `openalex_id` per author so agents can chain into `get_author_profile`/`get_author_affiliations`.

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
