"""Unit tests for the SingleFlight request-coalescing primitive.

The provider suites (e.g. ``tests/test_arxiv.py::TestGetPaperSingleFlight``)
already exercise single-flight end-to-end through ``arxiv.get_paper``. These
tests instead pin the class's own contract directly — the parts the
integration tests can't isolate:

- N concurrent callers for one key share a single factory run; followers
  never invoke their own factory, and receive the leader's *object*.
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
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import _singleflight
from academic_tools_mcp._singleflight import SingleFlight


async def _drain(rounds: int = 5) -> None:
    """Yield enough times for all currently-scheduled tasks to reach their
    next ``await`` point — i.e. for followers to register on the in-flight
    future before the test releases the leader."""
    for _ in range(rounds):
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
    assert sf._inflight == {}, "in-flight slot leaked after resolution"


@pytest.mark.asyncio
async def test_followers_receive_the_leaders_object_not_a_copy():
    """Identity, not just equality: ``cache.cached_lookup`` deep-copies per
    caller precisely because this class does not, and
    ``openalex._fetch_chunk`` skips that copy on the strength of it."""
    sf = SingleFlight()
    release = asyncio.Event()

    async def factory():
        await release.wait()
        return {"work": "payload"}

    tasks = [asyncio.create_task(sf.do("k", factory)) for _ in range(3)]
    await _drain()
    release.set()
    results = await asyncio.gather(*tasks)

    assert all(r is results[0] for r in results)


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
    assert sf._inflight == {}


@pytest.mark.asyncio
async def test_exception_propagates_to_every_waiter():
    sf = SingleFlight()
    calls = 0
    release = asyncio.Event()

    class BoomError(Exception):
        pass

    async def factory():
        nonlocal calls
        calls += 1
        await release.wait()
        raise BoomError("kaboom")

    tasks = [asyncio.create_task(sf.do("k", factory)) for _ in range(4)]
    await _drain()
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert calls == 1, "the exception must come from a single factory run"
    assert all(isinstance(r, BoomError) for r in results)
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


@settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    keys=st.lists(st.sampled_from(["a", "b", "c", ("t", 1)]), min_size=1, max_size=12),
)
def test_property_one_run_per_distinct_key(keys):
    """For any multiset of concurrent callers: one factory run per *distinct*
    key, every caller for a key gets that key's leader object, and no slot
    outlives resolution.

    Hypothesis rather than a ninth hand-written fan-in example — the rule is
    about arbitrary key multisets, not about any one arrangement of five
    callers. Driven under ``asyncio.run`` because Hypothesis cannot itself run
    an async test function.
    """

    async def scenario():
        sf = SingleFlight()
        runs: dict = {}
        release = asyncio.Event()

        def make(key):
            async def factory():
                runs[key] = runs.get(key, 0) + 1
                await release.wait()
                return {"key": key}  # a fresh object per run

            return factory

        tasks = [asyncio.create_task(sf.do(k, make(k))) for k in keys]
        await _drain(len(keys) + 5)  # every caller registers before we release
        release.set()
        results = await asyncio.gather(*tasks)

        assert runs == dict.fromkeys(set(keys), 1)
        first_for: dict = {}
        for key, result in zip(keys, results, strict=True):
            assert result == {"key": key}
            # Identity: one object per key, shared by all its callers.
            assert result is first_for.setdefault(key, result)
        assert sf._inflight == {}

    asyncio.run(scenario())


class TestLeaderCancellationDoesNotCancelFollowers:
    """A cancelled leader used to cancel every follower with it.

    The leader's task ending — an agent's tool call timing out, say — says
    nothing about the followers' lifetimes, but ``except BaseException`` set
    the ``CancelledError`` on the shared future, so unrelated concurrent calls
    for the same key failed for no reason.
    """

    @pytest.mark.asyncio
    async def test_follower_takes_over_and_succeeds(self):
        sf = SingleFlight()
        started = asyncio.Event()
        runs = 0

        async def factory():
            nonlocal runs
            runs += 1
            started.set()
            await asyncio.sleep(3600)
            return "leader"

        async def quick_factory():
            nonlocal runs
            runs += 1
            return "taken-over"

        leader = asyncio.create_task(sf.do("k", factory))
        await started.wait()

        follower = asyncio.create_task(sf.do("k", quick_factory))
        await asyncio.sleep(0)  # let the follower attach to the future

        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader

        assert await follower == "taken-over"
        assert runs == 2

    @pytest.mark.asyncio
    async def test_taking_over_reopens_the_slot_for_later_followers(self):
        """The new leader must register its own slot, or a takeover would
        silently un-coalesce every caller still queued behind it."""
        sf = SingleFlight()
        started = asyncio.Event()
        release = asyncio.Event()
        runs = 0

        async def hang():
            nonlocal runs
            runs += 1
            started.set()
            await asyncio.sleep(3600)

        async def takeover():
            nonlocal runs
            runs += 1
            await release.wait()
            return "taken-over"

        leader = asyncio.create_task(sf.do("k", hang))
        await started.wait()
        f1 = asyncio.create_task(sf.do("k", takeover))
        f2 = asyncio.create_task(sf.do("k", takeover))
        await _drain()

        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        await _drain()  # one follower leads; the other must attach to it
        release.set()

        assert await f1 == "taken-over"
        assert await f2 == "taken-over"
        # One cancelled leader + exactly one takeover, not one run per follower.
        assert runs == 2
        assert sf._inflight == {}, "in-flight slot leaked after takeover"

    @pytest.mark.asyncio
    async def test_follower_that_is_itself_cancelled_still_raises(self):
        sf = SingleFlight()
        started = asyncio.Event()

        async def factory():
            started.set()
            await asyncio.sleep(3600)

        leader = asyncio.create_task(sf.do("k", factory))
        await started.wait()
        follower = asyncio.create_task(sf.do("k", factory))
        await asyncio.sleep(0)

        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower

        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader

    @pytest.mark.asyncio
    async def test_real_errors_still_propagate_to_followers(self):
        # Only *cancellation* is leader-local; a genuine failure is shared,
        # exactly as before this change.
        sf = SingleFlight()
        calls = 0
        release = asyncio.Event()

        class BoomError(Exception):
            pass

        async def failing():
            nonlocal calls
            calls += 1
            await release.wait()
            raise BoomError("upstream exploded")

        tasks = [asyncio.create_task(sf.do("k", failing)) for _ in range(4)]
        await _drain()
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert calls == 1, "the failure must be shared, not re-run"
        assert all(isinstance(r, BoomError) for r in results)
        assert all(r is results[0] for r in results)

    @pytest.mark.asyncio
    async def test_slot_is_released_when_the_leader_is_cancelled(self):
        sf = SingleFlight()
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.sleep(3600)

        leader = asyncio.create_task(sf.do("k", hang))
        await started.wait()
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader

        assert sf._inflight == {}, "in-flight slot leaked after cancellation"

    @pytest.mark.asyncio
    async def test_repeated_leader_cancellation_falls_back_to_running_it(self, monkeypatch):
        """``_MAX_FOLLOW_ATTEMPTS`` exhausted: rather than spin, the caller runs
        the factory itself, unslotted.

        The bound is patched to 1 so a single cancelled leader reaches the
        fallback deterministically; at the shipped value it takes that many
        cancelled leaders in a row, which no ordinary workload produces.
        """
        monkeypatch.setattr(_singleflight, "_MAX_FOLLOW_ATTEMPTS", 1)
        sf = SingleFlight()
        started = asyncio.Event()
        running_unslotted = asyncio.Event()
        release = asyncio.Event()
        runs = 0

        async def hang():
            nonlocal runs
            runs += 1
            started.set()
            await asyncio.sleep(3600)

        async def own():
            nonlocal runs
            runs += 1
            running_unslotted.set()
            await release.wait()
            return "ran-it-myself"

        leader = asyncio.create_task(sf.do("k", hang))
        await started.wait()
        follower = asyncio.create_task(sf.do("k", own))
        await asyncio.sleep(0)

        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader

        await running_unslotted.wait()
        # The distinguishing observation: a caller that had taken over would be
        # holding the slot right now. The fallback runs outside it, so this
        # assertion is what separates the two paths — the result alone does not.
        assert sf._inflight == {}, "the fallback must not claim an in-flight slot"
        release.set()

        assert await follower == "ran-it-myself"
        assert runs == 2

    @pytest.mark.asyncio
    async def test_does_not_use_get_event_loop(self, monkeypatch):
        """``asyncio.get_event_loop`` is deprecated inside a coroutine (it
        would create or fetch a thread loop when none is running). Make the
        deprecated call fail outright so the assertion is about which API the
        leader reaches for, not merely that the happy path works."""

        def boom():
            raise AssertionError("get_event_loop called; use get_running_loop")

        monkeypatch.setattr(asyncio, "get_event_loop", boom)
        sf = SingleFlight()

        async def factory():
            return "v"

        assert await sf.do("k", factory) == "v"


class TestFollowerCancellationDoesNotReachTheLeader:
    """A follower giving up must not cancel the slot out from under everyone.

    Cancelling a task cancels the future it is suspended on — and for a
    follower that future is the *shared* one. So one follower's cancellation
    used to cancel the shared future, with two consequences: the leader then
    called ``set_result`` on it and raised ``InvalidStateError`` into its own
    caller in place of a perfectly good result, and every remaining follower
    saw the cancellation and re-ran the factory, defeating the coalescing this
    class exists for.

    This is the mirror of ``TestLeaderCancellationDoesNotCancelFollowers``,
    and the likelier direction: the documented fan-out is four parallel calls
    for one paper, so a cancellation lands on a follower three times in four.
    """

    @pytest.mark.asyncio
    async def test_leader_still_returns_its_result(self):
        sf = SingleFlight()
        started = asyncio.Event()

        async def factory():
            started.set()
            await asyncio.sleep(0.05)
            return "leader-result"

        leader = asyncio.create_task(sf.do("k", factory))
        await started.wait()
        follower = asyncio.create_task(sf.do("k", factory))
        await asyncio.sleep(0)

        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower

        # Used to be InvalidStateError.
        assert await leader == "leader-result"

    @pytest.mark.asyncio
    async def test_other_followers_still_share_the_one_call(self):
        sf = SingleFlight()
        started = asyncio.Event()
        runs = 0

        async def factory():
            nonlocal runs
            runs += 1
            started.set()
            await asyncio.sleep(0.05)
            return "result"

        leader = asyncio.create_task(sf.do("k", factory))
        await started.wait()
        doomed = asyncio.create_task(sf.do("k", factory))
        survivor = asyncio.create_task(sf.do("k", factory))
        await asyncio.sleep(0)

        doomed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await doomed

        assert await leader == "result"
        assert await survivor == "result"
        # The whole point: one outbound call, not two.
        assert runs == 1

    @pytest.mark.asyncio
    async def test_the_cancelled_follower_is_still_cancelled(self):
        # Shielding the shared future must not make a follower uncancellable —
        # its own caller asked it to stop.
        sf = SingleFlight()
        started = asyncio.Event()

        async def factory():
            started.set()
            await asyncio.sleep(0.05)
            return "result"

        leader = asyncio.create_task(sf.do("k", factory))
        await started.wait()
        follower = asyncio.create_task(sf.do("k", factory))
        await asyncio.sleep(0)

        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower
        assert follower.cancelled()
        await leader
