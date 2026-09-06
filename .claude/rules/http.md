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
- `error_dict(provider, exc)` — converts exceptions to `{error, retryable?, retry_after_seconds?, backpressure?}` dicts with provider-aware messages. **Every transient branch (429, 5xx, timeout, network, backpressure) sets `retryable: True`; "other 4xx" is left unflagged and nothing here sets `retryable: False`.** Callers branch on the key, not on the "Transient — retry." prose. `retry_after_seconds` is read on **any** transient status, matching `get_with_retry`.
- `parse_error_dict` / `JSON_PARSE_ERRORS` — the single home for "unparseable 200 body". Always `retryable: True`, never negative-cached; a fresh dict per call so a single-flight follower can't mutate a shared object. A non-JSON provider specializes the message through `detail`, never a local copy of the shape.
- `LocalBackpressureError` — raised by `Throttle.slot` when `pending >= max_pending`, carrying the throttle's `label` (not its namespace) as the agent-facing provider name. **`error_dict` takes that name off the exception in preference to its own `provider` argument**, so `label` wins over the call site's literal when they disagree. Surfaces as `{error, retryable: True, backpressure: True, max_concurrency, retry_after_seconds?}`. **`max_concurrency` carries `max_pending`, not `max_concurrent`** — it is the burst cap, despite the name. `retry_after_seconds` is omitted entirely when the throttle's `min_gap_seconds` is 0, since "retry after 0s" is not advice.
- `get_with_retry` — retries timeouts, network errors, and `_RETRYABLE_STATUSES` (an explicit allowlist, **not** a 5xx range; 501/505/507 are excluded on purpose). **That frozenset is the single definition of "transient status" — `error_dict` classifies from it too**, so the flag an agent reads and the retry the client actually performs cannot drift apart. `max_attempts` is clamped to at least 1 — a skipped loop returns an unbound `response`, and the `NameError` that follows is not in `HTTPX_ERRORS`. The sleep *after* a failed attempt *n* is `min(max(Retry-After, backoff_seconds * 2**(n-1)), _MAX_RETRY_AFTER_SECONDS)` — the first retry waits exactly `backoff_seconds`, the second twice that. On the transport-exception path there is no response, so only the backoff term applies. `Throttle.get` supplies `backoff_seconds` as the provider's own gap floored at one second, so a retry can never undercut the documented rate; `_MAX_RETRY_AFTER_SECONDS` stops a misconfigured `Retry-After` pinning the throttle for hours. `Retry-After` is honoured on any retryable status, not just 429/503, in both the delay-seconds and HTTP-date forms RFC 9110 permits. **`error_dict` clamps the agent-facing `retry_after_seconds` to that same ceiling — change one, change both**. Both bounds are pinned as properties, so widening either fails without a new example.

## _throttle.py — the shared `Throttle`

Each provider declares its *policy* constants (`_MAX_CONCURRENT`, `_MIN_REQUEST_GAP`, `_MAX_PENDING`) and constructs one keyword-only `Throttle(...)` from them, plus optional `retry_attempts` / `per_host`. Every numeric argument is **clamped in `__init__`** (`max_concurrent` / `max_pending` / `retry_attempts` at 1, the gap at 0), as `get_with_retry` clamps `max_attempts`: nothing validates a provider's constants and the failure modes are silent — `Semaphore(0)` waits forever with no timeout, `max_pending=0` refuses every caller. Two more arguments are required and neither is cosmetic: **`namespace` must equal the module's cache namespace** or its HTTP and cache counters split into two rows in `snapshot()`, and **`label` is the human-facing provider name** that reaches the agent in a `LocalBackpressureError`. The instance owns the mutable runtime state (`pending`, the last-start map, and a loop-bound semaphore + lock); the constants themselves live in each provider module (`.claude/rules/providers.md` for the policy behind them).

`Throttle.slot(url)` is an `@asynccontextmanager`; the gating order (burst cap → concurrency cap → inter-start gap) is in the module docstring. Two things are fragile under edit:

