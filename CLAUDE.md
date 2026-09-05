# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A FastMCP-based MCP server wrapping seven academic APIs (see `README.md` for the provider table). It exists primarily to support a Hugo-based academic notes/blog workflow: verifying paper metadata, authors, and institutions, generating BibTeX, and exploring reference/citation graphs.

## Commands

```bash
uv run pytest -v                             # Run the test suite
uv run python -m academic_tools_mcp.server   # Run the MCP server
```

## Code style & the format-on-edit hook

`ruff` (format + lint) and `mypy` enforce style — config in `pyproject.toml`. A
`PostToolUse` hook (`.claude/hooks/ruff-format.sh`, wired in `.claude/settings.json`)
runs `ruff format` + `ruff check --fix` on every `.py` file in this repo the moment
it's edited.

The autofix passes **`--unfixable F401`**, so an `import` added just before the code
that uses it survives the next edit instead of being stripped as momentarily unused.
It is still *reported*, so CI still fails on a genuinely unused import. Unfixable
findings (F821, most of B) are never fed back into the session — mid-sequence a file
legitimately references not-yet-written names, so CI is the gate for those.

## Changelog & versioning

This project keeps a [`CHANGELOG.md`](./CHANGELOG.md) in [Keep a Changelog](https://keepachangelog.com/) format and uses calendar versioning (`YYYY.MM.DD`, git tag `vYYYY.MM.DD`).

**Before opening a PR, add a bullet to the `## [Unreleased]` section of `CHANGELOG.md`** describing the user-facing change, under the appropriate `Added` / `Changed` / `Fixed` / `Removed` heading. Reference the PR number (e.g. `([#12])`) and add the matching link definition at the bottom of the file. Skip this only for changes with no user-facing effect (pure refactors, internal docs, test-only edits).

Releases are cut deliberately — not on every merge — by renaming `[Unreleased]` to the ship-date version (`## [YYYY.MM.DD] — YYYY-MM-DD`), bumping `version` in `pyproject.toml` to match (PEP 440 form drops leading zeros: tag `v2026.05.29` ↔ version `2026.5.29`), and tagging the release commit.

## Where the detail lives

**Layered design — tools never hit the API directly. Every API client uses every shared module.** Per-module deep detail (atomic writes, throttle/backpressure semantics, single-flight slot rules, per-provider quirks, PDF subprocess gating, server tool shapes and error contracts) lives in `.claude/rules/` and loads only when Claude touches the matching file:

- `.claude/rules/python-design.md` — layering and single-responsibility contracts; applies to **every** file under `src/`
- `.claude/rules/infrastructure.md` — the shared primitives: `cache.py`, `_http.py`, `_throttle.py`, `_clients.py`, `_singleflight.py`, `_stats.py`, `_pdf_download.py`, `config.py`, `_doi.py`, `_useragent.py`, `_textnorm.py`
- `.claude/rules/providers.md` — all seven API clients (`providers/*.py`)
- `.claude/rules/pipeline.md` — `papers.py`, `_fast_extract.py`, `manual.py`, `oa_download.py`, `cache_search.py`
- `.claude/rules/server.md` — `server.py`, `_app.py`, `tools/*.py`, `bibtex.py`

Adding a new API provider or a new OpenAlex entity: use the `add-provider` skill (`.claude/skills/add-provider/`).

## Cross-cutting design decisions

- **One paper tool per job, not one per provider.** The four core paper tools (`get_paper_metadata` / `_authors` / `_abstract` / `_bibtex`) take any identifier and dispatch internally via `manual.resolve_metadata_source()`. Responses tag `_source` and `_canonical_id` so agents can branch on provider-specific fields and reuse the canonical form. Dispatch is by identifier shape, not by which provider has more data — `get_paper_metadata("2301.00001")` returns arXiv's native response even if the paper is also in OpenAlex. Agents wanting OpenAlex-specific data (topics, citations, venue) call dedicated OpenAlex-only tools with the paper's DOI.
- **Tool responses are intentionally small.** Each tool fetches the full cached object then returns only the relevant slice — an LLM agent should not receive the full OpenAlex response.
- **Single shared cache across tools.** All tools for a given DOI or arXiv ID share one cached API response. Multiple tool calls = one API hit. Concurrent same-key callers are coalesced by single-flight to one outbound fetch.
- **Manual import is deduplicated by provider routing.** `import_paper(file, identifier)` detects identifier type and stores under the matching provider's namespace, so a subsequent `download_pdf(identifier)` finds the cached PDF — no duplicate downloads or conversions.
- **bioRxiv → journal chaining.** `get_paper_metadata(biorxiv_doi, follow_published=True)` auto-chains to OpenAlex when `published_doi` is set; falls back to the preprint record if OpenAlex misses.
- **In-paper search.** `find_in_paper(identifier, query)` scans a converted paper's markdown for substring (or whole-word) matches and returns `[{section, section_index, char_offset, snippet}, ...]`. Char offsets align with `get_paper_section`'s stripped section text so an agent can chain straight to the surrounding context. Pairs with `search_cached_papers` (BM25 across the corpus): "which paper mentioned X?" + "where in the paper does it say X?".

## Upstream metadata caveats

These surface through `get_paper_metadata` / `get_paper_authors` and are properties of the upstream providers, not defects in this tool. Operators correct them by hand under a "published version is authoritative" rule.

- **Author diacritics dropped or mangled** by OpenAlex (`Alan Aspuru-Guzik` for `Alán Aspuru-Guzik`).
- **Current vs. paper-time institution.** OpenAlex reports an author's *present* affiliation, not their affiliation at publication time.
- **Preprint vs. published author-count divergence.** arXiv and the published DOI can list different author sets for the same work. `follow_published=True` helps, but chains one direction only and only once OpenAlex has indexed the journal version — when it hasn't, the preprint response carries `followed_published: false` so a consumer can tell it is looking at preprint-era metadata. The batch `get_papers_metadata` does **not** support `follow_published`; chain explicitly per-paper.

## Cache TTLs

Per-provider `_POSITIVE_TTL_SECONDS` constants (grep `providers/`) so a long session sees fresh data without a manual cache wipe; eviction is mtime-based and self-healing. Negatives default to 24h, with **arxiv/biorxiv overriding to 1h** because preprint identifiers go live mid-session. **`force_refresh=True`** drops both halves via `cache.invalidate(...)` and re-fetches — the way to beat the 7-day OpenCitations TTL when the citation graph has grown. See `.claude/rules/infrastructure.md` and `.claude/rules/providers.md` for the per-provider rationale.

## Observability

`_stats.py` collects per-provider counters (`cache_hits`, `cache_misses`, `negative_hits`, `http_calls`, `http_retries`, `backpressure_refusals`, `cache_write_failures`) plus a live `in_flight` sample. Counters are cumulative since process start (or last `_stats.reset()`). **Not exposed as an MCP tool** — operational data is for the operator, not the agent.

- **`DEBUG_REQUESTS=1`** (also `true` / `yes` / `on`) — logs each throttled GET to **stderr** as `[academic-tools] {provider} GET {url} (throttle wait Xs)`. MCP servers speak JSON-RPC on stdout, so anything written there would corrupt the protocol stream. Re-read every call so an operator can flip the flag without restarting.
- **`ENABLE_DEBUG_TOOLS=1`** — registers a `get_server_stats` MCP tool returning `_stats.snapshot()`. Read at module import time, so flipping it requires a server restart. **Off by default** — agents would otherwise see operational data and might branch on it. Use `ENABLE_DEBUG_TOOLS=1 uv run python -m academic_tools_mcp.server` when you want to inspect counters from inside Claude Code without dropping into a Python REPL.

## APIs NOT to Use

- **Semantic Scholar** — API keys are not granted to individuals; the shared global pool is unreliable and practically unusable. Not viable.
- **Google Scholar** — no official API; scraping is fragile and against ToS.

## Future Possibilities

- **OpenReview** — has an API (`api.openreview.net` v1, `api2.openreview.net` v2) for venue/decision metadata, review scores, forum data on ML/AI conference papers. After the November 2025 security incident (reviewer identity leak), all endpoints now return 403 without authentication. Would require `OPENREVIEW_USERNAME`/`OPENREVIEW_PASSWORD` and token refresh management. Revisit if they reopen public access. We already have papers with OpenReview forum IDs (e.g. `openreview_n8hGHUfZ3Sy`).
