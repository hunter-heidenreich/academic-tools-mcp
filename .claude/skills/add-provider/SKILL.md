---
name: add-provider
description: Add a new API provider client or a new OpenAlex entity to academic-tools-mcp. Use when adding support for another academic API, or another OpenAlex entity type (works/authors/…) and its tools.
---

# Adding a new provider or OpenAlex entity

## Adding a new OpenAlex entity

1. Add `_normalize_*` and `_canonical_*` functions in `providers/openalex.py`.
2. Add an async `get_*` function that checks cache, fetches, stores.
3. Add focused tool(s) in the matching `tools/*.py` module (OpenAlex metadata → `tools/paper.py`) that extract lean slices; shared param types live in `app.py`.
4. Add unit tests for normalization in `tests/providers/test_openalex.py`.

## Adding a new API provider

Mirror `providers/biorxiv.py` — it is the fullest instance of the shape (both throttle wrappers, a `cached_lookup` getter, a PDF path). Read `providers/crossref.py` as a counter-example rather than a template: its rate constants are `_resolve_policy()` output, not literals. The shape (pooled client, `_throttled_get` + burst cap, `_single_flight`, cache → negative cache → fetch with re-checks inside the slot, 404 → negative cache) is documented in `.claude/rules/providers.md` and `.claude/rules/net.md` and `.claude/rules/store.md`. New clients live under `providers/` and import shared infra one level up, by package (`from ..net import clients, http`, `from ..store import cache`, `from ..util import config, doinorm, useragent`). After mirroring it:

1. Nothing to register: `stats.throttles()` and the conftest reset fixture both discover the module's `_throttle` instance by scanning imported modules for a `Throttle`-typed attribute — the name is conventional, the discovery is by type. Declare `NAMESPACE` **and `LABEL`** at module level and pass both to the `Throttle` (`namespace=NAMESPACE, label=LABEL`) — `tests/net/test_stats.py` asserts each matches, because counters are filed under the namespace and the label is the name that reaches the agent. **`NAMESPACE` is the on-disk `.cache/` directory name, so it is a data migration, not a rename** — it may differ from the module name and must not be changed to follow one (`providers/acl.py` keeps `NAMESPACE = "acl_anthology"`). Use `LABEL` at every other site naming the provider (`http.error_dict`, `http.parse_error_dict`, `stream_to_file(provider_label=)`); an AST scan in `tests/test_politeness.py` fails on a string literal at any of them.
2. Add env vars to `.env.example` and load via `config.get()`.
3. If the provider serves PDFs, add a `_Route` row to `manual._ROUTES` (`claims`, `NAMESPACE`, `canonical_key`, `pdf_path`) — position it before the generic-DOI fallback — and, if it also serves metadata, an entry in `manual._METADATA_SOURCE_BY_NAMESPACE`. Without the row nothing routes to the new namespace. If its ids need a slash restored from a stem, `corpus._restore_slashes` needs a clause too, or corpus hits won't chain back.
4. Add tools in the matching `tools/*.py` module.
5. Tests in `tests/providers/test_<name>.py` (and `tests/providers/test_<name>_properties.py` for anything hypothesis is stronger at), covering normalization, parsing, backpressure, 404 negative-cache, and TTL eviction / `force_refresh` if relevant. `tests/` mirrors `src/`; shared fakes live in `tests/helpers/` and are imported absolutely.
