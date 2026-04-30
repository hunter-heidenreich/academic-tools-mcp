"""Lifecycle tests for the per-provider client pool.

The pool itself is a thin lazy-singleton wrapper around
``httpx.AsyncClient``; the value of ``aclose_all`` is that a wedged
socket on shutdown can't pin the FastMCP lifespan. These tests stub
out the client to confirm the timeout actually trips and one stuck
provider doesn't block the others from closing.
"""

import asyncio

import pytest

from academic_tools_mcp import _clients


class _StubClient:
    """Minimal async client whose ``aclose`` we can rig to hang or fail."""

    def __init__(self, behaviour: str = "ok"):
        self.behaviour = behaviour
        self.closed = False

    async def aclose(self) -> None:
        if self.behaviour == "hang":
            # Sleep well past the 5s timeout so wait_for has to fire.
            await asyncio.sleep(60)
        elif self.behaviour == "raise":
            raise RuntimeError("simulated provider close failure")
        self.closed = True


@pytest.mark.asyncio
async def test_aclose_all_drains_registry_first(monkeypatch):
    """A concurrent ``get_client`` during shutdown must not see a
    half-closed client. ``aclose_all`` clears the registry before
    iterating so the next call rebuilds rather than reusing a mid-close
    object."""
    a = _StubClient()
    b = _StubClient()
    monkeypatch.setattr(_clients, "_clients", {"a": a, "b": b})

    await _clients.aclose_all()

    # Registry drained.
    assert _clients._clients == {}
    # Both clients closed in turn.
    assert a.closed and b.closed


@pytest.mark.asyncio
async def test_aclose_all_does_not_hang_on_wedged_client(monkeypatch):
    """A wedged socket on one provider must not block shutdown on the
    others. The hung aclose hits the 5s timeout (collapsed to 0.05s
    here for a fast test) and the second client still closes."""
    monkeypatch.setattr(_clients, "_ACLOSE_TIMEOUT_SECONDS", 0.05)

    hung = _StubClient(behaviour="hang")
    healthy = _StubClient()
    monkeypatch.setattr(_clients, "_clients", {"hung": hung, "ok": healthy})

    # If the timeout were ignored, this await would hang for 60s.
    # Wrap in our own watchdog so a regression fails the test fast.
    await asyncio.wait_for(_clients.aclose_all(), timeout=2.0)

    assert healthy.closed, "healthy provider must still close"
    assert _clients._clients == {}


@pytest.mark.asyncio
async def test_aclose_all_swallows_provider_exceptions(monkeypatch):
    """One provider throwing during aclose must not abort the loop —
    shutdown is best-effort by design."""
    raises = _StubClient(behaviour="raise")
    healthy = _StubClient()
    monkeypatch.setattr(
        _clients, "_clients", {"bad": raises, "ok": healthy}
    )

    await _clients.aclose_all()

    assert healthy.closed
    assert _clients._clients == {}


@pytest.mark.asyncio
async def test_aclose_all_idempotent(monkeypatch):
    """Calling it twice must be safe — no clients to close on the
    second pass, no error."""
    monkeypatch.setattr(_clients, "_clients", {})
    await _clients.aclose_all()
    await _clients.aclose_all()  # must not raise
