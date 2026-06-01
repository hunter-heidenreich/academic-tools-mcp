"""Shared per-provider HTTP throttle.

Every API client paces its outbound requests with the same three-layer gating;
this module is its single home (mirroring ``_singleflight.py`` / ``_http.py`` /
``cache.py``). Each provider holds one configured ``Throttle`` instance and
exposes thin ``_throttled_get`` / ``_request_slot`` wrappers that delegate to it.

The *mechanism* is shared; the *policy* is not. Per-provider config
(``max_concurrent``, ``min_gap_seconds``, ``max_pending``) stays declared in
each provider module and is passed at construction — arxiv's single-connection
rule, crossref's polite-pool budget, etc. are deliberate and live next to the
client they govern.

Gating order (see ``slot``):

1. **Burst cap** — ``pending >= max_pending`` raises ``LocalBackpressureError``
   immediately, before any sem/lock acquisition, so the (max_pending+1)-th
   concurrent caller fails fast instead of silently queueing.
2. **Concurrency cap** — an ``asyncio.Semaphore(max_concurrent)`` caps
   simultaneous in-flight requests.
3. **Inter-start gap** — a lock is held only briefly to enforce
   ``min_gap_seconds`` between request *starts* (not durations), then released
   before the actual GET so concurrent in-flight requests don't block each other.

``slot`` is an async context manager so a streaming PDF download can hold the
slot for the whole stream lifetime (open connections counting toward the
concurrency cap is the correct semantics). ``get`` is the common case: fire one
``_http.get_with_retry`` inside the slot and return.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import httpx

from . import _http, _stats


class Throttle:
    """Per-provider request pacing: burst cap, concurrency cap, inter-start gap.

    Owns the mutable runtime state (``pending``, ``last_request_time``, and the
    loop-bound semaphore/lock) so a provider module no longer carries it as
    module-level globals. Construct one per provider; share it across that
    provider's getters and PDF downloads.
    """

    def __init__(
        self,
        *,
        namespace: str,
        label: str,
        max_concurrent: int,
        min_gap_seconds: float,
        max_pending: int = 5,
    ) -> None:
        self.namespace = namespace
        self.label = label
        self.max_concurrent = max_concurrent
        self.min_gap_seconds = min_gap_seconds
        self.max_pending = max_pending
        self.pending = 0
        self.last_request_time: float = 0.0
        self._sem = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()

    def reset(self) -> None:
        """Zero the runtime counters and rebuild the loop-bound primitives.

        ``asyncio.Lock`` / ``Semaphore`` bind to the running event loop on first
        await; a stale instance from a previous loop raises "bound to a different
        event loop" if reused. Tests call this between cases (via the conftest
        fixture) so each test gets fresh primitives and a clean ``pending`` even
        if a prior error path leaked the counter.
        """
        self.pending = 0
        self.last_request_time = 0.0
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def slot(self, url: str):
        """Acquire the rate-limit slot for the lifetime of the with-block.

        Raises ``LocalBackpressureError`` past ``max_pending`` queued callers so
        a fan-out gets fast feedback instead of stacking behind the gap forever.
        """
        if self.pending >= self.max_pending:
            _stats.incr(self.namespace, "backpressure_refusals")
            raise _http.LocalBackpressureError(
                self.label, self.pending, self.max_pending, self.min_gap_seconds
            )
        self.pending += 1
        try:
            async with self._sem:
                async with self._lock:
                    now = time.monotonic()
                    elapsed = now - self.last_request_time
                    wait_seconds = 0.0
                    if self.last_request_time > 0 and elapsed < self.min_gap_seconds:
                        wait_seconds = self.min_gap_seconds - elapsed
                        await asyncio.sleep(wait_seconds)
                    self.last_request_time = time.monotonic()
                _stats.log_request(self.namespace, url, wait_seconds)
                _stats.incr(self.namespace, "http_calls")
                yield
        finally:
            self.pending -= 1

    async def get(self, client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        """Fire one GET (with one transparent retry) inside the slot.

        ``backoff_seconds`` floors the retry sleep at the provider's own gap so
        a retry never violates the documented rate-limit policy.
        """
        async with self.slot(url):
            return await _http.get_with_retry(
                client,
                url,
                backoff_seconds=max(self.min_gap_seconds, 1.0),
                provider=self.namespace,
                **kwargs,
            )
