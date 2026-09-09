---
paths:
  - "src/academic_tools_mcp/store/*.py"
---

# Cache and the cached-getter protocol

## store/cache.py

Generic file-based JSON cache under `.cache/<namespace>/<entity>/`, keyed by SHA-256 of the identifier. Provider-agnostic — a new provider is a new namespace.

**Atomicity, not durability.** Canonical writes go through `atomic.write_text` / `atomic.copy` — a sibling `mkstemp` temp, then `os.replace`. `fsync` is deliberately skipped, survivable only because every read self-heals — corrupt JSON, `OSError`, `UnicodeDecodeError` and non-dict payloads are caught, the bad file unlinked, `None` returned. **A new read path must hold up its half of that bargain**, and must name its encoding explicitly (UTF-8) so records survive an `LC_ALL=C` host.

**`put` / `put_negative` absorb `OSError` and return a bool.** A `fetch` closure must never treat `False` as a failure — the network response is already paid for, and serving it uncached is the correct outcome. Both go through `_write_entry`, which absorbs *only* `OSError`: an unserialisable payload is a programming error and must reach the caller. `atomic.write_text` is the single write seam, so patching it fakes a full disk for both writers at once. `_unlink_quietly` needs no counter of its own: every path that reaches a failed unlink goes on to `put` in the same directory, which fails and ticks `cache_write_failures`.

**`warm` is the search cache-warming probe, and the only sanctioned way to write a search hit.** TTL-aware, never a presence test: fresher search data replaces an entry already past the caller's TTL, but a within-TTL entry is never clobbered — a search hit is a slimmer record than a singleton fetch, so overwriting a live one downgrades it. Pass the same TTL the provider's *reader* uses, or the probe and the reader disagree about what "fresh" means. Its `get` is `count=False`: warming is not a lookup being served. Which keys one hit warms stays provider policy — crossref derives one from `item["DOI"]`, arxiv derives a versioned/bare pair.

**`_expires_at` is reserved** in a `put_negative` payload — overwritten on write, stripped on read. Every other key, underscore-prefixed ones included (`_canonical_id`), round-trips.

**Counters partition, they don't overlap.** `cache_hits` / `negative_hits` are booked by `get` / `get_negative` when a lookup is *served from disk*; `cache_misses` is booked immediately before `fetch` — it means "went upstream". A read that finds nothing counts nothing, which is why `openalex._fetch_chunk` books its own misses (it bypasses `cached_lookup`) and why `papers`' sections reads move no counter at all. PDF downloads are outside this series entirely.

**`CACHE_ROOT` is bound at import** (`_resolve_cache_root()` at module scope; `CACHE_DIR` relocates it for installed-wheel / read-only-tree deployments). `tests/conftest.py` monkeypatches that single attribute, so any module needing the test redirect reads `cache.CACHE_ROOT` at call time (`corpus`, `stems.migrate_legacy_stems`) rather than capturing it at import. Don't hide it behind a function without updating conftest.

**Cross-module surface — renames here break callers.** `cache_dir` and `CACHE_ROOT` are public because the path builders live elsewhere (`stems.pdf_path` / `markdown_path`, `corpus`). Anything reached from another module gets a public name; a leading underscore here means genuinely internal. `streaming.stream_to_file` writes its own temp-and-rename rather than calling `atomic`, because it enforces a byte cap while streaming.

**Lifetime**

