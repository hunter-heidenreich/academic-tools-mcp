# CLAUDE.md

## What This Is

A FastMCP-based MCP server wrapping academic APIs (see `README.md` for the
provider table). It exists primarily to support an academic notes/blog workflow:
verifying paper metadata, authors, and institutions, generating BibTeX, and
exploring reference/citation graphs.

## Commands

```bash
uv run pytest -v                                                 # Run the test suite
uv run python -m academic_tools_mcp.server                       # Run the MCP server
ENABLE_DEBUG_TOOLS=1 uv run python -m academic_tools_mcp.server  # + the get_server_stats tool
```

## Changelog & versioning

**Before opening a PR, add a bullet to `## [Unreleased]` in
[`CHANGELOG.md`](./CHANGELOG.md)** (Keep a Changelog format) under the right
`Added` / `Changed` / `Fixed` / `Removed` heading, referencing the PR number
(e.g. `([#12])`) with a matching link definition at the bottom of the file. Skip
only for changes with no user-facing effect (pure refactors, internal docs,
test-only edits).

Releases are cut deliberately, not per-merge. Calendar versioning: rename
`[Unreleased]` to `## [YYYY.MM.DD] — YYYY-MM-DD`, bump `version` in
`pyproject.toml` to match (PEP 440 drops leading zeros: tag `v2026.05.29` ↔
version `2026.5.29`), and tag the commit `vYYYY.MM.DD`.

## Where the detail lives

**Design detail lives in `.claude/rules/`, each file auto-loading from its own
`paths:` frontmatter when you read a matching file.** You never need to go looking
for one: open the code and its rules arrive with it.

**Where a new file goes, and what to call it, is machine-checked.**
`tests/test_layering.py` owns the layer order in its `_LAYERS` table and asserts it
by AST; `tests/test_rules_layer.py` does the same for the rules layer's own
structure. Both fail CI rather than review.

Adding a new API provider or a new OpenAlex entity: use the `add-provider` skill.

## Cross-cutting design decisions

- **One paper tool per job, not one per provider.** `get_paper_metadata` /
  `_authors` / `_abstract` / `_bibtex` take any identifier and dispatch on
  identifier *shape*, not on which provider has richer data —
  `get_paper_metadata("2301.00001")` returns arXiv's native response even though
  OpenAlex also has the paper. Provider-specific data (OpenAlex topics,
  citations, venue) gets dedicated OpenAlex-only tools.
- **Operational stats are not agent-facing.** `net/stats.py` counters exist for
  the operator; `get_server_stats` registers only under `ENABLE_DEBUG_TOOLS=1`,
  off by default, so an agent can't see or branch on cache/throttle state.

## Upstream metadata caveats

**The entries in `README.md` § Known upstream limitations are properties of the
upstream providers, not defects in this tool — don't "fix" them in code.**
Operators correct them by hand, under a "published version is authoritative" rule.
Read that section before treating any of them as a bug.

## APIs NOT to Use

- **Semantic Scholar** — API keys are not granted to individuals; the shared
  global pool is unreliable and practically unusable. Not viable.
- **Google Scholar** — no official API; scraping is fragile and against ToS.
