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
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import _http, _stats

# Cap on the per-host last-start map (``per_host=True`` only). Publisher
# domains seen in one session number in the tens; this is far above that and
# bounds a pathological walk.
_MAX_TRACKED_HOSTS = 512


class Throttle:
    """Per-provider request pacing: burst cap, concurrency cap, inter-start gap.

    Owns the mutable runtime state (``pending``, ``last_request_time``, and the
    loop-bound semaphore/lock). Construct one per provider; share it across that
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
        retry_attempts: int = 2,
        per_host: bool = False,
    ) -> None:
        self.namespace = namespace
        self.label = label
        self.max_concurrent = max_concurrent
        self.min_gap_seconds = min_gap_seconds
        self.max_pending = max_pending
        # Pace by request host rather than one global timestamp. Opt-in, for a
        # client whose URLs are not a single API: ``oa_download`` resolves DOIs
        # to arbitrary publisher CDNs, and a reference walk through one journal
        # lands many of them on the same domain. The seven API providers each
        # talk to exactly one host, where a per-host map would be a dict of
        # size one — they keep the single timestamp.
        self.per_host = per_host
        # Total attempts (1 original + N-1 retries) for ``get``. Default 2 = one
        # transparent retry; a provider behind a flakier edge (arXiv's Fastly
        # 429/503-without-Retry-After) raises it so ``get_with_retry``'s
        # exponential backoff can ride out a short cooldown.
        self.retry_attempts = retry_attempts
        self.pending = 0
        self.last_request_time: float = 0.0
        self._host_last_start: dict[str, float] = {}
        self._sem = asyncio.Semaphore(max_concurrent)
        self._lock = asyncio.Lock()

    def _last_start(self, key: str) -> float:
        if not self.per_host:
            return self.last_request_time
        return self._host_last_start.get(key, 0.0)

    def _record_start(self, key: str, when: float) -> None:
        if not self.per_host:
            self.last_request_time = when
            return
        self._host_last_start[key] = when
        self._prune_hosts(when)

    def _prune_hosts(self, now: float) -> None:
        """Bound the per-host map. Called under ``_lock``, per_host mode only.

        An entry older than ``min_gap_seconds`` can never produce a wait — the
        gap check treats a missing host exactly like an expired one — so the
        age sweep is semantics-preserving rather than a heuristic. This is why
        it needs none of ``papers.sections_lock``'s machinery: that skips
        currently-held entries because evicting a held lock destroys mutual
        exclusion a live writer depends on, and a timestamp has no such hazard.

        If everything is still inside the window (a fan-out wider than the
        cap), drop the oldest — they are nearest expiry, and the cost is at
        most one request starting early.
        """
        if len(self._host_last_start) <= _MAX_TRACKED_HOSTS:
            return
        cutoff = now - self.min_gap_seconds
        self._host_last_start = {h: t for h, t in self._host_last_start.items() if t > cutoff}
        overflow = len(self._host_last_start) - _MAX_TRACKED_HOSTS
        if overflow > 0:
            oldest = sorted(self._host_last_start.items(), key=lambda kv: kv[1])[:overflow]
            for host, _ in oldest:
                del self._host_last_start[host]

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
        self._host_last_start = {}
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def slot(self, url: str, *, count_request: bool = True) -> AsyncIterator[None]:
        """Acquire the rate-limit slot for the lifetime of the with-block.

        Raises ``LocalBackpressureError`` past ``max_pending`` queued callers so
        a fan-out gets fast feedback instead of stacking behind the gap forever.

        ``count_request`` records one ``http_calls``. It is the right unit for a
        streaming PDF download, which holds the slot for exactly one request.
        ``get`` passes ``False`` because ``get_with_retry`` counts each attempt
        it actually makes — a slot can issue several.
        """
        if self.pending >= self.max_pending:
            _stats.incr(self.namespace, "backpressure_refusals")
            raise _http.LocalBackpressureError(
                self.label, self.pending, self.max_pending, self.min_gap_seconds
            )
        self.pending += 1
        try:
            async with self._sem:
                # Netloc is case-insensitive per RFC 3986 and OpenAlex-supplied
                # URLs are inconsistently cased; the port stays in the key,
                # since one host on two ports is two services.
                key = urlsplit(url).netloc.lower() if self.per_host else ""
                async with self._lock:
                    now = time.monotonic()
                    last = self._last_start(key)
                    wait_seconds = 0.0
                    if last > 0 and now - last < self.min_gap_seconds:
                        wait_seconds = self.min_gap_seconds - (now - last)
                    # Reserve the instant we are about to start at *before*
                    # releasing the lock, so a concurrent caller for this key
                    # paces against the slot we just took rather than the
                    # previous one. This is what lets the sleep happen outside
                    # the lock: holding it across the sleep would serialise
                    # unrelated hosts and make per-host pacing pointless.
                    # Acquisition order is unchanged (asyncio.Lock is FIFO) and
                    # reserved starts stay spaced by exactly min_gap_seconds,
                    # so observable pacing is identical when per_host is off.
                    self._record_start(key, now + wait_seconds)
                if wait_seconds:
                    await asyncio.sleep(wait_seconds)
                _stats.log_request(self.namespace, url, wait_seconds)
                if count_request:
                    _stats.incr(self.namespace, "http_calls")
                yield
        finally:
            self.pending -= 1

    async def get(self, client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        """Fire one GET (with one transparent retry) inside the slot.

        ``backoff_seconds`` floors the retry sleep at the provider's own gap so
        a retry never violates the documented rate-limit policy.
        """
        async with self.slot(url, count_request=False):
            return await _http.get_with_retry(
                client,
                url,
                max_attempts=self.retry_attempts,
                backoff_seconds=max(self.min_gap_seconds, 1.0),
                provider=self.namespace,
                **kwargs,
            )
