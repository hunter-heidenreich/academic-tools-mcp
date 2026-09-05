---
paths:
  - "src/academic_tools_mcp/cache.py"
  - "src/academic_tools_mcp/_singleflight.py"
---

# Cache and the cached-getter protocol

## cache.py

Generic file-based JSON cache under `.cache/<namespace>/<entity>/`, keyed by SHA-256 of the identifier. Provider-agnostic — a new provider is a new namespace.

**Atomicity, not durability.** Canonical writes land in a sibling mkstemp temp and `os.replace` into place, so a reader never sees a torn entry; `fsync` is deliberately skipped, which is survivable only because every read self-heals — corrupt JSON, `OSError`, `UnicodeError` and non-dict payloads are caught, the bad file unlinked, `None` returned. A new read path must hold up its half of that bargain. Reads and writes are explicitly UTF-8 so non-ASCII records survive an `LC_ALL=C` container instead of failing to decode and self-deleting.

**`put` / `put_negative` absorb `OSError` and return a bool.** A `fetch` closure must never treat `False` as a failure — the network response is already paid for, and serving it uncached is the correct outcome.

**`_CACHE_ROOT` is bound at import** (`_resolve_cache_root()` at module scope; `CACHE_DIR` relocates it for installed-wheel / read-only-tree deployments). `tests/conftest.py` monkeypatches that single attribute, so any module needing the test redirect reads `cache._CACHE_ROOT` at call time (`cache_search`, `papers.migrate_legacy_stems`) rather than capturing it at import. Don't hide it behind a function without updating conftest.

**Cross-module surface — renames here break callers.** `cache_dir` is public for the PDF path builders (`providers/*.download_pdf`, `manual`, `papers`); `invalidate` is called from `papers`, `manual`, `tools/pipeline`, `_pdf_download` and `openalex`; `gc_orphan_tmp_files` from the FastMCP lifespan in `_app`; and two leading-underscore names are cross-module on purpose — `_atomic_write_text` (`papers` markdown) and `_atomic_copy` (`manual.import_paper`, its only caller; downloaded PDFs never touch it, since `_pdf_download.stream_to_file` owns its own temp-and-rename).

**Lifetime**

- **Positive TTL eviction** — `get(..., max_age_seconds=N)` unlinks entries older than N seconds (by mtime) and returns `None`. Entries never re-read with a `max_age_seconds` persist indefinitely; the cache has no global size bound and operators prune `.cache/` by hand.
- **Negative cache** (`get_negative` / `put_negative`) lives in a sibling `_neg/` subdirectory under each entity; a provider overrides the default via its own `_NEG_TTL_SECONDS`, and the per-provider policy lives in `.claude/rules/providers.md`.
- **`invalidate(namespace, entity, identifier)`** drops both halves at once — used by `force_refresh=True`.
- **Orphan `.tmp` sweep** — `gc_orphan_tmp_files()` unlinks `*.tmp` files older than `_ORPHAN_TMP_AGE_SECONDS`, called from lifespan startup. It never touches files newer than the cutoff, so it cannot race a live writer. It bounds *leakage*, not cache size — it evicts no entries.

### `cached_lookup` — the shared cached-getter protocol

The one home for the force_refresh → check → single-flight → in-slot re-check → `fetch` ordering, routed through by every provider getter: `arxiv.get_paper`, `openalex.get_work`/`get_author`, `crossref.get_work`, `biorxiv.get_paper`, `opencitations._fetch_direction`, `wikipedia.get_summary`. (`acl_anthology` has no metadata getter — it is PDF-only and routes through `_pdf_download.cached_download`.)

`fetch` is an async closure that owns its own caching — it decides whether to `put` (positive), `put_negative` (with whatever TTL), or cache nothing (transient parse errors); it is only called on a genuine miss. Provider quirks (bioRxiv's medRxiv fallback, arxiv's three not-found shapes, the explicit 404 branch) all live in that closure. Pass a tuple `sf_key` to keep distinct sub-fetches for one id apart (`("work", canonical)`, `("references", canonical)`). **Each caller receives an independent `copy.deepcopy` of the result** so in-batch single-flight followers, who share the leader's object, can't corrupt each other or the cached dict.

**Known narrow race:** a `force_refresh` in-slot re-check can be repopulated by a concurrent non-refresh caller — accepted, not fixed. It is a property of this protocol, so it applies to every getter above, not to any one provider.

## _singleflight.py

`SingleFlight.do(key, factory)` collapses N concurrent calls for the same key into one execution; followers `await` the same future and share the leader's outcome — success or failure, and the *same object*. The slot is dropped after resolution, so a failure is never cached.

Cancellation is the subtle part: a leader's cancellation must not propagate to followers (one takes over, bounded by `_MAX_TAKEOVERS`), and a follower's must not reach the leader — hence `asyncio.shield`. The `do` docstring carries the full argument.

Providers reach `do` through a protocol wrapper — `cache.cached_lookup` or its file-on-disk sibling `_pdf_download.cached_download` — and the wrapper is what deep-copies per caller. One deliberate direct caller: `openalex._fetch_chunk`, which skips the copy on purpose (see `.claude/rules/providers.md`). Don't assume every `do` result is deep-copied downstream.
