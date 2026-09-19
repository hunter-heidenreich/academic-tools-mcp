---
paths:
  - "src/academic_tools_mcp/net/*.py"
---

# HTTP: clients, retry, throttling, stats

## clients.py

**`aclose_all()`'s bound must stay hard *and* concurrent.** The trap is
`asyncio.wait_for` per client: it awaits the coroutine it just cancelled, so it
cannot bound a teardown that keeps awaiting past cancellation. Serial closes have
the other failure — they sum the bound and pin the lifespan.

## http.py

- **The error vocabulary has three states**: `retryable: True` (transient),
  `not_found: True` (definitive), and an unclassified 4xx carrying neither.
  Neither `error_dict` nor `response_error_dict` emits a fourth — an explicit
  `retryable: False` is a deliberate definitive verdict a caller writes, and
  `tools/search` branches on it.
  **`retryable is True` is the only test meaning "a retry might work"**; inverting
  `not_found` collapses three states into two.
- **Every downstream classifier reads these flags** — `tools/graph._FORWARDED_ERROR_KEYS`,
  `tools/paper`'s `fallback_crossref` gate, `openaccess`'s import-suggestion
  allowlist. A hand-spelled dict that omits one reads to all three as "unknown".
- **`max_concurrency` in the backpressure dict carries `max_pending`.** It is the
  burst cap despite the name, and nothing but this line says so.

## throttle.py

- **Moving the sleep inside the lock collapses per-host pacing back to one global
  rate** — host A's sleep would block host B from even computing its own wait.
- **`per_host=True` is opt-in for a reason.** Only `openaccess` uses it; opting in
  a single-host provider would silently widen its documented rate the day it gained
  a second hostname. **`max_concurrent` stays global in both modes**: it bounds
  *our* egress — sockets, fds, in-flight streams — not any one host's load.

**Test seams:** the thin module-level wrappers exist because tests monkeypatch
those names, and override pacing through `mod._throttle.min_gap_seconds` or
`mod._search_gap.min_gap_seconds`.

## stats.py

- **A quota is observed, never assumed.** Only OpenAlex sends `X-RateLimit-*`, so
  no header, no deadline and an elapsed deadline all read as *proceed* — refusing
  on ignorance would strand every provider that publishes nothing.
- **An armed lockout refuses the requests whose response would clear it**, so
  gating a free class strands it for the whole window rather than until the next
  reply. This is why `admit(metered=)` exempts the zero-priced classes.
- **A failed disk write is `cache_write_failures`, wherever it happens** —
  `cache.put` / `put_negative`, `streaming.stream_to_file`, `manual.import_local_pdf`.
  A PDF is the largest write the server makes; leaving it uncounted hid a full disk
  from the one counter that exists to show it.
