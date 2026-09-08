"""Shared per-provider HTTP throttle.

The single home for outbound pacing (mirroring ``_singleflight.py`` /
``_http.py`` / ``cache.py``). Each provider holds one configured ``Throttle``
and exposes thin ``_throttled_get`` / ``_request_slot`` wrappers over it: the
*mechanism* is shared, the policy is passed at construction.

Gating order (see ``slot``):

1. **Burst cap** — ``pending >= max_pending`` raises ``LocalBackpressureError``
   before any sem/lock acquisition, so a fan-out fails fast instead of
   silently queueing.
2. **Concurrency cap** — ``asyncio.Semaphore(max_concurrent)``.
3. **Inter-start gap** — a lock held just long enough to pace request *starts*
   (not durations) by ``min_gap_seconds``, released before the GET.

``slot`` is an async context manager so a streaming PDF download can hold it
for the whole stream, its open connection counting against the concurrency cap.

Rationale — sleep outside the lock, who counts ``http_calls``, when
``per_host`` applies — is in ``.claude/rules/http.md``.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import _http, _stats

# A bound on a pathological walk, far above the tens of publisher domains one
# session actually sees — not a tuning knob.
_MAX_TRACKED_HOSTS = 512

_GLOBAL_KEY = ""


class Throttle:
    """Per-provider request pacing: burst cap, concurrency cap, inter-start gap."""

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
        # Clamped, not trusted: a typo'd policy constant fails silently —
        # ``Semaphore(0)`` waits forever, ``max_pending=0`` refuses everyone.
        self.max_concurrent = max(1, max_concurrent)
        self.min_gap_seconds = max(0.0, min_gap_seconds)
        self.max_pending = max(1, max_pending)
        self.per_host = per_host
        # Total attempts, not retries: 2 is one original plus one retry.
        self.retry_attempts = max(1, retry_attempts)
        self.pending = 0
        self._last_start: dict[str, float] = {}
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._lock = asyncio.Lock()

    def _key(self, url: str) -> str:
        # Lowercased: RFC 3986 makes netloc case-insensitive and OpenAlex's is
        # inconsistent. Netloc, not hostname: two ports are two services.
        return urlsplit(url).netloc.lower() if self.per_host else _GLOBAL_KEY

    def _prune(self, now: float) -> None:
        """Bound the last-start map. Called under ``_lock``.

        Invariant: a swept entry could not have produced a wait at ``now``, so
        ``now`` is the real clock, never a caller's reserved (future) start.
        Past the cap with nothing expired, dropping the oldest costs one early
        request.
        """
        if len(self._last_start) <= _MAX_TRACKED_HOSTS:
            return
        cutoff = now - self.min_gap_seconds
        self._last_start = {k: t for k, t in self._last_start.items() if t > cutoff}
        overflow = len(self._last_start) - _MAX_TRACKED_HOSTS
        if overflow > 0:
            for key, _ in sorted(self._last_start.items(), key=lambda kv: kv[1])[:overflow]:
                del self._last_start[key]

    def reset(self) -> None:
        """Zero the runtime state and rebuild the loop-bound primitives.

        ``asyncio.Lock`` / ``Semaphore`` bind to the running event loop on first
        await, so one held over from a previous loop raises "bound to a
        different event loop". The conftest fixture calls this between tests.
        """
        self.pending = 0
        self._last_start = {}
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def slot(self, url: str, *, count_request: bool = True) -> AsyncIterator[None]:
        """Acquire the rate-limit slot for the lifetime of the with-block.

        Raises ``LocalBackpressureError`` past ``max_pending`` callers, so a
        fan-out gets fast feedback instead of stacking behind the gap.

        ``count_request`` records one ``http_calls`` — right for a streaming
        download, one slot per request. ``get`` passes ``False`` and lets
        ``get_with_retry`` count the attempts it actually makes.
        """
        if self.pending >= self.max_pending:
            _stats.incr(self.namespace, "backpressure_refusals")
            raise _http.LocalBackpressureError(
                self.label, self.pending, self.max_pending, self.min_gap_seconds
            )
        self.pending += 1
        try:
            async with self._sem:
                key = self._key(url)
                async with self._lock:
                    now = time.monotonic()
                    last = self._last_start.get(key)
                    wait_seconds = (
                        0.0 if last is None else max(0.0, self.min_gap_seconds - (now - last))
                    )
                    # A future instant, reserved: it lets the sleep sit outside
                    # the lock without two callers picking the same start.
                    self._last_start[key] = now + wait_seconds
                    self._prune(now)
                if wait_seconds:
                    await asyncio.sleep(wait_seconds)
                _stats.log_request(self.namespace, url, wait_seconds)
                if count_request:
                    _stats.incr(self.namespace, "http_calls")
                yield
        finally:
            self.pending -= 1

    async def get(self, client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        """Fire one GET inside the slot, retried per ``retry_attempts``.

        The backoff floor is the provider's own gap, so a retry cannot undercut
        the documented rate.
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