- **The lock is held only to compute the wait and *reserve* the instant this caller will start at — the sleep happens outside it.** Moving the sleep inside collapses per-host pacing back to one global rate, because host A's sleep would block host B from even computing its own wait. `_stats.log_request` fires after the sleep, inside the slot.
- **`pending` counts in-flight *plus* queued callers** — `slot` increments it before acquiring the semaphore — so `max_pending` is total admitted callers, not queue depth. With `max_concurrent=4, max_pending=5` exactly one caller can be waiting before the sixth is refused.

**`per_host=True` (opt-in)** keys the gap by `urlsplit(url).netloc.lower()` instead of one global timestamp — only `oa_download` uses it (`.claude/rules/pdf-download.md` says why). Opting in a single-host provider would silently widen its documented rate the day it gained a second hostname. `max_concurrent` stays global in both modes: it bounds *our* egress (sockets, fds, simultaneous in-flight streams), not any one host's load.

The last-start map is one dict in both modes — global mode is the degenerate single-key case, which is why the prune never fires there — and is bounded past `_MAX_TRACKED_HOSTS`. The age sweep is semantics-preserving — an entry older than `min_gap_seconds` can never produce a wait, since the gap check treats a missing host identically to an expired one — but a fan-out that leaves every entry fresh falls back to dropping the oldest anyway, at a cost of one request starting early. **Both branches are load-bearing**. `_prune` must be handed the **real clock**, never the caller's reserved (`now + wait`) start: sweeping against a future instant drops every entry in `(now - gap, now + wait - gap]`, each of which would still have paced a caller arriving now.

**Who counts `http_calls`.** `Throttle.get` passes `count_request=False` and lets `get_with_retry` count each attempt; a raw `slot()` caller (a streaming PDF download, one slot = one request) keeps the default. Do not "deduplicate" the two — counting at slot entry under-reports outbound volume by a factor of up to `max_attempts`, the number a politeness audit reads.

Each provider keeps thin **module-level wrappers** that delegate to its instance — `_throttled_get` → `_throttle.get` (openalex's is url-only and builds the client internally), `_request_slot` → `_throttle.slot` (arxiv/biorxiv/acl_anthology/oa_download). The wrappers preserve the call sites and the test seams (`monkeypatch.setattr(mod, "_throttled_get"/"_request_slot", ...)`); tests that need to override pacing set `mod._throttle.min_gap_seconds`. crossref adds a third, `_throttled_search_get` — a lock-based gate riding on top of the shared slot (policy in `providers.md`) with its own `reset_search_pacing()`, which the conftest fixture must also call.

`Throttle.reset()` zeroes the counters and rebuilds the loop-bound lock/sem, which the conftest fixture calls between tests.

## _stats.py

Per-provider counters plus a live `in_flight` sample read from the `Throttle.pending` of every throttle a module holds (`snapshot()` documents the shape).

- **`throttles()` discovers by scanning `sys.modules`, not from a list of module paths.** A new provider is sampled the moment it is imported, and `snapshot()` stays a read — importing a module in order to measure it would report in-flight rows for providers the process never used. `tests/conftest.py` resets through the same seam, so there is no second list to keep in sync. It scans **every module attribute, matched on the type's name** (an `isinstance` would close an import cycle; a `hasattr` shape check matches any module-level `MagicMock` a test leaves behind), and dedupes by identity — so a module that grows a *second* throttle cannot drop out of the reset seam, and `snapshot()` **sums** `in_flight` per namespace rather than assigning it.
- **`in_flight` is filed under `throttle.namespace`**, the same string the module's cache and HTTP counters use, so a provider can never split into two rows. That `Throttle(namespace=…)` equals the module's own `NAMESPACE` is pinned by a test, not by convention.
- **`DEBUG_REQUESTS`** logs each throttled GET to **stderr** — never stdout, which carries the JSON-RPC stream. Parsed by `config.flag`, the single home for env-var truthiness, and re-read per call so it flips without a restart.
- **A failed disk write is `cache_write_failures`, wherever it happens** — `cache.put` / `put_negative`, `_pdf_download.stream_to_file`, `manual.import_local_pdf`. A PDF is the largest write the server makes; leaving it uncounted hid a full disk from the one counter that exists to show it.
