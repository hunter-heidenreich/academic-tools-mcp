---
name: add-provider
description: Add a new API provider client or a new OpenAlex entity to academic-tools-mcp. Use when adding support for another academic API, or another OpenAlex entity type (works/authors/…) and its tools.
---

# Adding a new provider or OpenAlex entity

## Adding a new OpenAlex entity

1. Add `_normalize_*` and `_canonical_*` functions in `providers/openalex.py`.
2. Add an async `get_*` function that checks cache, fetches, stores.
3. Add focused tool(s) in the matching `tools/*.py` module (OpenAlex metadata → `tools/paper.py`) that extract lean slices; shared param types live in `_app.py`.
4. Add unit tests for normalization in `tests/test_openalex.py`.

## Adding a new API provider

Mirror `providers/biorxiv.py` — it is the fullest instance of the shape (both throttle wrappers, a `cached_lookup` getter, a PDF path). Read `providers/crossref.py` as a counter-example rather than a template: its rate constants are `_resolve_policy()` output, not literals. The shape (pooled client, `_throttled_get` + burst cap, `_single_flight`, cache → negative cache → fetch with re-checks inside the slot, 404 → negative cache) is documented in `.claude/rules/providers.md` and `.claude/rules/http.md` and `.claude/rules/cache.md`. New clients live under `providers/` and import shared infra one level up (`from .. import _http, cache, …`). After mirroring it:

1. Nothing to register: `_stats.throttles()` and the conftest reset fixture both discover the module's `_throttle` by scanning imported modules. Give the `Throttle` the module's own `NAMESPACE` — a test asserts they match, because in-flight and cache counters are filed under it.
2. Add env vars to `.env.example` and load via `config.get()`.
3. If the provider serves PDFs, add a `_Route` row to `manual._ROUTES` (`claims`, `NAMESPACE`, `canonical_key`, `pdf_path`) — position it before the generic-DOI fallback — and, if it also serves metadata, an entry in `manual._METADATA_SOURCE_BY_NAMESPACE`. Without the row nothing routes to the new namespace. If its ids need a slash restored from a stem, `cache_search._restore_slashes` needs a clause too, or corpus hits won't chain back.
4. Add tools in the matching `tools/*.py` module.
5. Tests covering normalization, parsing, backpressure, 404 negative-cache, and TTL eviction / `force_refresh` if relevant.
