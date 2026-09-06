"""Tests for the shared ``Throttle`` primitive.

The per-provider gating (burst cap -> concurrency semaphore -> inter-start
gap-lock -> stats) used to be copy-pasted into every provider module. It now
lives once in ``academic_tools_mcp._throttle.Throttle`` and each provider holds
a configured instance. These tests exercise the class directly so the behaviour
is verified in one place instead of five near-identical per-provider copies.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_tools_mcp import _http, _stats
from academic_tools_mcp._throttle import _MAX_TRACKED_HOSTS, Throttle


def _make(**overrides) -> Throttle:
    kwargs = {
        "namespace": "testprovider",
        "label": "TestProvider",
        "max_concurrent": 4,
        "min_gap_seconds": 0.0,
        "max_pending": 5,
    }
    kwargs.update(overrides)
    return Throttle(**kwargs)


async def _until(predicate: Callable[[], bool], *, limit_seconds: float = 1.0) -> None:
    """Yield to the loop until ``predicate`` holds. Fails rather than hanging."""
    deadline = time.monotonic() + limit_seconds
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_slot_enforces_inter_start_gap():
    """Two sequential slots must be spaced by at least min_gap_seconds."""
    t = _make(min_gap_seconds=0.05)

    started = time.monotonic()
    async with t.slot("http://example.com/a"):
        pass
    async with t.slot("http://example.com/b"):
        pass
    elapsed = time.monotonic() - started

    # First slot starts immediately (nothing recorded yet); the second must
    # wait out the gap. So total >= one gap interval — and, since a cold
    # throttle paces nobody, comfortably under two.
    assert elapsed >= 0.05
    assert elapsed < 0.10


@pytest.mark.asyncio
async def test_gap_is_measured_between_starts_not_durations():
    """A body longer than the gap leaves nothing to wait out.

    The documented unit is the interval between request *starts*: a slow
    request has already paid the gap by the time it releases. Measuring from
    release instead would silently halve every provider's throughput.
    """
    t = _make(min_gap_seconds=0.10)

    async with t.slot("http://example.com/a"):
        await asyncio.sleep(0.20)

    released = time.monotonic()
    async with t.slot("http://example.com/b"):
        waited = time.monotonic() - released

    assert waited < 0.05


@pytest.mark.asyncio
async def test_slot_caps_peak_concurrency():
    """No more than max_concurrent bodies run inside slot() at once."""
    t = _make(max_concurrent=4, max_pending=100)
    in_flight = 0
    peak = 0

    async def worker():
        nonlocal in_flight, peak
        async with t.slot("http://example.com"):
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1

    await asyncio.gather(*[worker() for _ in range(12)])
    assert peak <= 4


@pytest.mark.asyncio
async def test_per_host_mode_still_caps_global_concurrency():
    """max_concurrent bounds *our* egress, so it stays global under per_host.

    Making the semaphore per-host would let a 12-publisher walk open 12
    parallel streams, however polite that is to each publisher individually.
    """
    t = _make(max_concurrent=4, max_pending=100, per_host=True)
    in_flight = 0
    peak = 0

    async def worker(i: int):
        nonlocal in_flight, peak
        async with t.slot(f"http://h{i}.example/x"):
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1

    await asyncio.gather(*[worker(i) for i in range(12)])
    assert peak <= 4


@pytest.mark.asyncio
async def test_slot_is_held_for_whole_body():
    """pending reflects an open slot for the full async-with lifetime.

    This is what lets a streaming PDF download hold the slot while bytes
    flush — the slot is not released the moment the GET is fired.
    """
    t = _make()
    assert t.pending == 0
    async with t.slot("http://example.com"):
        assert t.pending == 1
    assert t.pending == 0


@pytest.mark.asyncio
async def test_pending_is_released_when_the_body_raises():
    """A failing request must not leak a permanent slot.

    Without the finally, each error burns one of max_pending until restart — a
    provider that 500s a few times would answer backpressure forever after.
    """
    t = _make()

    with pytest.raises(RuntimeError):
        async with t.slot("http://example.com"):
            raise RuntimeError("upstream blew up")

    assert t.pending == 0


@pytest.mark.asyncio
async def test_semaphore_is_released_when_the_body_raises():
    """The concurrency permit comes back too, not just the pending count."""
    t = _make(max_concurrent=1)

    with pytest.raises(RuntimeError):
        async with t.slot("http://example.com"):
            raise RuntimeError("upstream blew up")

    async def take_slot():
        async with t.slot("http://example.com"):
            return True

    # A leaked permit deadlocks here rather than failing an assertion.
    assert await asyncio.wait_for(take_slot(), timeout=1.0)


@pytest.mark.asyncio
async def test_admits_max_pending_callers_and_refuses_the_next():
    """The burst-cap boundary, under real concurrency rather than a set pending.

    ``pending`` counts in-flight *plus* queued callers, so max_pending is total
    admitted callers: at max_concurrent=1, max_pending=2, exactly one caller
    waits behind the in-flight one before the third is refused.
    """
    t = _make(max_concurrent=1, max_pending=2)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        async with t.slot("http://example.com"):
            entered.set()
            await release.wait()

    in_flight = asyncio.create_task(hold())
    await entered.wait()
    # Admitted at exactly the cap: queues on the semaphore, does not raise.
    queued = asyncio.create_task(hold())
    await _until(lambda: t.pending == 2)

    with pytest.raises(_http.LocalBackpressureError):
        async with t.slot("http://example.com"):
            pass

    release.set()
    await asyncio.wait_for(asyncio.gather(in_flight, queued), timeout=1.0)
    assert t.pending == 0
    assert not queued.cancelled()


@pytest.mark.asyncio
async def test_burst_cap_raises_and_counts():
    """The (max_pending+1)-th queued caller fails fast with backpressure."""
    t = _make(max_pending=5)
    t.pending = 5  # simulate 5 already queued

    with pytest.raises(_http.LocalBackpressureError) as excinfo:
        async with t.slot("http://example.com"):
            pass

    assert excinfo.value.pending == 5
    assert excinfo.value.max_pending == 5
    assert excinfo.value.provider == "TestProvider"
    # The refusal is not itself a request, and must not leave a phantom caller.
    assert t.pending == 5
    snap = _stats.snapshot()["providers"]
    assert snap.get("testprovider", {}).get("http_calls", 0) == 0
    assert snap.get("testprovider", {}).get("backpressure_refusals") == 1


@pytest.mark.asyncio
async def test_slot_counts_http_calls():
    """A successful slot increments the http_calls counter once."""
    t = _make()
    async with t.slot("http://example.com"):
        pass
    snap = _stats.snapshot()["providers"]
    assert snap.get("testprovider", {}).get("http_calls") == 1


@pytest.mark.asyncio
async def test_slot_can_suppress_the_http_call_count():
    """count_request=False is how get() avoids double-counting its attempts.

    get_with_retry counts each attempt it actually makes; counting the slot too
    would over-report outbound volume by one per call.
    """
    t = _make()
    async with t.slot("http://example.com", count_request=False):
        pass
    snap = _stats.snapshot()["providers"]
    assert snap.get("testprovider", {}).get("http_calls", 0) == 0


@pytest.mark.asyncio
async def test_reset_zeroes_state_and_rebuilds_primitives():
    """reset() clears counters and rebuilds the loop-bound lock/semaphore."""
    t = _make(max_concurrent=2)
    t.pending = 3
    t._last_start["x"] = 123.0
    old_sem, old_lock = t._sem, t._lock

    t.reset()

    assert t.pending == 0
    assert t._last_start == {}
    assert t._sem is not old_sem
    assert t._lock is not old_lock

    # Rebuilt at the configured capacity, asserted through the behaviour rather
    # than asyncio's private counter: two callers run at once, a third waits.
    held = asyncio.Event()
    release = asyncio.Event()
    running = 0

    async def worker():
        nonlocal running
        async with t.slot("http://example.com"):
            running += 1
            if running == 2:
                held.set()
            await release.wait()

    tasks = [asyncio.create_task(worker()) for _ in range(3)]
    await asyncio.wait_for(held.wait(), timeout=1.0)
    await asyncio.sleep(0)
    assert running == 2
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)


@pytest.mark.asyncio
async def test_get_fires_request_inside_slot():
    """get() runs _http.get_with_retry inside the slot and returns the response."""
    t = _make()
    resp = MagicMock()
    resp.status_code = 200
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    out = await t.get(client, "http://example.com", params={"x": "1"})

    assert out is resp
    client.get.assert_awaited_once()
    snap = _stats.snapshot()["providers"]
    assert snap.get("testprovider", {}).get("http_calls") == 1


@pytest.mark.asyncio
async def test_get_threads_retry_attempts_into_get_with_retry(monkeypatch):
    """A provider's retry_attempts policy reaches get_with_retry as max_attempts."""
    t = _make(retry_attempts=3)
    captured: dict = {}

    async def fake_get_with_retry(client, url, **kwargs):
        captured.update(kwargs)
        return MagicMock(status_code=200)

    monkeypatch.setattr(_http, "get_with_retry", fake_get_with_retry)

    await t.get(MagicMock(), "http://example.com")

    assert captured["max_attempts"] == 3


