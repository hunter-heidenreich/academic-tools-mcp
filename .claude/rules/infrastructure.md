---
paths:
  - "src/academic_tools_mcp/cache.py"
  - "src/academic_tools_mcp/_http.py"
  - "src/academic_tools_mcp/_throttle.py"
  - "src/academic_tools_mcp/_clients.py"
  - "src/academic_tools_mcp/_singleflight.py"
  - "src/academic_tools_mcp/_stats.py"
  - "src/academic_tools_mcp/_pdf_download.py"
  - "src/academic_tools_mcp/config.py"
---

# Shared infrastructure

## cache.py

Generic file-based JSON cache under `.cache/<provider>/<entity>/`. Files are SHA-256 hashed by identifier.

- **Atomic writes** via `_atomic_write_text` (mkstemp + `os.replace`; `_atomic_write_json` is a thin UTF-8 wrapper over it). A crashed/killed process can leak a stray `.tmp` file but a reader can never see a half-written canonical entry. This is *not* a crash-durability guarantee: `os.replace` orders the rename, not the data flush, and we deliberately skip `fsync` (cost-vs-benefit for a self-healing cache) — a power loss could leave a durable rename pointing at unflushed contents, which a self-healing read then treats as corruption.
- **`_atomic_copy(src, dst)`** gives the same torn-write guarantee for an arbitrary (possibly large) binary file — chunked `shutil.copyfileobj` through a sibling temp, `copystat`, then `os.replace` — without buffering the whole file in memory. `manual.import_local_pdf` uses it for imported PDFs; `manual.import_markdown` and `papers._finalize_markdown` use `_atomic_write_text` for the markdown.
- **Reads/writes are always UTF-8.** Writes use `ensure_ascii=False` + explicit `encoding="utf-8"`; reads pass `encoding="utf-8"` too, so non-ASCII records survive on hosts with a non-UTF-8 locale (e.g. `LC_ALL=C` containers) instead of failing to decode and self-deleting.
- **Self-healing reads** — corrupt JSON / OS errors / Unicode errors *and non-dict payloads* are caught, the bad file is unlinked, `get`/`get_negative` return `None`.
- **Cache root** is `.cache/` next to the project by default; the `CACHE_DIR` env var (via `config.get`, resolved in `_resolve_cache_root`) relocates it for installed-wheel / read-only-tree deployments.
- **No global size bound** — growth is bounded only by on-read TTL eviction (`max_age_seconds`) and the orphan `.tmp` sweep. Entries never re-read with a `max_age_seconds` persist indefinitely; operators prune `.cache/` manually.
- **Negative cache** (`get_negative` / `put_negative`) lives in a sibling `_neg/` subdirectory under each entity. Default 24h TTL on negatives; arxiv/biorxiv override to 1h via per-module `_NEG_TTL_SECONDS` because preprint identifiers go live mid-session.
- **Positive TTL eviction** — `cache.get(..., max_age_seconds=N)` unlinks entries older than N seconds (by mtime) and returns `None`.
- **`cache.invalidate(namespace, entity, identifier)`** drops both halves at once — used by `force_refresh=True`.
- **`cache.cache_dir(namespace, entity)`** — public (the PDF-handling modules `providers/*.download_pdf`, `manual`, `papers` build canonical file paths under it). Returns `.cache/<namespace>/<entity>/`.
- **Orphan `.tmp` sweep** — `cache.gc_orphan_tmp_files()` walks `.cache/` for `*.tmp` files older than 1h. Called from FastMCP lifespan startup; never touches files newer than the cutoff so it can't race a live writer.

Cache contents are agnostic to provider — scales to new providers by namespace.

### `cached_lookup` — the shared cached-getter protocol

`cache.cached_lookup(*, single_flight, namespace, entity, canonical, positive_ttl, fetch, force_refresh=False, sf_key=None)` is the one home for the protocol every provider getter (`arxiv.get_paper`, `openalex.get_work`/`get_author`, `crossref.get_work`, `biorxiv.get_paper`, `opencitations._fetch_direction`, `wikipedia.get_summary`) used to hand-roll:

