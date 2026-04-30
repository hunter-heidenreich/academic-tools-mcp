"""Tests for the per-provider concurrency model.

The new ``_throttled_get`` shape uses an ``asyncio.Semaphore`` (size
``_MAX_CONCURRENT``) to cap simultaneous in-flight requests, with the
inter-request gap-lock held only briefly to record start times. For
providers with concurrency > 1 (openalex, crossref, biorxiv,
opencitations, wikipedia, acl_anthology) multiple GETs should be in
flight at once; for arxiv (concurrency=1) the strict serial behaviour
is preserved.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_tools_mcp import arxiv, openalex


@pytest.mark.asyncio
async def test_openalex_throttle_allows_parallel_in_flight(monkeypatch):
    """openalex._MAX_CONCURRENT=4 should let 4 GETs run concurrently.

    The mock GET sleeps for a measurable interval; if the throttle
    serialised everything (old behaviour) total wall time would be
    ~4× the per-request sleep. With concurrency 4 it should be ~1×.
    """
    monkeypatch.setattr(openalex, "_MIN_REQUEST_GAP", 0.0)

    per_request_delay = 0.05

    async def slow_get(*args, **kwargs):
        await asyncio.sleep(per_request_delay)
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(return_value={})
        resp.raise_for_status = MagicMock()
        return resp

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=slow_get)
    monkeypatch.setattr(
        openalex, "_get_client", lambda: mock_client
    )

    started = time.monotonic()
    await asyncio.gather(*[
        openalex._throttled_get("http://example.com")
        for _ in range(openalex._MAX_CONCURRENT)
    ])
    elapsed = time.monotonic() - started

    # If concurrency truly = _MAX_CONCURRENT, all calls overlap and
    # total wall time is ~per_request_delay. Generous 3× headroom for
    # event-loop scheduling jitter.
    assert elapsed < per_request_delay * 3, (
        f"Expected ~{per_request_delay}s with concurrency "
        f"{openalex._MAX_CONCURRENT}, got {elapsed:.3f}s"
    )


@pytest.mark.asyncio
async def test_arxiv_throttle_serialises_strictly(monkeypatch):
    """arxiv._MAX_CONCURRENT=1: 2 calls must run back-to-back.

    With sleep mocked the gap should still bracket the second call so
    the elapsed time is at least one gap interval. We can't measure
    this directly without real sleep — instead, assert that the second
    call observes the first's start_time.
    """
    monkeypatch.setattr(arxiv, "_MIN_REQUEST_GAP", 0.0)
    # Track ordering: each request appends its slot before sleeping.
    order: list[str] = []

    async def slow_get(*args, **kwargs):
        order.append("start")
        # Yield briefly so the other coroutine has a chance to pre-empt
        # us if the sem isn't blocking it.
        await asyncio.sleep(0)
        order.append("end")
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        return resp

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=slow_get)

    await asyncio.gather(
        arxiv._throttled_get(mock_client, "http://example.com/a"),
        arxiv._throttled_get(mock_client, "http://example.com/b"),
    )

    # With concurrency=1 the order is strictly start,end,start,end —
    # never start,start,end,end (which would prove parallelism).
    assert order == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_openalex_max_concurrency_holds_under_overload(monkeypatch):
    """Even with 8 concurrent callers, no more than _MAX_CONCURRENT
    GETs should be in flight at the same time."""
    monkeypatch.setattr(openalex, "_MIN_REQUEST_GAP", 0.0)

    in_flight = 0
    peak = 0

    async def tracking_get(*args, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        return resp

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=tracking_get)
    monkeypatch.setattr(openalex, "_get_client", lambda: mock_client)

    # Fan out fewer than _MAX_PENDING concurrent callers so backpressure
    # doesn't kick in — we want to test the sem cap, not the queue cap.
    callers = min(openalex._MAX_PENDING, 8)
    await asyncio.gather(*[
        openalex._throttled_get("http://example.com") for _ in range(callers)
    ])

    assert peak <= openalex._MAX_CONCURRENT, (
        f"Saw {peak} concurrent GETs; cap is {openalex._MAX_CONCURRENT}"
    )
