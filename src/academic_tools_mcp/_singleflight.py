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
from typing import Any, Awaitable, Callable, Hashable


class SingleFlight:
    """Coalesce concurrent calls keyed by a hashable identifier.

    A factory is invoked at most once per key while a call is in flight.
    Once the future resolves, it is dropped from the registry; the next
    call for that key re-runs the factory (this is *not* a cache).
    """

    def __init__(self) -> None:
        self._inflight: dict[Hashable, asyncio.Future[Any]] = {}

    async def do(
        self, key: Hashable, factory: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Run ``factory`` if no call for ``key`` is in flight; else share.

        If ``factory`` raises or returns an error result, every concurrent
        waiter for ``key`` sees the same outcome. The next call (after the
        future is dropped) re-runs the factory — failure is not cached.
        """
        existing = self._inflight.get(key)
        if existing is not None:
            return await existing

        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._inflight[key] = future
        try:
            result = await factory()
            future.set_result(result)
            return result
        except BaseException as exc:
            # Surface the failure to every waiter, not just the leader.
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            # Clear before any waiter resumes so the next call (post-resolve)
            # starts a fresh in-flight slot.
            self._inflight.pop(key, None)
