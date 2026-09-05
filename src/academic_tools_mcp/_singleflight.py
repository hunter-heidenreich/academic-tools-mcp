"""Single-flight: collapse N concurrent calls for the same key into one.

The fan-out problem: an agent calls ``get_paper_metadata``,
``get_paper_authors``, ``get_paper_abstract``, ``get_paper_bibtex`` in
parallel for the same arXiv ID. All four take the cache-miss path,
all four queue behind the throttle, and three of them re-fetch the
same paper that the first call already wrote to disk — because the
throttle releases between requests but nobody re-checks the cache.

SingleFlight fixes this at the call-site: the first caller wins the
in-flight slot for ``key`` and runs the factory; the others ``await``
the same future and share the result. No second HTTP call, no second
cache write.

asyncio's cooperative scheduling makes the dict access here race-free:
the check + insert in ``do`` is synchronous, so no other coroutine can
sneak in between them.
"""

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

# Bound on how many times a caller will take over from a cancelled leader
# before just running the factory itself. Only reachable if leaders are being
# cancelled repeatedly; exists so this can never spin.
_MAX_TAKEOVERS = 3


def _self_is_cancelling() -> bool:
    """Whether the *current* task is the one being cancelled.

    ``Task.cancelling()`` counts pending cancellation requests against this
    task (the 3.11 cancel/uncancel protocol). It distinguishes "someone
    cancelled me" from "I observed a CancelledError that belongs to another
    task's future" — which is exactly the case a single-flight follower hits
    when its leader is cancelled.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class SingleFlight:
    """Coalesce concurrent calls keyed by a hashable identifier.

    A factory is invoked at most once per key while a call is in flight.
    Once the future resolves, it is dropped from the registry; the next
    call for that key re-runs the factory (this is *not* a cache).
    """

    def __init__(self) -> None:
        self._inflight: dict[Hashable, asyncio.Future[Any]] = {}

    async def do(self, key: Hashable, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Run ``factory`` if no call for ``key`` is in flight; else share.

        If ``factory`` raises or returns an error result, every concurrent
        waiter for ``key`` sees the same outcome. The next call (after the
        future is dropped) re-runs the factory — failure is not cached.

        **Leader cancellation must not cancel the followers.** If the leader's
        task is cancelled — an agent's tool call times out, say — its lifetime
        ended, but the followers' did not, so propagating the
        ``CancelledError`` to them fails unrelated calls for no reason. A
        follower that is not itself being cancelled takes over as the new
        leader and runs the factory instead.

        **Nor may follower cancellation reach the leader**, which is what the
        ``asyncio.shield`` below is for. Cancelling a task cancels the future it
        is suspended on, and for a follower that future is the *shared* one, so
        an unshielded follower giving up cancels the slot out from under
        everybody: the leader then calls ``set_result`` on a cancelled future
        (``InvalidStateError``, raised into its own caller in place of a
        perfectly good result), and every remaining follower sees the
        cancellation and redundantly re-runs the factory — defeating the
        coalescing this class exists for.
        """
        for _ in range(_MAX_TAKEOVERS):
            existing = self._inflight.get(key)
            if existing is None:
                return await self._lead(key, factory)
            try:
                # shield: cancelling this task must cancel our *view* of the
                # shared future, never the shared future itself.
                return await asyncio.shield(existing)
            except asyncio.CancelledError:
                if _self_is_cancelling():
                    # Our own task is being cancelled — that is ours to honour.
                    raise
                # The leader was cancelled, not us. Loop round and take over.
                continue
        # Pathological: leaders kept getting cancelled. Run it ourselves
        # rather than spinning.
        return await factory()

    async def _lead(self, key: Hashable, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Own the in-flight slot for ``key`` and run the factory."""
        # get_running_loop, not get_event_loop: we are inside a coroutine, so
        # a running loop is guaranteed, and get_event_loop is deprecated here
        # (it would create or fetch a loop for the thread when none is
        # running, which is never what this wants).
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await factory()
            # Defence in depth: shield keeps followers from cancelling this
            # future, but setting a result on an already-resolved future is an
            # InvalidStateError that would replace the leader's answer with a
            # crash. Never worth risking for a branch this cheap.
            if not future.done():
                future.set_result(result)
            return result
        except BaseException as exc:
            # Surface the failure to every waiter, not just the leader.
            if not future.done():
                future.set_exception(exc)
            # If no follower ever awaited this future, its exception would be
            # garbage-collected unretrieved and asyncio would log a spurious
            # "Future exception was never retrieved" warning. Read it here to
            # mark it retrieved — followers already suspended on the future
            # still receive it when they resume.
            if future.done() and not future.cancelled():
                future.exception()
            raise
        finally:
            # Clear before any waiter resumes so the next call (post-resolve)
            # starts a fresh in-flight slot.
            self._inflight.pop(key, None)