def test_retry_attempts_defaults_to_two():
    """The default is one transparent retry (1 original + 1)."""
    assert _make().retry_attempts == 2


class TestPolicyIsClamped:
    """A typo'd policy constant must degrade, not hang.

    Mirrors ``get_with_retry``'s clamp of ``max_attempts``: nothing validates a
    provider module's constants, and the failure modes are silent — Semaphore(0)
    waits forever with no timeout, and max_pending=0 refuses every caller.
    """

    def test_max_concurrent_floors_at_one(self):
        assert _make(max_concurrent=0).max_concurrent == 1

    def test_max_pending_floors_at_one(self):
        assert _make(max_pending=0).max_pending == 1

    def test_retry_attempts_floors_at_one(self):
        assert _make(retry_attempts=0).retry_attempts == 1

    def test_gap_floors_at_zero(self):
        assert _make(min_gap_seconds=-5.0).min_gap_seconds == 0.0

    @pytest.mark.asyncio
    async def test_a_zero_concurrency_throttle_still_serves(self):
        t = _make(max_concurrent=0)

        async def take_slot():
            async with t.slot("http://example.com"):
                return True

        assert await asyncio.wait_for(take_slot(), timeout=1.0)


# ---------------------------------------------------------------------------
# Per-host pacing (opt-in)
# ---------------------------------------------------------------------------
#
# For a client whose URLs are not one API. `oa_download` resolves DOIs to
# arbitrary publisher CDNs, and a reference walk through one journal lands many
# of them on the same domain — which a single global timestamp either paces far
# too loosely (gap 0) or paces every unrelated host for (one global gap).
# Verified here once rather than per provider.