1. `force_refresh` → `invalidate` both halves, then always fetch.
2. otherwise check positive cache (TTL-aware) then negative cache, short-circuit on a hit.
3. coalesce concurrent callers for `sf_key` (default `canonical`) via single-flight; **inside** the slot, re-check both caches first (so a follower picks up the leader's just-written entry instead of re-fetching), then call `fetch`.

`fetch` is an async closure that owns its own caching — it decides whether to `put` (positive), `put_negative` (with whatever TTL), or cache nothing (transient parse errors); it's only called on a genuine miss. Provider quirks (bioRxiv's medRxiv fallback, arxiv's three not-found shapes, the explicit 404 branch) all live in that closure. Pass a tuple `sf_key` to keep distinct sub-fetches for one id apart (`("work", canonical)`, `("references", canonical)`). **Each caller receives an independent `copy.deepcopy` of the result** so in-batch single-flight followers (who share the leader's object) can't corrupt each other or the cached dict.

## _clients.py

Per-provider lazy-singleton `httpx.AsyncClient` pool. Each provider gets one long-lived client (`httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30s)`) — TCP+TLS handshakes get reused. Headers (e.g. polite-pool User-Agent) are baked in at construction.

`aclose_all()` is wired to FastMCP's lifespan so sockets close on shutdown. Each per-client `aclose` is bounded by `_ACLOSE_TIMEOUT_SECONDS=5.0` so a wedged socket on one provider can't pin the lifespan or block others from closing.

## _singleflight.py

`SingleFlight.do(key, factory)` collapses N concurrent calls for the same key into one execution; followers `await` the same future and share the leader's result (success or failure). After resolution the slot is dropped — failure is not cached. Each provider holds its own `_single_flight` instance. Followers share the *same* result object, so `cache.cached_lookup` (the protocol that wraps `do`) deep-copies the result per caller — the providers reach `do` through that helper, not directly.

## _http.py

Shared HTTP utilities used by every API client.

