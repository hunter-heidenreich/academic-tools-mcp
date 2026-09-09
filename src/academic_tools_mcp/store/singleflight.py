"""Single-flight: collapse N concurrent calls for the same key into one.

The hazard: the four unified paper tools called in parallel for one ID all miss
the cache, and the throttle only paces them — it releases between requests — so
without a shared slot three re-fetch what the first wrote.
"""

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, TypeVar

_T = TypeVar("_T")

# Cancelled leaders one caller follows before running the factory itself,
# unslotted — an anti-spin bound, not a tuned policy.
_MAX_FOLLOW_ATTEMPTS = 3


def _self_is_cancelling() -> bool:
    """Whether the *current* task is the one being cancelled.

    ``Task.cancelling()`` counts cancellation requests against this task, so
    it is what separates "someone cancelled me" from "I caught a
    CancelledError belonging to another task's future".
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class SingleFlight:
    """Coalesce concurrent calls keyed by a hashable identifier.

    Absent cancellation the factory runs once per key *per in-flight window*: the
    slot is dropped when the future resolves, so the next call re-runs it. This is
    not a cache.
    """

    def __init__(self) -> None:
        """Start with no call in flight."""
        self._inflight: dict[Hashable, asyncio.Future[Any]] = {}

    async def do(self, key: Hashable, factory: Callable[[], Awaitable[_T]]) -> _T:
        """Run ``factory`` if no call for ``key`` is in flight; else share.

        Every waiter gets the leader's outcome — the same result *object*, or the
        same exception instance; neither is cached. A cancelled leader is the
        exception: a waiter not cancelling itself takes over and re-runs.
        """
        for _ in range(_MAX_FOLLOW_ATTEMPTS):
            existing = self._inflight.get(key)
            if existing is None:
                # Awaiting a coroutine runs its body inline: nothing yields between this
                # check and ``_lead``'s insert, so the slot can't be double-claimed.
                return await self._lead(key, factory)
            try:
                # shield: cancel our *view* of the shared future, never the future.
                return await asyncio.shield(existing)
            except asyncio.CancelledError:
                if _self_is_cancelling():
                    raise
                # The leader was cancelled, not us: loop round and take over.
                continue
        # Bound exhausted — run it ourselves rather than spin.
        return await factory()

    async def _lead(self, key: Hashable, factory: Callable[[], Awaitable[_T]]) -> _T:
        """Own the in-flight slot for ``key`` and run the factory."""
        # In a coroutine the running loop is guaranteed; ``get_event_loop``'s
        # thread-local fallback is never what this wants.
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        # Register before the first await, or a second caller opens its own slot.
        self._inflight[key] = future
        try:
            result = await factory()
            # Defence in depth: shield should leave this pending, and ``set_result`` on
            # a resolved future raises ``InvalidStateError`` in place of the answer.
            if not future.done():
                future.set_result(result)
            return result
        except BaseException as exc:
            # Defensive, like the success path: nothing outside ``_lead`` resolves this.
            if not future.done():
                future.set_exception(exc)
                # Mark retrieved: with no follower awaiting, asyncio would log
                # a spurious "Future exception was never retrieved".
                future.exception()
            raise
        finally:
            # Drop the slot before any waiter resumes; the next call starts fresh.
            self._inflight.pop(key, None)
