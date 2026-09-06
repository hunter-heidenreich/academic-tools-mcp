---
paths:
  - "src/academic_tools_mcp/cache.py"
  - "src/academic_tools_mcp/atomic.py"
  - "src/academic_tools_mcp/_singleflight.py"
---

# Cache and the cached-getter protocol

## cache.py

Generic file-based JSON cache under `.cache/<namespace>/<entity>/`, keyed by SHA-256 of the identifier. Provider-agnostic — a new provider is a new namespace.

**Atomicity, not durability.** Canonical writes go through `atomic.write_text` / `atomic.copy` — a sibling `mkstemp` temp, then `os.replace`. `fsync` is deliberately skipped, survivable only because every read self-heals — corrupt JSON, `OSError`, `UnicodeDecodeError` and non-dict payloads are caught, the bad file unlinked, `None` returned. **A new read path must hold up its half of that bargain**, and must name its encoding explicitly (UTF-8) so records survive an `LC_ALL=C` host.

**`put` / `put_negative` absorb `OSError` and return a bool.** A `fetch` closure must never treat `False` as a failure — the network response is already paid for, and serving it uncached is the correct outcome. Both go through `_write_entry`, which absorbs *only* `OSError`: an unserialisable payload is a programming error and must reach the caller. `atomic.write_text` is the single write seam, so patching it fakes a full disk for both writers at once. `_unlink_quietly` needs no counter of its own: every path that reaches a failed unlink goes on to `put` in the same directory, which fails and ticks `cache_write_failures`.

**`_expires_at` is reserved** in a `put_negative` payload — overwritten on write, stripped on read. Every other key, underscore-prefixed ones included (`_canonical_id`), round-trips.

**Counters partition, they don't overlap.** `cache_hits` / `negative_hits` are booked by `get` / `get_negative` when a lookup is *served from disk*; `cache_misses` is booked immediately before `fetch` — it means "went upstream". A read that finds nothing counts nothing, which is why `openalex._fetch_chunk` books its own misses (it bypasses `cached_lookup`) and why `papers`' sections reads move no counter at all. PDF downloads are outside this series entirely.

**`CACHE_ROOT` is bound at import** (`_resolve_cache_root()` at module scope; `CACHE_DIR` relocates it for installed-wheel / read-only-tree deployments). `tests/conftest.py` monkeypatches that single attribute, so any module needing the test redirect reads `cache.CACHE_ROOT` at call time (`cache_search`, `_stems.migrate_legacy_stems`) rather than capturing it at import. Don't hide it behind a function without updating conftest.

**Cross-module surface — renames here break callers.** `cache_dir` and `CACHE_ROOT` are public because the path builders live elsewhere (`_stems.pdf_path` / `markdown_path`, `cache_search`). Anything reached from another module gets a public name; a leading underscore here means genuinely internal. `_pdf_download.stream_to_file` writes its own temp-and-rename rather than calling `atomic`, because it enforces a byte cap while streaming.

**Lifetime**

- **Positive TTL eviction** — `get(..., max_age_seconds=N)` unlinks entries older than N seconds (by mtime) and returns `None`. Entries never re-read with a `max_age_seconds` persist indefinitely.
- **Negative cache** (`get_negative` / `put_negative`) lives in a sibling `_neg/` subdirectory under each entity; a provider overrides the default via its own `_NEG_TTL_SECONDS`, and the per-provider policy lives in `.claude/rules/providers.md`.
- **`invalidate(namespace, entity, identifier)`** drops both halves at once — used by `force_refresh=True`.
- **Orphan `.tmp` sweep** — `gc_orphan_tmp_files()` unlinks `*.tmp` files older than `_ORPHAN_TMP_AGE_SECONDS`, called from the FastMCP lifespan in `_app`. It never touches files newer than the cutoff, so it cannot race a live writer. It bounds *leakage*, not cache size — it evicts no entries. **Invariant this rests on: a temp file's mtime is its own write time.** `atomic.copy` therefore calls `os.utime` after `shutil.copystat` — a copy that inherits an old source's mtime hands the sweep a live temp that reads as an orphan.
- **Boundaries fall on the safe side.** An entry aged exactly `max_age_seconds` still hits (`age > max_age`), a negative entry at exactly its expiry is still live (`expires_at < now`), and a `.tmp` exactly at the cutoff *is* swept (`mtime > cutoff`).

