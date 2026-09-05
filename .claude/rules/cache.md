---
paths:
  - "src/academic_tools_mcp/cache.py"
  - "src/academic_tools_mcp/_singleflight.py"
---

# Cache and the cached-getter protocol

## cache.py

Generic file-based JSON cache under `.cache/<namespace>/<entity>/`, keyed by SHA-256 of the identifier. Provider-agnostic — a new provider is a new namespace.

**Atomicity, not durability.** Canonical writes land in a sibling `mkstemp` temp and `os.replace` into place; `fsync` is deliberately skipped, survivable only because every read self-heals — corrupt JSON, `OSError`, `UnicodeDecodeError` and non-dict payloads are caught, the bad file unlinked, `None` returned. **A new read path must hold up its half of that bargain**, and must name its encoding explicitly (UTF-8) so records survive an `LC_ALL=C` host.

**`put` / `put_negative` absorb `OSError` and return a bool.** A `fetch` closure must never treat `False` as a failure — the network response is already paid for, and serving it uncached is the correct outcome.

**`_CACHE_ROOT` is bound at import** (`_resolve_cache_root()` at module scope; `CACHE_DIR` relocates it for installed-wheel / read-only-tree deployments). `tests/conftest.py` monkeypatches that single attribute, so any module needing the test redirect reads `cache._CACHE_ROOT` at call time (`cache_search`, `papers.migrate_legacy_stems`) rather than capturing it at import. Don't hide it behind a function without updating conftest.

**Cross-module surface — renames here break callers.** `cache_dir` is public because the path builders live elsewhere: `providers/*.pdf_path`, `manual.pdf_path`, `papers.markdown_path`. Two leading-underscore names are cross-module on purpose — `_atomic_write_text` (`papers.store_markdown_and_index`) and `_atomic_copy` (`manual.import_local_pdf`, its only caller; downloaded PDFs never touch it, since `_pdf_download.stream_to_file` owns its own temp-and-rename).

**Lifetime**

- **Positive TTL eviction** — `get(..., max_age_seconds=N)` unlinks entries older than N seconds (by mtime) and returns `None`. Entries never re-read with a `max_age_seconds` persist indefinitely.
- **Negative cache** (`get_negative` / `put_negative`) lives in a sibling `_neg/` subdirectory under each entity; a provider overrides the default via its own `_NEG_TTL_SECONDS`, and the per-provider policy lives in `.claude/rules/providers.md`.
- **`invalidate(namespace, entity, identifier)`** drops both halves at once — used by `force_refresh=True`.
- **Orphan `.tmp` sweep** — `gc_orphan_tmp_files()` unlinks `*.tmp` files older than `_ORPHAN_TMP_AGE_SECONDS`, called from the FastMCP lifespan in `_app`. It never touches files newer than the cutoff, so it cannot race a live writer. It bounds *leakage*, not cache size — it evicts no entries.

### `cached_lookup` — the shared cached-getter protocol

The one home for the force_refresh → check → single-flight → in-slot re-check → `fetch` ordering. Every metadata getter routes through it: `arxiv.get_paper`, `openalex.get_work` / `get_author`, `crossref.get_work`, `biorxiv.get_paper`, `opencitations._fetch_direction`, `wikipedia.get_summary`. (`acl_anthology` has no metadata getter — it is PDF-only.)

**Two paths deliberately re-implement this ordering; a change here must be mirrored in both.** `openalex.get_works_batch` open-codes the outer invalidate / positive / negative check per DOI and coalesces a whole chunk through `_fetch_chunk`, and `_pdf_download.cached_download` is the file-on-disk sibling (`.claude/rules/pdf-download.md`). Anything else that wants this ordering calls `cached_lookup`.

`positive_ttl` has no default: every provider must state a freshness policy. **Pass a tuple `sf_key` whenever one identifier has more than one sub-fetch** (`("work", canonical)`, `("author", canonical)`, opencitations' `(kind, canonical)`) — each provider module owns *one* `SingleFlight` shared by all of its keys, so an un-namespaced key collides across entities, and a `fetch` that awaits another getter on the same instance self-deadlocks unless its key differs.

**Each caller receives an independent `copy.deepcopy`** — single-flight followers share the leader's object, and the in-slot re-check hands the same dict to every one of them.

**Known narrow race (unguarded, accepted).** Between a `force_refresh`'s `invalidate` and its in-slot re-check the entry can be rewritten — by a concurrent `get_works_batch` chunk under a different `sf_key`, or by a non-refresh leader the refresher queued behind — and the refresher then serves it without fetching. A property of the protocol, so it applies to every getter above, not to any one provider.

## _singleflight.py

`SingleFlight.do(key, factory)` collapses N concurrent calls for the same key into one execution; followers `await` the same future and share the leader's outcome — success or failure, and the *same object*. The slot is dropped after resolution, so a failure is never cached.

Cancellation is the subtle part: a leader's cancellation must not propagate to followers (one takes over, bounded by `_MAX_TAKEOVERS`), and a follower's must not reach the leader — hence `asyncio.shield`.

Providers reach `do` through a protocol wrapper — `cache.cached_lookup` or its file-on-disk sibling `_pdf_download.cached_download` — and the wrapper is what deep-copies per caller. One deliberate direct caller: `openalex._fetch_chunk`, which skips the copy on purpose (see `.claude/rules/providers.md`). Don't assume every `do` result is deep-copied downstream.
