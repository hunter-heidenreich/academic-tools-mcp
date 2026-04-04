# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A FastMCP-based MCP server that wraps the OpenAlex API to provide lean, focused tools for LLM agents working with academic papers. Designed for verifying paper metadata, authors, institutions, and generating BibTeX citations — primarily in support of a Hugo-based academic notes/blog workflow.

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
                       ↘ bibtex.py (BibTeX generation)
```

- **`cache.py`** — Generic file-based JSON cache under `.cache/<provider>/<entity>/`. Namespaced so it scales to future providers (arxiv, etc.). Files are SHA-256 hashed by identifier. No expiration.
- **`config.py`** — Loads `.env` from project root. All API credentials come from environment variables, never from tool parameters.
- **`openalex.py`** — Thin async client for OpenAlex singleton endpoints (`/works/{id}`, `/authors/{id}`). Handles ID normalization (DOI formats, OpenAlex URLs, ORCIDs) and cache read/write. Each entity type has a `_normalize_*` and `_canonical_*` pair for API path formatting and cache keying respectively.
- **`bibtex.py`** — Generates BibTeX entries from raw OpenAlex work objects. Maps OpenAlex `type` to BibTeX entry types (`_TYPE_MAP`). Handles surname particles (`van`, `de la`, `von`, etc.) for both citation keys and author formatting.
- **`server.py`** — FastMCP tool definitions. Each tool fetches the full cached object then returns only the relevant slice. Tools use `Annotated` types (`DOI`, `AUTHOR_ID`) for parameter descriptions.

**Key design decisions:**
- Tool responses are intentionally small — an LLM agent should not receive the full OpenAlex response. Each tool returns only what's needed for its purpose.
- All tools for a given DOI share one cached API response. Multiple tool calls = one API hit.
- OpenAlex singleton lookups (works, authors) are free and unlimited. Search/filter endpoints have daily limits and are not currently implemented.
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
