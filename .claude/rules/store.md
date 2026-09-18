---
paths:
  - "src/academic_tools_mcp/store/*.py"
---

# Cache and the cached-getter protocol

## cache.py

**`atomic.write_text` is the single write seam**, so patching it fakes a full disk
for both writers at once. `_unlink_quietly` needs no counter of its own: every path
reaching a failed unlink goes on to `put` in the same directory, which fails and
ticks `cache_write_failures`.

**Counters partition, they don't overlap** — and two things sit outside the series:
`openalex._fetch_chunk` books its own misses because it bypasses `cached_lookup`,
and PDF downloads are counted nowhere here.

**Cross-module surface — renames here break callers.** `cache_dir` and `CACHE_ROOT`
are public because the path builders live elsewhere. Anything reached from another
module gets a public name; a leading underscore here means genuinely internal.

Per-provider TTL policy lives with each provider; the operator-facing table is
`README.md` § Caching.

### `cached_lookup` — the shared protocol

**Three paths deliberately re-implement its ordering; a change here must be
mirrored in all of them.** `openalex.get_works_batch` and `arxiv.get_papers_batch`
open-code the per-id invalidate / positive / negative check and coalesce a whole
chunk; `streaming.cached_download` is the file-on-disk sibling. Anything else that
wants this ordering calls `cached_lookup`.

**Pass a tuple `sf_key` whenever one identifier has more than one sub-fetch** —
`("work", canonical)`, `("author", canonical)`. Each provider module owns *one*
`SingleFlight` shared by all of its keys, so an un-namespaced key collides across
entities, and a `fetch` awaiting another getter on the same instance self-deadlocks
unless its key differs. **This is the one home for that rule.**

**Known narrow race (unguarded, accepted).** Between a `force_refresh`'s
`invalidate` and its in-slot re-check the entry can be rewritten — by a concurrent
batch chunk under a different `sf_key`, or by a non-refresh leader the refresher
queued behind — and the refresher then serves it without fetching. A property of
the protocol, so it applies to every getter.

## stems.py

- **`urllib.parse.quote` is the sole authority on the output alphabet.** Don't
  reintroduce a hand-written keep-set beside it: the two drift, and `quote` passes
  the whole RFC 3986 unreserved set (`~` and `_` included).
- **A caller holding a *stem* off disk rather than an identifier takes
  `markdown_path_for_stem` / `sections_key_for_stem`.**

### Checksums

**A writer checksums the string it parsed, never the file it just wrote.** The two
are separated by a window another writer fits through, and an index stamped with
the *other* document's checksum matches disk forever — so `_reparse_sections_locked`
accepts it and never re-parses. This is what makes the entry correct by
construction rather than by lock discipline: a losing writer's entry simply
mismatches and self-heals.

The on-disk half of that comparison is a *test* oracle, `tests/helpers/checksums.py`,
**deliberately not importable from `src/`**. Re-adding it to `stems` so a writer
can reach it is the bug the paragraph above describes.