@pytest.mark.asyncio
async def test_per_host_gap_delays_a_repeat_host():
    t = _make(min_gap_seconds=0.05, per_host=True)
    start = time.monotonic()
    async with t.slot("http://a.example/one"):
        pass
    async with t.slot("http://a.example/two"):
        pass
    # Keyed on netloc, not the whole URL: two paths on one host share pacing.
    assert time.monotonic() - start >= 0.05


@pytest.mark.asyncio
async def test_per_host_gap_does_not_delay_a_different_host():
    t = _make(min_gap_seconds=0.20, per_host=True)
    start = time.monotonic()
    async with t.slot("http://a.example/one"):
        pass
    async with t.slot("http://b.example/one"):
        pass
    assert time.monotonic() - start < 0.20


@pytest.mark.asyncio
async def test_global_mode_still_delays_across_hosts():
    # Pins that the seven API providers did NOT change: with per_host off, two
    # distinct hosts still pace against one timestamp.
    t = _make(min_gap_seconds=0.05)
    start = time.monotonic()
    async with t.slot("http://a.example/one"):
        pass
    async with t.slot("http://b.example/one"):
        pass
    assert time.monotonic() - start >= 0.05


@pytest.mark.asyncio
async def test_global_mode_keeps_one_key():
    # The map is the single home for both modes; global mode is the dict of
    # size one it degenerates to, which is why the prune never fires there.
    t = _make(min_gap_seconds=0.0)
    async with t.slot("http://a.example/one"):
        pass
    async with t.slot("http://b.example/one"):
        pass
    assert len(t._last_start) == 1


@pytest.mark.asyncio
async def test_concurrent_distinct_hosts_do_not_serialise():
    """The acceptance criterion for the reservation design.

    ``slot`` reserves its start instant under the lock and sleeps outside it.
    Holding the lock across the sleep instead would make host A's wait block
    host B's lock acquisition, collapsing the effective rate back to
    1/min_gap globally — at which point ``per_host`` buys nothing over just
    setting a global gap. This test fails under that arrangement.
    """
    t = _make(max_concurrent=4, min_gap_seconds=0.30, per_host=True)

    # Prime only the slow host, so it owes a full gap while the other is
    # brand new and owes nothing.
    async with t.slot("http://slow.example/x"):
        pass

    latencies: dict[str, float] = {}

    async def touch(host: str) -> None:
        began = time.monotonic()
        async with t.slot(f"http://{host}.example/x"):
            latencies[host] = time.monotonic() - began

    await asyncio.gather(touch("slow"), touch("fresh"))

    # The unrelated host must not queue behind the sleeper. Under
    # sleep-inside-lock it cannot even compute its own wait until the slow
    # host's 0.3s sleep releases the shared lock.
    assert latencies["slow"] >= 0.25
    assert latencies["fresh"] < 0.10, (
        f"an unrelated host waited {latencies['fresh']:.2f}s behind a paced one"
    )


