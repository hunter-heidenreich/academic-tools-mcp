"""Unit tests for the SingleFlight request-coalescing primitive.

The provider suites (e.g. ``tests/test_arxiv.py::TestGetPaperSingleFlight``)
already exercise single-flight end-to-end through ``arxiv.get_paper``. These
tests instead pin the class's own contract directly — the parts the
integration tests can't isolate:

- N concurrent callers for one key share a single factory run; followers
  never invoke their own factory.
- The slot is dropped after resolution, so it is *not* a cache: the next
  call re-runs the factory.
- A raising factory propagates the *same* exception to every waiter and is
  likewise not cached.
- Hashable tuple keys form independent namespaces (mirrors OpenAlex's
  ``("work", id)`` vs ``("author", id)`` sharing one instance) and distinct
  keys never block each other.

These exercise the leader/follower fan-in by gating the leader's factory on
an ``asyncio.Event`` and draining the ready queue so followers attach to the
in-flight future *before* the leader resolves — the only window in which
coalescing can be observed.
"""

import asyncio

import pytest

from academic_tools_mcp._singleflight import SingleFlight


async def _drain() -> None:
    """Yield enough times for all currently-scheduled tasks to reach their
    next ``await`` point — i.e. for followers to register on the in-flight
    future before the test releases the leader."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_concurrent_same_key_collapses_to_one_factory_run():
    sf = SingleFlight()
    calls = 0
    release = asyncio.Event()

    async def factory():
        nonlocal calls
        calls += 1
        await release.wait()
        return "result"

    tasks = [asyncio.create_task(sf.do("k", factory)) for _ in range(5)]
    await _drain()  # all five register on the same future before we release
    release.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1, f"expected one factory run, got {calls}"
    assert results == ["result"] * 5


@pytest.mark.asyncio
async def test_follower_factory_is_never_invoked():
    """A follower passes its own factory, but only the leader's runs — the
    follower shares the leader's result regardless of what it would return."""
    sf = SingleFlight()
    leader_calls = 0
    follower_calls = 0
    release = asyncio.Event()

    async def leader():
        nonlocal leader_calls
        leader_calls += 1
        await release.wait()
        return "leader-result"

    async def follower():
        nonlocal follower_calls
        follower_calls += 1
        return "follower-result"

    t1 = asyncio.create_task(sf.do("k", leader))
    await _drain()  # leader wins the slot and blocks on release
    t2 = asyncio.create_task(sf.do("k", follower))
    await _drain()  # follower attaches to the leader's future
    release.set()
    r1, r2 = await asyncio.gather(t1, t2)

    assert (r1, r2) == ("leader-result", "leader-result")
    assert leader_calls == 1
    assert follower_calls == 0


@pytest.mark.asyncio
async def test_slot_dropped_after_resolution_reruns_factory():
    """Single-flight is not a cache: once the future resolves the slot is
    freed, so the next sequential call re-runs the factory."""
    sf = SingleFlight()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return calls

    r1 = await sf.do("k", factory)
    r2 = await sf.do("k", factory)

    assert (r1, r2) == (1, 2)
    assert calls == 2


@pytest.mark.asyncio
async def test_exception_propagates_to_every_waiter():
    sf = SingleFlight()
    calls = 0
    release = asyncio.Event()

    class Boom(Exception):
        pass

    async def factory():
        nonlocal calls
        calls += 1
        await release.wait()
        raise Boom("kaboom")

    tasks = [asyncio.create_task(sf.do("k", factory)) for _ in range(4)]
    await _drain()
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert calls == 1, "the exception must come from a single factory run"
    assert all(isinstance(r, Boom) for r in results)
    # Every waiter sees the *same* exception object the leader raised.
    assert all(r is results[0] for r in results)


@pytest.mark.asyncio
async def test_exception_is_not_cached():
    """A failed factory is not remembered — the next call re-runs it."""
    sf = SingleFlight()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        raise RuntimeError("transient")

    with pytest.raises(RuntimeError):
        await sf.do("k", factory)
    with pytest.raises(RuntimeError):
        await sf.do("k", factory)

    assert calls == 2  # slot dropped in `finally`, factory re-ran


@pytest.mark.asyncio
async def test_distinct_keys_do_not_block_each_other():
    sf = SingleFlight()
    a_release = asyncio.Event()

    async def fa():
        await a_release.wait()
        return "a"

    async def fb():
        return "b"

    ta = asyncio.create_task(sf.do("a", fa))
    await _drain()  # "a" is in flight and blocked

    # "b" must complete despite "a" still holding its slot.
    assert await sf.do("b", fb) == "b"

    a_release.set()
    assert await ta == "a"


@pytest.mark.asyncio
async def test_tuple_keys_are_independent_namespaces():
    """Mirrors OpenAlex/OpenCitations sharing one SingleFlight across entity
    kinds: ("work", id) and ("author", id) carry the same canonical id but
    must not collide on one in-flight slot."""
    sf = SingleFlight()
    work_calls = 0
    author_calls = 0
    release = asyncio.Event()

    async def fwork():
        nonlocal work_calls
        work_calls += 1
        await release.wait()
        return "work-data"

    async def fauthor():
        nonlocal author_calls
        author_calls += 1
        return "author-data"

    twork = asyncio.create_task(sf.do(("work", "X"), fwork))
    await _drain()  # the work fetch is in flight under ("work", "X")

    # Same id "X", different entity — must run its own factory, not join work's.
    assert await sf.do(("author", "X"), fauthor) == "author-data"
    assert author_calls == 1

    release.set()
    assert await twork == "work-data"
    assert work_calls == 1
