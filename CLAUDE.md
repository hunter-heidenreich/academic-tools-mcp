# CLAUDE.md

## What This Is

A FastMCP-based MCP server wrapping academic APIs (see `README.md` for the provider table). It exists primarily to support an academic notes/blog workflow: verifying paper metadata, authors, and institutions, generating BibTeX, and exploring reference/citation graphs.

## Commands

```bash
uv run pytest -v                                                 # Run the test suite
uv run python -m academic_tools_mcp.server                       # Run the MCP server
ENABLE_DEBUG_TOOLS=1 uv run python -m academic_tools_mcp.server  # + the get_server_stats tool
```

## Code style & the format-on-edit hook

`ruff` (format + lint) and `mypy` enforce style — config in `pyproject.toml`. A `PostToolUse` hook (`.claude/hooks/ruff-format.sh`, wired in `.claude/settings.json`) runs `ruff format` + `ruff check --fix` on every `.py` file in this repo the moment it's edited.

The autofix passes **`--unfixable F401`**, so an import added just before its first use survives the next edit; it is still *reported*, so CI fails on a genuinely unused import. Unfixable findings (F821, most of B) never reach the session — mid-edit files reference not-yet-written names — so CI gates those.

## Changelog & versioning

**Before opening a PR, add a bullet to `## [Unreleased]` in [`CHANGELOG.md`](./CHANGELOG.md)** (Keep a Changelog format) under the right `Added` / `Changed` / `Fixed` / `Removed` heading, referencing the PR number (e.g. `([#12])`) with a matching link definition at the bottom of the file. Skip only for changes with no user-facing effect (pure refactors, internal docs, test-only edits).

Releases are cut deliberately, not per-merge. Calendar versioning: rename `[Unreleased]` to `## [YYYY.MM.DD] — YYYY-MM-DD`, bump `version` in `pyproject.toml` to match (PEP 440 drops leading zeros: tag `v2026.05.29` ↔ version `2026.5.29`), and tag the commit `vYYYY.MM.DD`.

## Where the detail lives

**Layered design — tools never hit the API directly. Every API client uses every shared module.** Deep per-module detail (atomic writes, throttle/backpressure, single-flight slots, provider quirks, PDF subprocess gating, tool shapes and error contracts) lives in `.claude/rules/*.md`, each auto-loading from its own `paths:` frontmatter when you touch a matching file. `python-design.md` covers every file under `src/`. The PDF pipeline is a package — `papers/{sections,index,convert}.py` over the `_stems.py` naming layer; `pipeline.md` § Layout says which seam is which.

Adding a new API provider or a new OpenAlex entity: use the `add-provider` skill.

## Cross-cutting design decisions

- **One paper tool per job, not one per provider.** `get_paper_metadata` / `_authors` / `_abstract` / `_bibtex` take any identifier and dispatch on identifier *shape*, not on which provider has richer data — `get_paper_metadata("2301.00001")` returns arXiv's native response even though OpenAlex also has the paper. Provider-specific data (OpenAlex topics, citations, venue) gets dedicated OpenAlex-only tools.
- **Tool responses are intentionally small.** Each tool fetches the full cached object and returns only the relevant slice — an LLM agent should not receive the full OpenAlex response.
- **Single shared cache across tools.** All tools for a given DOI or arXiv ID share one cached response: multiple tool calls = one API hit, and concurrent same-key callers coalesce via single-flight to one outbound fetch. Per-provider TTLs are tabulated in `README.md` § Caching; `force_refresh=True` drops both cache halves and re-fetches.
- **Manual import routes by provider namespace.** `import_paper` stores under the identifier's provider namespace, so a later `download_pdf(identifier)` hits the cached PDF instead of re-downloading.
- **Operational stats are not agent-facing.** `_stats.py` counters exist for the operator; `get_server_stats` registers only under `ENABLE_DEBUG_TOOLS=1`, off by default, so an agent can't see or branch on cache/throttle state.

## Upstream metadata caveats

Properties of the upstream providers, not defects in this tool — don't "fix" them in code. Operators correct them by hand, under a "published version is authoritative" rule. The user-facing copy is `README.md` § Known upstream limitations — keep both in sync.

- **Author diacritics dropped or mangled** by OpenAlex (`Alan Aspuru-Guzik` for `Alán Aspuru-Guzik`).
- **Current vs. paper-time institution.** OpenAlex reports an author's *present* affiliation, not their affiliation at publication time.
- **Preprint vs. published author sets diverge.** arXiv and the published DOI can list different authors for the same work. `follow_published=True` chains preprint → journal, but only once OpenAlex has indexed the journal version; otherwise the response carries `followed_published: false`. Batch `get_papers_metadata` doesn't support it — chain per-paper.

## APIs NOT to Use

- **Semantic Scholar** — API keys are not granted to individuals; the shared global pool is unreliable and practically unusable. Not viable.
- **Google Scholar** — no official API; scraping is fragile and against ToS.