@pytest.mark.asyncio
async def test_netloc_key_is_case_insensitive():
    # RFC 3986 makes netloc case-insensitive, and OpenAlex-supplied URLs are
    # inconsistently cased — treating them as two hosts would halve the gap.
    t = _make(min_gap_seconds=0.05, per_host=True)
    start = time.monotonic()
    async with t.slot("http://A.Example/one"):
        pass
    async with t.slot("http://a.example/two"):
        pass
    assert time.monotonic() - start >= 0.05


@pytest.mark.asyncio
async def test_port_is_part_of_the_host_key():
    # One host on two ports is two services.
    t = _make(min_gap_seconds=0.20, per_host=True)
    start = time.monotonic()
    async with t.slot("http://a.example:8080/one"):
        pass
    async with t.slot("http://a.example:9090/one"):
        pass
    assert time.monotonic() - start < 0.20


@pytest.mark.asyncio
async def test_reset_clears_per_host_state():
    t = _make(min_gap_seconds=0.01, per_host=True)
    async with t.slot("http://a.example/one"):
        pass
    assert t._last_start

    t.reset()

    assert t._last_start == {}


@pytest.mark.asyncio
async def test_host_map_is_bounded_by_the_age_sweep():
    # gap 0 means every recorded start is instantly older than the window, so
    # the sweep can drop all of them — the cheap, exact case.
    t = _make(min_gap_seconds=0.0, per_host=True)
    for i in range(_MAX_TRACKED_HOSTS + 50):
        async with t.slot(f"http://h{i}.example/x"):
            pass

    assert len(t._last_start) <= _MAX_TRACKED_HOSTS


@pytest.mark.asyncio
async def test_host_map_is_bounded_even_when_every_entry_is_fresh():
    # A gap wide enough that nothing has expired: the sweep drops nothing, so
    # the oldest-first fallback is what has to hold the cap. Those entries are
    # nearest expiry anyway, so at worst one request starts early.
    t = _make(min_gap_seconds=3600.0, per_host=True)
    # Populate directly: taking real slots would sleep for an hour apiece.
    now = time.monotonic()
    for i in range(_MAX_TRACKED_HOSTS + 50):
        t._last_start[f"h{i}.example"] = now + i
    t._prune(now)

    assert len(t._last_start) <= _MAX_TRACKED_HOSTS
    # The newest survive; the oldest are the ones dropped.
    assert "h0.example" not in t._last_start
    assert f"h{_MAX_TRACKED_HOSTS + 49}.example" in t._last_start


@pytest.mark.asyncio
async def test_host_map_exactly_at_the_cap_is_left_alone():
    """The cap is inclusive: at _MAX_TRACKED_HOSTS the sweep does not run.

    Every entry here is long expired, so a sweep would drop all of them — which
    is what makes the early return observable rather than a no-op.
    """
    t = _make(min_gap_seconds=0.0, per_host=True)
    now = time.monotonic()
    for i in range(_MAX_TRACKED_HOSTS):
        t._last_start[f"h{i}.example"] = now - 60.0

    t._prune(now)

    assert len(t._last_start) == _MAX_TRACKED_HOSTS


@pytest.mark.asyncio
async def test_age_sweep_keeps_an_entry_that_can_still_produce_a_wait():
    """The sweep prunes against the real clock, not the reserved start.

    A caller that owes a wait reserves ``now + wait``; sweeping against that
    instant drops every entry in ``(now - gap, now + wait - gap]``, each of
    which would still have paced a caller arriving now. Only the real clock
    makes the sweep semantics-preserving, as its docstring claims.
    """
    t = _make(min_gap_seconds=0.05, per_host=True)
    now = time.monotonic()
    t._last_start["self.example"] = now  # forces the caller below to wait
    t._last_start["live.example"] = now - 0.03  # still paces until now + 0.02
    for i in range(_MAX_TRACKED_HOSTS):
        t._last_start[f"expired{i}.example"] = now - 60.0

    async with t.slot("http://self.example/x"):
        pass

    assert "live.example" in t._last_start