### `cached_lookup` — the shared cached-getter protocol

The one home for the force_refresh → check → single-flight → in-slot re-check → `fetch` ordering. Every metadata getter routes through it: `arxiv.get_paper`, `openalex.get_work` / `get_author`, `crossref.get_work`, `biorxiv.get_paper`, `opencitations._fetch_direction`, `wikipedia.get_summary`. (`acl_anthology` has no metadata getter — it is PDF-only.)

**Two paths deliberately re-implement this ordering; a change here must be mirrored in both.** `openalex.get_works_batch` open-codes the outer invalidate / positive / negative check per DOI and coalesces a whole chunk through `_fetch_chunk`, and `_pdf_download.cached_download` is the file-on-disk sibling (`.claude/rules/pdf-download.md`). Anything else that wants this ordering calls `cached_lookup`.

`positive_ttl` has no default: every provider must state a freshness policy. **Pass a tuple `sf_key` whenever one identifier has more than one sub-fetch** (`("work", canonical)`, `("author", canonical)`, opencitations' `(kind, canonical)`) — each provider module owns *one* `SingleFlight` shared by all of its keys, so an un-namespaced key collides across entities, and a `fetch` that awaits another getter on the same instance self-deadlocks unless its key differs.

**Each caller receives an independent `copy.deepcopy`** — single-flight followers share the leader's object, and the in-slot re-check hands the same dict to every one of them.

**Known narrow race (unguarded, accepted).** Between a `force_refresh`'s `invalidate` and its in-slot re-check the entry can be rewritten — by a concurrent `get_works_batch` chunk under a different `sf_key`, or by a non-refresh leader the refresher queued behind — and the refresher then serves it without fetching. A property of the protocol, so it applies to every getter above, not to any one provider.

## _singleflight.py

`SingleFlight.do(key, factory)` collapses N concurrent calls for the same key into one execution; followers `await` the same future and share the leader's outcome — success or failure, and the *same object*. The slot is dropped after resolution, so a failure is never cached.

Cancellation is the subtle part, and neither direction may leak — the module docstring points here rather than restating it.

- **A cancelled leader must not fail its followers.** The leader's task ending (an agent's tool call timing out, say) says nothing about the followers' lifetimes, so the `CancelledError` set on the shared future is not theirs to honour: a follower that is not itself cancelling takes over as the new leader and runs the factory. `_self_is_cancelling` — `Task.cancelling()`, the 3.11 cancel/uncancel protocol — is what tells the two apart.
- **A cancelled follower must not reach the leader**, which is what `asyncio.shield` is for. Cancelling a task cancels the future it is suspended on, and for a follower that is the *shared* future: unshielded, one follower giving up cancels the slot out from under everybody, the leader's `set_result` raises `InvalidStateError` into its own caller in place of a good result, and every remaining follower re-runs the factory.
- **`_MAX_FOLLOW_ATTEMPTS` bounds failed *follows*, not takeovers** — a caller that wins the slot returns and never comes back round the loop. Exhausting it means leaders were cancelled that many times in a row, and the caller then runs the factory itself, unslotted, rather than spinning.

The leader registers its future *before* its first await, and nothing suspends between `do`'s check and that insert (awaiting a coroutine runs its body inline), so the slot cannot be double-claimed. Keep both halves await-free.

Providers reach `do` through a protocol wrapper — `cache.cached_lookup` or its file-on-disk sibling `_pdf_download.cached_download` — and the wrapper is what deep-copies per caller. One deliberate direct caller: `openalex._fetch_chunk`, which skips the copy on purpose (see `.claude/rules/providers.md`). Don't assume every `do` result is deep-copied downstream.