- **Positive TTL eviction** — `get(..., max_age_seconds=N)` unlinks entries older than N seconds (by mtime) and returns `None`. Entries never re-read with a `max_age_seconds` persist indefinitely.
- **Negative cache** (`get_negative` / `put_negative`) lives in a sibling `_neg/` subdirectory under each entity; a provider overrides the default via its own `_NEG_TTL_SECONDS`, and the per-provider policy lives in `.claude/rules/providers.md`.
- **`invalidate(namespace, entity, identifier)`** drops both halves at once — used by `force_refresh=True`.
- **Orphan `.tmp` sweep** — `gc_orphan_tmp_files()` unlinks `*.tmp` files older than `_ORPHAN_TMP_AGE_SECONDS`, called from the FastMCP lifespan in `app`. It never touches files newer than the cutoff, so it cannot race a live writer. It bounds *leakage*, not cache size — it evicts no entries. **Invariant this rests on: a temp file's mtime is its own write time.** `atomic.copy` therefore calls `os.utime` after `shutil.copystat` — a copy that inherits an old source's mtime hands the sweep a live temp that reads as an orphan.
- **Boundaries fall on the safe side.** An entry aged exactly `max_age_seconds` still hits (`age > max_age`), a negative entry at exactly its expiry is still live (`expires_at < now`), and a `.tmp` exactly at the cutoff *is* swept (`mtime > cutoff`).

### `cached_lookup` — the shared cached-getter protocol

The one home for the force_refresh → check → single-flight → in-slot re-check → `fetch` ordering. Every metadata getter routes through it: `arxiv.get_paper`, `openalex.get_work` / `get_author`, `crossref.get_work`, `biorxiv.get_paper`, `opencitations._fetch_direction`, `wikipedia.get_summary`. (`acl` has no metadata getter — it is PDF-only.)

**Two paths deliberately re-implement this ordering; a change here must be mirrored in both.** `openalex.get_works_batch` open-codes the outer invalidate / positive / negative check per DOI and coalesces a whole chunk through `_fetch_chunk`, and `streaming.cached_download` is the file-on-disk sibling (`.claude/rules/download.md`). Anything else that wants this ordering calls `cached_lookup`.

`positive_ttl` has no default: every provider must state a freshness policy. **Pass a tuple `sf_key` whenever one identifier has more than one sub-fetch** (`("work", canonical)`, `("author", canonical)`, opencitations' `(kind, canonical)`) — each provider module owns *one* `SingleFlight` shared by all of its keys, so an un-namespaced key collides across entities, and a `fetch` that awaits another getter on the same instance self-deadlocks unless its key differs.

**Each caller receives an independent `copy.deepcopy`** — single-flight followers share the leader's object, and the in-slot re-check hands the same dict to every one of them.

**Known narrow race (unguarded, accepted).** Between a `force_refresh`'s `invalidate` and its in-slot re-check the entry can be rewritten — by a concurrent `get_works_batch` chunk under a different `sf_key`, or by a non-refresh leader the refresher queued behind — and the refresher then serves it without fetching. A property of the protocol, so it applies to every getter above, not to any one provider.

## store/singleflight.py

`SingleFlight.do(key, factory)` collapses N concurrent calls for the same key into one execution; followers `await` the same future and share the leader's outcome — success or failure, and the *same object*. The slot is dropped after resolution, so a failure is never cached.

Cancellation is the subtle part, and neither direction may leak.

- **A cancelled leader must not fail its followers.** The leader's task ending (an agent's tool call timing out, say) says nothing about the followers' lifetimes, so the `CancelledError` set on the shared future is not theirs to honour: a follower that is not itself cancelling takes over as the new leader and runs the factory. `_self_is_cancelling` — `Task.cancelling()`, the 3.11 cancel/uncancel protocol — is what tells the two apart.
- **A cancelled follower must not reach the leader**, which is what `asyncio.shield` is for. Cancelling a task cancels the future it is suspended on, and for a follower that is the *shared* future: unshielded, one follower giving up cancels the slot out from under everybody, the leader's `set_result` raises `InvalidStateError` into its own caller in place of a good result, and every remaining follower re-runs the factory.
- **`_MAX_FOLLOW_ATTEMPTS` bounds failed *follows*, not takeovers** — a caller that wins the slot returns and never comes back round the loop. Exhausting it means leaders were cancelled that many times in a row, and the caller then runs the factory itself, unslotted, rather than spinning.

