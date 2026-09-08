"""Single-flight: collapse N concurrent calls for the same key into one.

Four unified paper tools called in parallel for one ID all take the
cache-miss path, all queue behind the throttle, and three re-fetch what the
first already wrote — the throttle releases between requests, but nobody
re-checks the cache. Here the first caller wins the in-flight slot for
``key`` and runs the factory; the rest ``await`` the same future.

The cancellation contract in both directions, and why followers receive the
leader's *object* rather than a copy, live in ``.claude/rules/cache.md``.
"""

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, TypeVar

_T = TypeVar("_T")

# How many cancelled leaders one caller watches before running the factory
# itself, unslotted. A bound, not a policy: it exists so this can never spin.
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

    The factory runs at most once per key *while a call is in flight*: the
    slot is dropped when the future resolves, so the next call re-runs it.
    This is not a cache.
    """

    def __init__(self) -> None:
        self._inflight: dict[Hashable, asyncio.Future[Any]] = {}

    async def do(self, key: Hashable, factory: Callable[[], Awaitable[_T]]) -> _T:
        """Run ``factory`` if no call for ``key`` is in flight; else share.

        Every waiter for ``key`` gets the leader's outcome — the same result
        *object*, or the same exception instance. Neither is cached.
        """
        for _ in range(_MAX_FOLLOW_ATTEMPTS):
            existing = self._inflight.get(key)
            if existing is None:
                # Awaiting a coroutine runs its body inline, so nothing yields
                # between this check and ``_lead``'s insert — no double-claim.
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
        # We are in a coroutine, so the running loop is guaranteed;
        # get_event_loop's thread-local fallback is never what this wants.
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        # Register before the first await, or a second caller opens its own slot.
        self._inflight[key] = future
        try:
            result = await factory()
            # Defence in depth: shield should leave this pending, and set_result
            # on a resolved one is an InvalidStateError in place of an answer.
            if not future.done():
                future.set_result(result)
            return result
        except BaseException as exc:
            # Already done can only mean cancelled: nothing else resolves it.
            if not future.done():
                future.set_exception(exc)
                # Mark retrieved: with no follower awaiting, asyncio would log
                # a spurious "Future exception was never retrieved".
                future.exception()
            raise
        finally:
            # Drop the slot before any waiter resumes; the next call starts fresh.
            self._inflight.pop(key, None)
