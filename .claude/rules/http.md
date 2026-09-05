---
paths:
  - "src/academic_tools_mcp/_http.py"
  - "src/academic_tools_mcp/_throttle.py"
  - "src/academic_tools_mcp/_clients.py"
  - "src/academic_tools_mcp/_stats.py"
---

# HTTP: clients, retry, throttling, stats

## _clients.py

Per-provider lazy-singleton `httpx.AsyncClient` pool. Each provider gets one long-lived client (`httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30s)`) — TCP+TLS handshakes get reused. Headers (e.g. polite-pool User-Agent) are baked in at construction.

`aclose_all()` is wired to FastMCP's lifespan so sockets close on shutdown. Each per-client `aclose` is bounded by `_ACLOSE_TIMEOUT_SECONDS=5.0` so a wedged socket on one provider can't pin the lifespan or block others from closing.

## _http.py

Shared HTTP utilities used by every API client.

- `HTTPX_ERRORS` — tuple of `httpx.HTTPStatusError`, `TimeoutException`, `RequestError`, plus `LocalBackpressureError`. Every client wraps its request block in `try/except _http.HTTPX_ERRORS`.
- `error_dict(provider, exc)` — converts exceptions to `{error, retry_after_seconds?, retryable?, backpressure?}` dicts with provider-aware messages.
- `LocalBackpressureError(provider, pending, max_pending, min_gap_seconds=0.0)` — raised by `Throttle.slot` when `pending >= max_pending` (default 5). Surfaces as `{error, retryable: True, backpressure: True, max_concurrency, retry_after_seconds?}`. **`max_concurrency` carries `max_pending`, not `max_concurrent`** — it is the burst cap, despite the name. `retry_after_seconds` only appears when the provider has a documented gap (omitted for ACL Anthology where `min_gap_seconds=0`).
- `get_with_retry` — issues a GET with transparent retries on transient failure (timeouts, network errors, and `_RETRYABLE_STATUSES` = 408/425/429/500/502/503/504 — note *not* every 5xx). Backoff grows **exponentially** across attempts: the sleep before attempt *n* is `backoff_seconds * 2**(n-1)`, so at the default `max_attempts=2` only one sleep happens, while a provider that raises `max_attempts` (arXiv) gets a widening gap. On 429/503 honours `Retry-After`. Actual sleep is `min(max(retry_after, effective_backoff), _MAX_RETRY_AFTER_SECONDS)` — `backoff_seconds` (the provider's own throttle gap) is the floor, and `_MAX_RETRY_AFTER_SECONDS` (600s / 10 min) is an absolute ceiling so a genuine multi-minute cooldown is respected while a misconfigured huge `Retry-After` (or runaway exponential growth) can't pin the throttle for hours. When `provider` is passed, **each attempt** increments `http_calls` and each retry increments `http_retries` in `_stats` — counting per outbound request rather than per slot, since one slot can issue up to `max_attempts` of them. `max_attempts` is set per provider by `Throttle.get` (from `Throttle.retry_attempts`).

## _throttle.py — the shared `Throttle`

The gating *mechanism* lives once here; each provider declares only its *policy* constants (`_MAX_CONCURRENT`, `_MIN_REQUEST_GAP`, `_MAX_PENDING`) and constructs one `Throttle(namespace, label, max_concurrent, min_gap_seconds, max_pending)`. It owns the mutable runtime state (`pending`, `last_request_time`, and a loop-bound semaphore + lock).

`Throttle.slot(url)` (an `@asynccontextmanager`) enforces three layers in order:

1. **Burst cap** — `pending >= max_pending` (default 5) raises `LocalBackpressureError` immediately, before any sem/lock acquisition. The 6th concurrent caller fails fast instead of silently queueing.
2. **Concurrency cap** — an `asyncio.Semaphore(max_concurrent)` caps simultaneous in-flight requests. Each provider declares its own `_MAX_CONCURRENT` and passes it in; the values and their justifications live with the policy in `.claude/rules/providers.md`, not here. Note that crossref's is *resolved at import* from `CROSSREF_MAILTO` rather than being a literal — don't assume a fixed number for it.
3. **Inter-start gap** — `min_gap_seconds` between request *starts*, not durations. The lock is held only to compute the wait and **reserve** the instant this caller will start at; the sleep itself happens *outside* it. That ordering is what makes per-host pacing meaningful — holding the lock across the sleep would block an unrelated host from even computing its own wait. `asyncio.Lock` is FIFO, so acquisition order holds and reserved starts stay spaced by exactly `min_gap_seconds`. `_stats.log_request` fires here.

**`per_host=True` (opt-in)** keys the gap by `urlsplit(url).netloc.lower()` instead of one global timestamp, for a client whose URLs are not a single API — only `oa_download` uses it. The seven API providers each talk to exactly one host, where the map would be a dict of size one, and opting one in would silently widen its documented rate the day it gained a second hostname. `max_concurrent` stays global in both modes: it bounds *our* egress (sockets, fds, simultaneous in-flight streams), not any one host's load. The host map is bounded by an age sweep, which is **exact rather than heuristic** — an entry older than `min_gap_seconds` can never produce a wait, since the gap check treats a missing host identically to an expired one. `reset()` clears the map.

`Throttle.get(client, url, **kw)` is the common case: fire one `_http.get_with_retry` inside `slot(url)` (with `backoff_seconds=max(min_gap_seconds, 1.0)` and `max_attempts=retry_attempts`). `retry_attempts` (constructor arg, default 2 = one retry) is per-provider policy alongside the gap/concurrency caps — arxiv raises it to 3 because its Fastly edge returns 429/503 with no `Retry-After` and one retry tends to land in the same cooldown. `Throttle.reset()` zeroes the counters and rebuilds the loop-bound lock/sem (the conftest fixture calls it between tests).

**`slot(url, count_request=True)` and who counts `http_calls`.** `get` passes `count_request=False` and lets `_http.get_with_retry` count each attempt it actually makes; a raw `slot()` caller (streaming PDF downloads, which hold one slot for exactly one request) keeps the default and is counted here. Do not "deduplicate" the two — counting at slot entry under-reports real outbound volume by up to `max_attempts` (3× for arXiv), which is precisely the number a politeness audit reads.

Each provider keeps thin **module-level wrappers** that delegate to its instance — `_throttled_get` → `_throttle.get` (openalex's is url-only and builds the client internally), `_request_slot` → `_throttle.slot` (arxiv/biorxiv/acl_anthology/oa_download). The wrappers preserve the call sites and the test seams (`monkeypatch.setattr(mod, "_throttled_get"/"_request_slot", ...)`); tests that need to override pacing set `mod._throttle.min_gap_seconds`. Streaming PDF downloads hold `slot(url)` open for the whole stream lifetime — open connections counting toward `max_concurrent` is the correct semantics, since releasing earlier would let a fan-out exceed documented limits while slow streams flush.

The gating behaviour is verified once in `tests/test_throttle.py` (gap, concurrency cap, burst cap, slot lifetime, reset, get) rather than re-tested per provider.

## _stats.py

Per-provider counters (`cache_hits`, `cache_misses`, `negative_hits`, `http_calls`, `http_retries`, `backpressure_refusals`, `cache_write_failures`) plus a live `in_flight` sample drawn from each provider module's `_throttle.pending`.

- `_stats.reset()` zeroes the cumulative counters; the conftest fixture calls it between tests.
- Wired into `cache.get`/`get_negative`, `Throttle.slot` (`backpressure_refusals` on burst-cap refusal; `http_calls` only when `count_request=True`), and `_http.get_with_retry` (`http_calls` per attempt, `http_retries` per retry).
- **`DEBUG_REQUESTS`** flag (`1`/`true`/`yes`/`on`) makes each throttled GET log `[academic-tools] {provider} GET {url} (throttle wait Xs)` to **stderr** (not stdout — MCP speaks JSON-RPC there). Re-read every call so an operator can flip the flag without restarting.

When adding a new provider: append the module name to `_PROVIDER_MODULES` so its `in_flight` count appears in `snapshot()`.