The leader registers its future *before* its first await, and nothing suspends between `do`'s check and that insert (awaiting a coroutine runs its body inline), so the slot cannot be double-claimed. Keep both halves await-free.

Providers reach `do` through a protocol wrapper — `cache.cached_lookup` or its file-on-disk sibling `streaming.cached_download` — and the wrapper is what deep-copies per caller. One deliberate direct caller: `openalex._fetch_chunk`, which skips the copy on purpose (see `.claude/rules/providers.md`). Don't assume every `do` result is deep-copied downstream.

## store/stems.py

### Naming

- **Every derived path routes through `safe_stem()`** — PDF, markdown, sections key, extraction-dir prefix — so they can never disagree about which file belongs to which paper. It percent-encodes rather than collapsing, because collapsing would merge `a b` and `a_b` onto one file; `/` → `_` is the one deliberate exception, unreachable for real identifiers.
- **`urllib.parse.quote` is the sole authority on the output alphabet.** `safe_stem` is `quote(canonical, safe="").replace("%2F", "_")` — don't reintroduce a hand-written keep-set beside it, because the two drift: `quote` passes the whole RFC 3986 unreserved set (`~` and `_` included) and a keep-set that under-counts it makes `_MIGRATED_STEM_RE` wrong. The `%2F` rewrite is exact, not a heuristic: a literal `%` is written `%25`, so no other input can produce that escape, and `quote` emits uppercase hex.
- **`safe_stem` is not idempotent** (`a%20b` → `a%2520b`), so anything re-deriving a stem must gate on `_MIGRATED_STEM_RE` first. `migrate_legacy_stems()`, run once at startup, is the idempotent wrapper. **Invariant: `_MIGRATED_STEM_RE` is exactly `safe_stem`'s output alphabet** — a character the writer emits but the gate rejects makes startup re-encode a correct name (`notes~draft%202024` → `notes~draft%25202024`) and orphan the file it just renamed.
- **`pdf_path(namespace, canonical)` is the sibling of `markdown_path`, and every provider delegates to it.** Each keeps only its own canonicalization step. A local `safe_stem(x) + ".pdf"` is a fifth copy of a rule that has one home. A caller holding a *stem* off disk rather than an identifier takes `markdown_path_for_stem` / `sections_key_for_stem` — re-sanitizing a stored stem encodes its own escapes. Call it as `stems.pdf_path`. **No symbol here is re-exported through `papers`** — this module has exactly one import name (`.claude/rules/pipeline.md` § Layout), and `pdf_path` is why a facade could never carry the whole set anyway: the name would shadow each provider's own one-argument `pdf_path(identifier)`.
- **The sweep renames `.pdf` and `.md` only.** `atomic._new_temp` names an in-flight write `<dst.name>.<rand>.tmp`, whose stem still carries the destination's legacy characters; renaming it makes the writer's `os.replace` raise.
- **No startup sweep may raise.** They run inside `_lifespan`, so a directory one cannot read must be skipped, not propagated — an unreadable `.cache/` subtree would otherwise stop the server from starting. `list_dir` is that gate, shared by all three sweeps (`migrate_legacy_stems`, `manual.migrate_misrouted_arxiv`, `acl.migrate_legacy_pdf_stems`), and it also materialises the listing, because each renames files into the directory it is walking.

### Checksums

**A writer checksums the string it parsed (`checksum_text`), never the file it just wrote.** The two are separated by a window another writer fits through, and an index stamped with the *other* document's checksum matches disk forever — so `_reparse_sections_locked` accepts it and never re-parses. This is what makes the entry correct by construction rather than by lock discipline: a losing writer's entry simply mismatches and self-heals. It rests on `atomic.write_text` pinning `newline=""`, so the bytes on disk are exactly the UTF-8 encoding of the payload.

The on-disk half of that comparison is a *test* oracle — `tests/helpers/checksums.py`, deliberately not importable from `src/`. Re-adding it to `stems` so a writer can reach it is the bug the paragraph above describes.
