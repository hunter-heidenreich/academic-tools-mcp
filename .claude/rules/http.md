---
paths:
  - "src/academic_tools_mcp/_http.py"
  - "src/academic_tools_mcp/_throttle.py"
  - "src/academic_tools_mcp/_clients.py"
  - "src/academic_tools_mcp/_stats.py"
---

# HTTP: clients, retry, throttling, stats

## _clients.py

`get_client(name)` is a lazy per-provider singleton over one shared pool config. **A second call with the same `name` silently ignores its kwargs** — headers and timeout are configured in exactly one place per provider; passing different ones later is a no-op, not an override.

`aclose_all()` (wired into `_app._lifespan`) must stay **concurrent**, with `_ACLOSE_TIMEOUT_SECONDS` bounding the whole set: serial closes sum that bound and pin the lifespan. The bound must also stay **hard** — `asyncio.wait` + cancel the stragglers, never `asyncio.wait_for` per client, which awaits the coroutine it just cancelled and so cannot bound a teardown that keeps awaiting past cancellation. Stragglers are deliberately left unawaited; the process is exiting. `CancelledError` must keep propagating — the best-effort `except Exception` around each close deliberately does not catch it.

## _http.py

- `HTTPX_ERRORS` — the except tuple every client wraps its request block in. It includes `LocalBackpressureError`, so a local refusal reaches the agent through the same `error_dict` contract as an upstream failure.
- `error_dict(provider, exc)` — converts exceptions to `{error, retry_after_seconds?, retryable?, backpressure?}` dicts with provider-aware messages.
- `parse_error_dict` / `JSON_PARSE_ERRORS` — the single home for "unparseable 200 body". Always `retryable: True`, never negative-cached; a fresh dict per call so a single-flight follower can't mutate a shared object.
- `LocalBackpressureError` — raised by `Throttle.slot` when `pending >= max_pending`, carrying the throttle's `label` (not its namespace) as the agent-facing provider name. Surfaces as `{error, retryable: True, backpressure: True, max_concurrency, retry_after_seconds?}`. **`max_concurrency` carries `max_pending`, not `max_concurrent`** — it is the burst cap, despite the name. `retry_after_seconds` is omitted entirely when the throttle's `min_gap_seconds` is 0, since "retry after 0s" is not advice.
- `get_with_retry` — retries timeouts, network errors, and `_RETRYABLE_STATUSES` (an explicit allowlist, **not** a 5xx range). The sleep *after* a failed attempt *n* is `min(max(Retry-After, backoff_seconds * 2**(n-1)), _MAX_RETRY_AFTER_SECONDS)` — the first retry waits exactly `backoff_seconds`, the second twice that. On the transport-exception path there is no response, so only the backoff term applies. `Throttle.get` supplies `backoff_seconds` as the provider's own gap floored at one second, so a retry can never undercut the documented rate; `_MAX_RETRY_AFTER_SECONDS` stops a misconfigured `Retry-After` pinning the throttle for hours. `Retry-After` is honoured on any retryable status, not just 429/503, in both the delay-seconds and HTTP-date forms RFC 9110 permits. **`error_dict`'s 429 branch clamps the agent-facing `retry_after_seconds` to that same ceiling — change one, change both**.

## _throttle.py — the shared `Throttle`

Each provider declares its *policy* constants (`_MAX_CONCURRENT`, `_MIN_REQUEST_GAP`, `_MAX_PENDING`) and constructs one keyword-only `Throttle(...)` from them, plus optional `retry_attempts` / `per_host`. Two more arguments are required and neither is cosmetic: **`namespace` must equal the module's cache namespace** or its HTTP and cache counters split into two rows in `snapshot()`, and **`label` is the human-facing provider name** that reaches the agent in a `LocalBackpressureError`. The instance owns the mutable runtime state (`pending`, `last_request_time`, and a loop-bound semaphore + lock); the constants themselves live in each provider module (`.claude/rules/providers.md` for the policy behind them).

`Throttle.slot(url)` is an `@asynccontextmanager`; the gating order (burst cap → concurrency cap → inter-start gap) is in the module docstring. Two things are fragile under edit:

- **The lock is held only to compute the wait and *reserve* the instant this caller will start at — the sleep happens outside it.** Moving the sleep inside collapses per-host pacing back to one global rate, because host A's sleep would block host B from even computing its own wait. `_stats.log_request` fires after the sleep, inside the slot.
- **`pending` counts in-flight *plus* queued callers** — `slot` increments it before acquiring the semaphore — so `max_pending` is total admitted callers, not queue depth. With `max_concurrent=4, max_pending=5` exactly one caller can be waiting before the sixth is refused.

**`per_host=True` (opt-in)** keys the gap by `urlsplit(url).netloc.lower()` instead of one global timestamp — only `oa_download` uses it (`.claude/rules/pdf-download.md` says why). Opting in a single-host provider would silently widen its documented rate the day it gained a second hostname. `max_concurrent` stays global in both modes: it bounds *our* egress (sockets, fds, simultaneous in-flight streams), not any one host's load.

The per-host map is bounded past `_MAX_TRACKED_HOSTS`. The age sweep is semantics-preserving — an entry older than `min_gap_seconds` can never produce a wait, since the gap check treats a missing host identically to an expired one — but a fan-out that leaves every entry fresh falls back to dropping the oldest anyway, at a cost of one request starting early. **Both branches are load-bearing**.

**Who counts `http_calls`.** `Throttle.get` passes `count_request=False` and lets `get_with_retry` count each attempt; a raw `slot()` caller (a streaming PDF download, one slot = one request) keeps the default. Do not "deduplicate" the two — counting at slot entry under-reports outbound volume by a factor of up to `max_attempts`, the number a politeness audit reads.

Each provider keeps thin **module-level wrappers** that delegate to its instance — `_throttled_get` → `_throttle.get` (openalex's is url-only and builds the client internally), `_request_slot` → `_throttle.slot` (arxiv/biorxiv/acl_anthology/oa_download). The wrappers preserve the call sites and the test seams (`monkeypatch.setattr(mod, "_throttled_get"/"_request_slot", ...)`); tests that need to override pacing set `mod._throttle.min_gap_seconds`. crossref adds a third, `_throttled_search_get` — a lock-based gate riding on top of the shared slot (policy in `providers.md`) with its own `reset_search_pacing()`, which the conftest fixture must also call.

`Throttle.reset()` zeroes the counters and rebuilds the loop-bound lock/sem, which the conftest fixture calls between tests.

## _stats.py

Per-provider counters plus a live `in_flight` sample read from each provider module's `_throttle.pending` (`snapshot()` documents the shape).

- **`DEBUG_REQUESTS`** logs each throttled GET to **stderr** — never stdout, which carries the JSON-RPC stream. Re-read per call, so it flips without a restart.

When adding a new provider: append the module path to `_PROVIDER_MODULES` **and** to `_reset_pooled_state` in `tests/conftest.py`. The two lists must stay in sync and nothing enforces it.
