---
paths:
  - "src/academic_tools_mcp/cache.py"
  - "src/academic_tools_mcp/_singleflight.py"
---

# Cache and the cached-getter protocol

## cache.py

Generic file-based JSON cache under `.cache/<provider>/<entity>/`, keyed by SHA-256 of the identifier. Provider-agnostic — a new provider is a new namespace.

**Durability and encoding**

- **Atomic writes** via `_atomic_write_text` (mkstemp + `os.replace`; `_atomic_write_json` wraps it for JSON). A reader can never see a half-written entry, though a killed process can leak a stray `.tmp`. **This is not a crash-durability guarantee** — `os.replace` orders the rename, not the data flush, and `fsync` is deliberately skipped as a cost-vs-benefit call for a self-healing cache; a power loss can leave a durable rename pointing at unflushed bytes, which the next read treats as corruption.
- **`_atomic_copy(src, dst)`** — the same guarantee for arbitrary binary files: chunked `shutil.copyfileobj` through a sibling temp, `copystat`, `os.replace`, never buffering the whole file. Binary content routes through it, text through `_atomic_write_text`.
- **Reads and writes are always UTF-8**, explicitly (`ensure_ascii=False` on write, `encoding="utf-8"` both ways), so non-ASCII records survive a non-UTF-8 locale (`LC_ALL=C` containers) instead of failing to decode and self-deleting.
- **Self-healing reads** — corrupt JSON, OS errors, Unicode errors *and non-dict payloads* are caught, the bad file is unlinked, `get`/`get_negative` return `None`.

**Lifetime**

- **Positive TTL eviction** — `get(..., max_age_seconds=N)` unlinks entries older than N seconds (by mtime) and returns `None`.
- **Negative cache** (`get_negative` / `put_negative`) lives in a sibling `_neg/` subdirectory under each entity, defaulting to 24h; a provider overrides via its own `_NEG_TTL_SECONDS` (arxiv/biorxiv use 1h).
- **`invalidate(namespace, entity, identifier)`** drops both halves at once — used by `force_refresh=True`.
- **No global size bound** — growth is bounded only by on-read TTL eviction and the orphan sweep. Entries never re-read with a `max_age_seconds` persist indefinitely; operators prune `.cache/` by hand.
- **Orphan `.tmp` sweep** — `gc_orphan_tmp_files()` walks `.cache/` for `*.tmp` files older than 1h, called from FastMCP lifespan startup. It never touches files newer than the cutoff, so it cannot race a live writer.

**Surface**

- **Cache root** is `.cache/` beside the project; `CACHE_DIR` (via `config.get`, resolved in `_resolve_cache_root`) relocates it for installed-wheel / read-only-tree deployments.
- **`cache_dir` is public on purpose** — the PDF-handling modules (`providers/*.download_pdf`, `manual`, `papers`) build canonical file paths under it. Everything else here is reached through `get`/`put`/`cached_lookup`.

### `cached_lookup` — the shared cached-getter protocol

`cache.cached_lookup` is the one home for the protocol every provider getter (`arxiv.get_paper`, `openalex.get_work`/`get_author`, `crossref.get_work`, `biorxiv.get_paper`, `opencitations._fetch_direction`, `wikipedia.get_summary`) routes through:

1. `force_refresh` → `invalidate` both halves, then always fetch.
2. otherwise check positive cache (TTL-aware) then negative cache, short-circuit on a hit.
3. coalesce concurrent callers for `sf_key` (default `canonical`) via single-flight; **inside** the slot, re-check both caches first (so a follower picks up the leader's just-written entry instead of re-fetching), then call `fetch`.

`fetch` is an async closure that owns its own caching — it decides whether to `put` (positive), `put_negative` (with whatever TTL), or cache nothing (transient parse errors); it's only called on a genuine miss. Provider quirks (bioRxiv's medRxiv fallback, arxiv's three not-found shapes, the explicit 404 branch) all live in that closure. Pass a tuple `sf_key` to keep distinct sub-fetches for one id apart (`("work", canonical)`, `("references", canonical)`). **Each caller receives an independent `copy.deepcopy` of the result** so in-batch single-flight followers (who share the leader's object) can't corrupt each other or the cached dict.

## _singleflight.py

`SingleFlight.do(key, factory)` collapses N concurrent calls for the same key into one execution; followers `await` the same future and share the leader's outcome — success or failure, and the *same object*. The slot is dropped after resolution, so a failure is never cached. Each provider holds its own instance but reaches `do` through `cache.cached_lookup`, never directly — that helper is what deep-copies per caller.
