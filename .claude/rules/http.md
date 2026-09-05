---
paths:
  - "src/academic_tools_mcp/_http.py"
  - "src/academic_tools_mcp/_throttle.py"
  - "src/academic_tools_mcp/_clients.py"
  - "src/academic_tools_mcp/_stats.py"
---

# HTTP: clients, retry, throttling, stats

## _clients.py

`get_client(name)` returns a per-provider lazy singleton `httpx.AsyncClient` sharing one pool config, so a multi-call session pays one TCP+TLS handshake instead of one per request. Headers (polite-pool UA) and timeout are baked in at construction, and **a second call with the same `name` silently ignores its kwargs** — a provider configures its client in exactly one place.

`aclose_all()` (wired into `_app._lifespan`) closes every pooled client **concurrently**, each bounded by `_ACLOSE_TIMEOUT_SECONDS`. Serial closes sum those bounds — eight wedged clients would pin the lifespan for 8× the timeout, which is exactly what the per-client bound exists to prevent.

## _http.py

- `HTTPX_ERRORS` — the except tuple every client wraps its request block in. It includes `LocalBackpressureError`, so a local refusal reaches the agent through the same `error_dict` contract as an upstream failure.
- `error_dict(provider, exc)` — converts exceptions to `{error, retry_after_seconds?, retryable?, backpressure?}` dicts with provider-aware messages.
- `LocalBackpressureError(provider, pending, max_pending, min_gap_seconds=0.0)` — raised by `Throttle.slot` when `pending >= max_pending`. Surfaces as `{error, retryable: True, backpressure: True, max_concurrency, retry_after_seconds?}`. **`max_concurrency` carries `max_pending`, not `max_concurrent`** — it is the burst cap, despite the name. `retry_after_seconds` only appears when the provider has a documented gap (omitted for ACL Anthology, whose `min_gap_seconds` is 0).
- `get_with_retry` — GET with transparent retries on timeouts, network errors, and `_RETRYABLE_STATUSES` (an explicit allowlist, **not** a 5xx range). The sleep before attempt *n* is `min(max(Retry-After, backoff_seconds * 2**(n-1)), _MAX_RETRY_AFTER_SECONDS)`: the provider's own throttle gap is the floor, so a retry can never undercut its documented rate, and the ceiling stops a misconfigured `Retry-After` pinning the throttle for hours. `Retry-After` is honoured on any retryable status, not just 429/503. `error_dict`'s 429 branch clamps the agent-facing `retry_after_seconds` to that same ceiling — change one, change both. `max_attempts` comes from `Throttle.retry_attempts`; at the default only one sleep happens, so the exponential term matters only for a provider that raises it.

## _throttle.py — the shared `Throttle`

Each provider declares only its *policy* constants (`_MAX_CONCURRENT`, `_MIN_REQUEST_GAP`, `_MAX_PENDING`) and constructs one keyword-only `Throttle(...)` from them, plus optional `retry_attempts` / `per_host`. The instance owns the mutable runtime state (`pending`, `last_request_time`, and a loop-bound semaphore + lock). Values and their justifications live with the policy in `.claude/rules/providers.md`, not here.

`Throttle.slot(url)` (an `@asynccontextmanager`) enforces three layers in order:

1. **Burst cap** — `pending >= max_pending` raises `LocalBackpressureError` immediately, before any sem/lock acquisition, so an over-cap caller fails fast instead of silently queueing.
2. **Concurrency cap** — an `asyncio.Semaphore(max_concurrent)` caps simultaneous in-flight requests.
3. **Inter-start gap** — `min_gap_seconds` between request *starts*, not durations. The lock is held only to compute the wait and **reserve** the instant this caller will start at; the sleep itself happens *outside* it. That ordering is what makes per-host pacing meaningful — holding the lock across the sleep would block an unrelated host from even computing its own wait. `asyncio.Lock` is FIFO, so acquisition order holds and reserved starts stay at least `min_gap_seconds` apart. `_stats.log_request` fires here.

**`per_host=True` (opt-in)** keys the gap by `urlsplit(url).netloc.lower()` instead of one global timestamp — only `oa_download` uses it (`providers.md` says why). Opting in a single-host provider would silently widen its documented rate the day it gained a second hostname. `max_concurrent` stays global in both modes: it bounds *our* egress (sockets, fds, simultaneous in-flight streams), not any one host's load.

The per-host map is bounded past `_MAX_TRACKED_HOSTS`. The age sweep is semantics-preserving — an entry older than `min_gap_seconds` can never produce a wait, since the gap check treats a missing host identically to an expired one — but a fan-out that leaves every entry fresh falls back to dropping the oldest anyway, at a cost of one request starting early. **Both branches are load-bearing**; `tests/test_throttle.py::test_host_map_is_bounded_even_when_every_entry_is_fresh` pins the second.

`Throttle.get(client, url, **kw)` is the common case: fire one `_http.get_with_retry` inside `slot(url)`. `retry_attempts` is per-provider policy alongside the gap and concurrency caps (`providers.md`). `Throttle.reset()` zeroes the counters and rebuilds the loop-bound lock/sem, which the conftest fixture calls between tests.

**Who counts `http_calls`.** `get` passes `count_request=False` and lets `get_with_retry` count each attempt; a raw `slot()` caller (a streaming PDF download, one slot = one request) keeps the default. Do not "deduplicate" the two — counting at slot entry under-reports outbound volume by up to `max_attempts`, the number a politeness audit reads.

Each provider keeps thin **module-level wrappers** that delegate to its instance — `_throttled_get` → `_throttle.get` (openalex's is url-only and builds the client internally), `_request_slot` → `_throttle.slot` (arxiv/biorxiv/acl_anthology/oa_download). The wrappers preserve the call sites and the test seams (`monkeypatch.setattr(mod, "_throttled_get"/"_request_slot", ...)`); tests that need to override pacing set `mod._throttle.min_gap_seconds`. Streaming PDF downloads hold `slot(url)` open for the whole stream lifetime — open connections counting toward `max_concurrent` is the correct semantics, since releasing earlier would let a fan-out exceed documented limits while slow streams flush.

The gating behaviour is verified once in `tests/test_throttle.py` — including the per-host keying and map bounding — rather than re-tested per provider.

## _stats.py

Per-provider counters plus a live `in_flight` sample read from each provider module's `_throttle.pending` (`snapshot()` documents the shape).

- **`DEBUG_REQUESTS`** logs each throttled GET to **stderr** — never stdout, which carries the JSON-RPC stream. Re-read per call, so it flips without a restart.

When adding a new provider: append the module path to `_PROVIDER_MODULES` **and** to `_reset_pooled_state` in `tests/conftest.py`. The two lists must stay in sync and nothing enforces it.
