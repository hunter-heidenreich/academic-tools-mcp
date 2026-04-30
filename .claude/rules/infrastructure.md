---
paths:
  - "src/academic_tools_mcp/cache.py"
  - "src/academic_tools_mcp/_http.py"
  - "src/academic_tools_mcp/_clients.py"
  - "src/academic_tools_mcp/_singleflight.py"
  - "src/academic_tools_mcp/_stats.py"
  - "src/academic_tools_mcp/_pdf_download.py"
  - "src/academic_tools_mcp/config.py"
---

# Shared infrastructure

## cache.py

Generic file-based JSON cache under `.cache/<provider>/<entity>/`. Files are SHA-256 hashed by identifier.

- **Atomic writes** via `_atomic_write_json` (mkstemp + `os.replace`). A crashed/killed process can leak a stray `.tmp` file but cannot leave a half-written canonical entry.
- **Self-healing reads** — corrupt JSON / OS errors / Unicode errors are caught, the bad file is unlinked, `get` returns `None`.
- **Negative cache** (`get_negative` / `put_negative`) lives in a sibling `_neg/` subdirectory under each entity. Default 24h TTL on negatives; arxiv/biorxiv override to 1h via per-module `_NEG_TTL_SECONDS` because preprint identifiers go live mid-session.
- **Positive TTL eviction** — `cache.get(..., max_age_seconds=N)` unlinks entries older than N seconds (by mtime) and returns `None`.
- **`cache.invalidate(namespace, entity, identifier)`** drops both halves at once — used by `force_refresh=True`.
- **Orphan `.tmp` sweep** — `cache.gc_orphan_tmp_files()` walks `.cache/` for `*.tmp` files older than 1h. Called from FastMCP lifespan startup; never touches files newer than the cutoff so it can't race a live writer.

Cache contents are agnostic to provider — scales to new providers by namespace.

## _clients.py

Per-provider lazy-singleton `httpx.AsyncClient` pool. Each provider gets one long-lived client (`httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30s)`) — TCP+TLS handshakes get reused. Headers (e.g. polite-pool User-Agent) are baked in at construction.

`aclose_all()` is wired to FastMCP's lifespan so sockets close on shutdown. Each per-client `aclose` is bounded by `_ACLOSE_TIMEOUT_SECONDS=5.0` so a wedged socket on one provider can't pin the lifespan or block others from closing.

## _singleflight.py

`SingleFlight.do(key, factory)` collapses N concurrent calls for the same key into one execution; followers `await` the same future and share the leader's result (success or failure). After resolution the slot is dropped — failure is not cached. Each provider holds its own `_single_flight` instance.

## _http.py

Shared HTTP utilities used by every API client.

- `HTTPX_ERRORS` — tuple of `httpx.HTTPStatusError`, `TimeoutException`, `RequestError`, plus `LocalBackpressureError`. Every client wraps its request block in `try/except _http.HTTPX_ERRORS`.
- `error_dict(provider, exc)` — converts exceptions to `{error, retry_after_seconds?, retryable?, backpressure?}` dicts with provider-aware messages.
- `LocalBackpressureError(provider, pending, max_pending, min_gap_seconds=0.0)` — raised when the per-provider `_throttled_get` sees `_pending >= _MAX_PENDING` (default 5). Surfaces as `{error, retryable: True, backpressure: True, max_concurrency, retry_after_seconds?}`. `retry_after_seconds` only appears when the provider has a documented gap (omitted for ACL Anthology where `_MIN_REQUEST_GAP=0`).
- `get_with_retry(client, url, *, max_attempts=2, backoff_seconds=1.0, provider=None, **kwargs)` — issues a GET with one transparent retry on transient failure (timeouts, network errors, 408/425/429, 5xx). On 429/503 honours `Retry-After` capped at `backoff_seconds * 30`. Actual sleep is `min(max(retry_after, backoff_seconds), cap)` — provider's own throttle gap is the floor. When `provider` is passed, retries are recorded in `_stats`.

## _stats.py

Per-provider counters (`cache_hits`, `cache_misses`, `negative_hits`, `http_calls`, `http_retries`, `backpressure_refusals`) plus a live `in_flight` sample drawn from each provider module's `_pending` global.

- `_stats.snapshot()` returns `{providers: {arxiv: {...}, openalex: {...}, ...}}`.
- `_stats.reset()` zeroes cumulative counters (used by the test fixture).
- Wired into `cache.get`/`get_negative` and every provider's `_throttled_get`.
- **`DEBUG_REQUESTS`** flag (`1`/`true`/`yes`/`on`) makes each throttled GET log `[academic-tools] {provider} GET {url} (throttle wait Xs)` to **stderr** (not stdout — MCP speaks JSON-RPC there). Re-read every call so an operator can flip the flag without restarting.

When adding a new provider: append the module name to `_PROVIDER_MODULES` so its `in_flight` count appears in `snapshot()`.

## config.py

Loads `.env` from project root. All API credentials come from env vars, never from tool parameters.

## Throttle shape (uniform across providers)

Every provider's `_throttled_get` (and, in the PDF-downloading providers, `_request_slot` async context manager) enforces three layers in this order:

1. **Burst cap** — `_pending >= _MAX_PENDING` (default 5) raises `LocalBackpressureError` immediately, before any sem/lock acquisition. The 6th concurrent caller fails fast instead of silently queueing.
2. **Concurrency cap** — `_request_sem = asyncio.Semaphore(_MAX_CONCURRENT)` caps simultaneous in-flight requests. Per-provider values: arxiv=1 (single-connection rule), openalex=4, acl_anthology=4, crossref=3 (polite-pool concurrency budget), biorxiv=2, opencitations=2, wikipedia=2.
3. **Inter-start gap** — `_request_lock` is held only briefly to enforce `_MIN_REQUEST_GAP` between request *starts* (not durations). Released before the actual GET so concurrent in-flight requests don't block each other.

`_request_slot` exposes the gating as a context manager so streaming PDF downloads can hold the slot for the whole stream lifetime — open connections counting toward `_MAX_CONCURRENT` is the correct semantics, since releasing earlier would let a fan-out exceed documented limits while slow streams flush. `_throttled_get` becomes a thin wrapper that fires `_http.get_with_retry` inside the slot.

## _pdf_download.py

Shared streaming-download helper used by `arxiv.download_pdf`, `biorxiv.download_pdf`, and `acl_anthology.download_pdf`. The slot acquisition is per-provider (different gap / concurrency caps), but the streaming + size-capping + atomic-rename logic is identical and lives here.

- **`stream_to_file(client, url, dest, *, slot_factory, provider_label, timeout=60.0, not_found_message=None)`** — opens `client.stream("GET", ...)` inside the provider's slot, writes 64 KiB chunks to a sibling `.tmp` file via `mkstemp`, and `os.replace`s into place on success. Peak memory = one chunk, not the whole PDF (the previous `response.content` + `write_bytes` path peaked at 2× PDF size).
- **MAX_PDF_BYTES cap** — `resolve_max_pdf_bytes()` reads `MAX_PDF_BYTES` env var (default 200_000_000; `none`/`off`/`disabled`/`0` disables). A download that would exceed the cap is aborted mid-stream with `{error, retryable: False, max_bytes}`; the partial temp is unlinked, dest is never created. Fires *during* the download so a misrouted URL can't fill the disk before any size check.
- **Cleanup** — every non-success path (404, transport error, size cap, exception) unlinks the temp file. The fd is closed manually if we never reached `os.fdopen` (early-return before the write loop). Success paths leave `tmp_path.unlink()` as a no-op because `os.replace` already moved the file.