- `HTTPX_ERRORS` — tuple of `httpx.HTTPStatusError`, `TimeoutException`, `RequestError`, plus `LocalBackpressureError`. Every client wraps its request block in `try/except _http.HTTPX_ERRORS`.
- `error_dict(provider, exc)` — converts exceptions to `{error, retry_after_seconds?, retryable?, backpressure?}` dicts with provider-aware messages.
- `LocalBackpressureError(provider, pending, max_pending, min_gap_seconds=0.0)` — raised by `Throttle.slot` when `pending >= max_pending` (default 5). Surfaces as `{error, retryable: True, backpressure: True, max_concurrency, retry_after_seconds?}`. `retry_after_seconds` only appears when the provider has a documented gap (omitted for ACL Anthology where `min_gap_seconds=0`).
- `get_with_retry(client, url, *, max_attempts=2, backoff_seconds=1.0, provider=None, **kwargs)` — issues a GET with transparent retries on transient failure (timeouts, network errors, 408/425/429, 5xx). Backoff grows **exponentially** across attempts: the sleep before attempt *n* is `backoff_seconds * 2**(n-1)`, so at the default `max_attempts=2` only one sleep happens (factor 1 — single-retry providers are byte-for-byte unchanged), while a provider that raises `max_attempts` (arXiv) gets a widening gap. On 429/503 honours `Retry-After`. Actual sleep is `min(max(retry_after, effective_backoff), _MAX_RETRY_AFTER_SECONDS)` — `backoff_seconds` (the provider's own throttle gap) is the floor, and `_MAX_RETRY_AFTER_SECONDS` (600s / 10 min) is an absolute ceiling so a genuine multi-minute cooldown is respected while a misconfigured huge `Retry-After` (or runaway exponential growth) can't pin the throttle for hours. When `provider` is passed, retries are recorded in `_stats`. `max_attempts` is set per provider by `Throttle.get` (from `Throttle.retry_attempts`).

## _stats.py

Per-provider counters (`cache_hits`, `cache_misses`, `negative_hits`, `http_calls`, `http_retries`, `backpressure_refusals`) plus a live `in_flight` sample drawn from each provider module's `_throttle.pending`.

- `_stats.snapshot()` returns `{providers: {arxiv: {...}, openalex: {...}, ...}}`.
- `_stats.reset()` zeroes cumulative counters (used by the test fixture).
- Wired into `cache.get`/`get_negative` and `Throttle.slot` (`http_calls` after the gap clears, `backpressure_refusals` on burst-cap refusal).
- **`DEBUG_REQUESTS`** flag (`1`/`true`/`yes`/`on`) makes each throttled GET log `[academic-tools] {provider} GET {url} (throttle wait Xs)` to **stderr** (not stdout — MCP speaks JSON-RPC there). Re-read every call so an operator can flip the flag without restarting.

When adding a new provider: append the module name to `_PROVIDER_MODULES` so its `in_flight` count appears in `snapshot()`.

## config.py

Loads `.env` from project root. All API credentials come from env vars, never from tool parameters.

## _throttle.py — the shared `Throttle`

The gating *mechanism* lives once here; each provider declares only its *policy*
constants (`_MAX_CONCURRENT`, `_MIN_REQUEST_GAP`, `_MAX_PENDING`) and constructs one
`Throttle(namespace, label, max_concurrent, min_gap_seconds, max_pending)`. It owns
the mutable runtime state (`pending`, `last_request_time`, and a loop-bound
semaphore + lock) — no more `global _pending`/`_last_request_time` per module.

`Throttle.slot(url)` (an `@asynccontextmanager`) enforces three layers in order:

1. **Burst cap** — `pending >= max_pending` (default 5) raises `LocalBackpressureError` immediately, before any sem/lock acquisition. The 6th concurrent caller fails fast instead of silently queueing.
2. **Concurrency cap** — an `asyncio.Semaphore(max_concurrent)` caps simultaneous in-flight requests. Per-provider `_MAX_CONCURRENT`: arxiv=1 (single-connection rule), openalex=4, acl_anthology=4, crossref=3 (polite-pool concurrency budget), biorxiv=2, opencitations=2, wikipedia=2, oa_download=2.
3. **Inter-start gap** — the lock is held only briefly to enforce `min_gap_seconds` between request *starts* (not durations). Released before the actual GET so concurrent in-flight requests don't block each other. `_stats.log_request` + `incr("http_calls")` fire here.

`Throttle.get(client, url, **kw)` is the common case: fire one `_http.get_with_retry` inside `slot(url)` (with `backoff_seconds=max(min_gap_seconds, 1.0)` and `max_attempts=retry_attempts`). `retry_attempts` (constructor arg, default 2 = one retry) is per-provider policy alongside the gap/concurrency caps — arxiv raises it to 3 because its Fastly edge returns 429/503 with no `Retry-After` and one retry tends to land in the same cooldown. `Throttle.reset()` zeroes the counters and rebuilds the loop-bound lock/sem (the conftest fixture calls it between tests).

Each provider keeps thin **module-level wrappers** that delegate to its instance — `_throttled_get` → `_throttle.get` (openalex's is url-only and builds the client internally), `_request_slot` → `_throttle.slot` (arxiv/biorxiv/acl_anthology/oa_download). The wrappers preserve the call sites and the test seams (`monkeypatch.setattr(mod, "_throttled_get"/"_request_slot", ...)`); tests that need to override pacing set `mod._throttle.min_gap_seconds`. Streaming PDF downloads hold `slot(url)` open for the whole stream lifetime — open connections counting toward `max_concurrent` is the correct semantics, since releasing earlier would let a fan-out exceed documented limits while slow streams flush.

The gating behaviour is verified once in `tests/test_throttle.py` (gap, concurrency cap, burst cap, slot lifetime, reset, get) rather than re-tested per provider.

## _pdf_download.py

Shared streaming-download helper used by `arxiv.download_pdf`, `biorxiv.download_pdf`, and `acl_anthology.download_pdf`. The slot acquisition is per-provider (different gap / concurrency caps), but the streaming + size-capping + atomic-rename logic is identical and lives here.

- **`stream_to_file(client, url, dest, *, slot_factory, provider_label, timeout=60.0, not_found_message=None)`** — opens `client.stream("GET", ...)` inside the provider's slot, writes 64 KiB chunks to a sibling `.tmp` file via `mkstemp`, and `os.replace`s into place on success. Peak memory = one chunk, not the whole PDF (the previous `response.content` + `write_bytes` path peaked at 2× PDF size).
- **MAX_PDF_BYTES cap** — `resolve_max_pdf_bytes()` reads `MAX_PDF_BYTES` env var (default 200_000_000; `none`/`off`/`disabled`/`0` disables). A download that would exceed the cap is aborted mid-stream with `{error, retryable: False, max_bytes}`; the partial temp is unlinked, dest is never created. Fires *during* the download so a misrouted URL can't fill the disk before any size check.
- **Cleanup** — every non-success path (404, transport error, size cap, exception) unlinks the temp file. The fd is closed manually if we never reached `os.fdopen` (early-return before the write loop). Success paths leave `tmp_path.unlink()` as a no-op because `os.replace` already moved the file.
