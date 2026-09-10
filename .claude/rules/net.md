---
paths:
  - "src/academic_tools_mcp/net/*.py"
---

# HTTP: clients, retry, throttling, stats

**Per-function behaviour lives in the docstrings.** This file covers only what no
single docstring can: the cross-module contracts, the upstream facts, and the
edits that look safe and aren't.

## net/clients.py

`aclose_all()`'s bound must stay hard *and* concurrent. The trap is
`asyncio.wait_for` per client: it awaits the coroutine it just cancelled, so it
cannot bound a teardown that keeps awaiting past cancellation. Serial closes have
the other failure — they sum the bound and pin the lifespan.

## net/http.py

- **`addresses_a_record` is necessary, not sufficient, and it tests only the
  *trailing* segment.** A provider that interpolates its identifier anywhere else
  owns a second check: `opencitations._fetch_direction` and
  `openalex._fetch_singleton` test the bare identifier (a `doi:` scheme prefix
  keeps the last segment non-empty); `biorxiv.get_paper` tests for an empty
  segment anywhere, since its identifier sits mid-path. **Deliberately not folded
  in here** — an empty segment is legal in `openalex.get_author`'s URL-shaped
  identifier, which `safe=":/"` exists to preserve, so "no empty segments" is
  per-provider policy, not a shared rule. This is the one home for that argument;
  a provider section states only what its own shortened path lands on.
- **The three-state error vocabulary.** `retryable: True` (transient),
  `not_found: True` (definitive), and an unclassified 4xx carrying neither.
  `retryable is True` is the only test that means "a retry might work" —
  inverting `not_found` collapses three states into two and sends an agent back
  at a call that cannot succeed. Every classifier downstream reads these flags:
  `tools/graph._FORWARDED_ERROR_KEYS`, `tools/paper`'s `fallback_crossref` gate,
  `openaccess`'s import-suggestion allowlist. A hand-spelled dict that omits the
  flag reads to all of them as "unknown".
- **`LocalBackpressureError`: two naming traps.** `max_concurrency` in the error
  dict carries `max_pending`, not `max_concurrent` — it is the burst cap despite
  the name. And `error_dict` takes the provider name off the exception in
  preference to its own `provider` argument, so the throttle's `label` wins over
  the call site's literal when they disagree.
- **`error_dict` clamps the agent-facing `retry_after_seconds` to
  `_MAX_RETRY_AFTER_SECONDS`, the same ceiling `get_with_retry` sleeps under —
  change one, change both.** Both bounds are pinned as properties, so widening
  either fails without a new example.

## net/throttle.py — the shared `Throttle`

Every numeric argument is clamped in `__init__` because nothing validates a
provider's constants and the failure modes are silent: `Semaphore(0)` waits
forever with no timeout, `max_pending=0` refuses every caller.

- **The lock is held only to compute and reserve this caller's start; the sleep
  happens outside it.** Moving the sleep inside collapses per-host pacing back to
  one global rate — host A's sleep would block host B from even computing its
  own wait.
- **`pending` counts in-flight *plus* queued callers.** With
  `max_concurrent=4, max_pending=5`, exactly one caller can be waiting before the
  sixth is refused.
- **`per_host=True` is opt-in for a reason.** Only `openaccess` uses it
  (`.claude/rules/download.md` says why). Opting in a single-host provider would
  silently widen its documented rate the day it gained a second hostname.
  `max_concurrent` stays global in both modes: it bounds *our* egress — sockets,
  fds, simultaneous in-flight streams — not any one host's load.
- **The last-start map's two prune branches are both load-bearing.** Global mode
  is the degenerate single-key case, which is why the age sweep never fires
  there; a fan-out that leaves every entry fresh falls back to dropping the
  oldest, at a cost of one request starting early.

Each provider keeps thin module-level wrappers (`_throttled_get`,
`_request_slot`) that exist to preserve the test seams: tests monkeypatch those
names, and override pacing via `mod._throttle.min_gap_seconds`. crossref adds
`_throttled_search_get` with its own `reset_search_pacing()`, which the conftest
fixture must also call.

## net/stats.py

- **A quota is observed, never assumed.** Only OpenAlex sends `X-RateLimit-*`, so
  no header, no deadline and an elapsed deadline all read as *proceed* — refusing
  on ignorance would strand every provider that publishes nothing.
- **`_quota_dict`'s `retry_after_seconds` escapes `_MAX_RETRY_AFTER_SECONDS`.**
  That ceiling bounds a sleep, and a quota refusal never sleeps; clamping an
  hours-away refill to 10 minutes advertises a retry that cannot succeed.
- **`env_file` rides the `ENABLE_DEBUG_TOOLS` gate**, so exposing it changes no
  agent-facing surface — a config path is operator data.
- **`DEBUG_REQUESTS` is re-read per call**, through `config.flag`, so it flips
  without a restart.
- **A failed disk write is `cache_write_failures`, wherever it happens** —
  `cache.put` / `put_negative`, `streaming.stream_to_file`,
  `manual.import_local_pdf`. A PDF is the largest write the server makes;
  leaving it uncounted hid a full disk from the one counter that exists to show it.
