---
name: add-provider
description: Add a new API provider client or a new OpenAlex entity to academic-tools-mcp. Use when adding support for another academic API, or another OpenAlex entity type (works/authors/…) and its tools.
---

# Adding a new provider or OpenAlex entity

## Adding a new OpenAlex entity

1. Add `_normalize_*` and `_canonical_*` functions in `providers/openalex.py`.
2. Add an async `get_*` function that checks cache, fetches, stores.
3. Add focused tool(s) in the matching `tools/*.py` module (OpenAlex metadata →
   `tools/paper.py`) that extract lean slices; shared param types live in `_app.py`.
4. Add unit tests for normalization in `tests/test_openalex.py`.

## Adding a new API provider

Mirror `providers/arxiv.py` or `providers/crossref.py` — they're the canonical
examples. The shape (pooled client, `_throttled_get` + burst cap, `_single_flight`,
cache → negative cache → fetch with re-checks inside the slot, 404 → negative
cache) is documented in `.claude/rules/providers.md` and
`.claude/rules/infrastructure.md`. New clients live under `providers/` and import
shared infra one level up (`from .. import _http, cache, …`). After mirroring it:

1. Add the dotted module path (`providers.<name>`) to `_reset_pooled_state` in
   `tests/conftest.py` and to `_PROVIDER_MODULES` in `_stats.py`.
2. Add env vars to `.env.example` and load via `config.get()`.
3. Add tools in the matching `tools/*.py` module.
4. Tests covering normalization, parsing, backpressure, 404 negative-cache, and
   TTL eviction / `force_refresh` if relevant.
