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
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_tools_mcp import _http, _stats
from academic_tools_mcp._throttle import Throttle


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

    # First slot starts immediately (last_request_time == 0); the second must
    # wait out the gap. So total >= one gap interval.
    assert elapsed >= 0.05


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
    snap = _stats.snapshot()["providers"]
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
async def test_reset_zeroes_state_and_rebuilds_primitives():
    """reset() clears counters and rebuilds the loop-bound lock/semaphore."""
    t = _make(max_concurrent=2)
    t.pending = 3
    t.last_request_time = 123.0
    old_sem, old_lock = t._sem, t._lock

    t.reset()

    assert t.pending == 0
    assert t.last_request_time == 0.0
    assert t._sem is not old_sem
    assert t._lock is not old_lock
    assert t._sem._value == 2  # rebuilt at the configured capacity


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
