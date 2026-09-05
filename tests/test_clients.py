"""Lifecycle tests for the per-provider client pool.

The pool itself is a thin lazy-singleton wrapper around
``httpx.AsyncClient``; the value of ``aclose_all`` is that a wedged
socket on shutdown can't pin the FastMCP lifespan. These tests stub
out the client to confirm the timeout actually trips and one stuck
provider doesn't block the others from closing.
"""

import asyncio
import time

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
    monkeypatch.setattr(_clients, "_clients", {"bad": raises, "ok": healthy})

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


class TestAcloseAllIsConcurrent:
    """``aclose_all`` closed clients one at a time, each with its own 5s
    timeout — so eight wedged sockets took up to 40s, exactly the
    lifespan-pinning the per-client timeout exists to prevent.
    """

    @pytest.mark.asyncio
    async def test_wedged_clients_do_not_add_up(self, monkeypatch):
        monkeypatch.setattr(_clients, "_ACLOSE_TIMEOUT_SECONDS", 0.15)

        class WedgedClient:
            async def aclose(self):
                await asyncio.sleep(3600)

        _clients._clients.clear()
        for i in range(6):
            _clients._clients[f"p{i}"] = WedgedClient()

        start = time.monotonic()
        await _clients.aclose_all()
        elapsed = time.monotonic() - start

        # Serial would be ~0.9s; concurrent is ~0.15s. Assert well below the
        # serial figure rather than pinning an exact duration.
        assert elapsed < 0.5, f"closes did not overlap ({elapsed:.2f}s)"
        assert _clients._clients == {}

    @pytest.mark.asyncio
    async def test_one_failing_close_does_not_stop_the_others(self):
        closed = []

        class Boom:
            async def aclose(self):
                raise RuntimeError("transport already gone")

        class Fine:
            def __init__(self, name):
                self.name = name

            async def aclose(self):
                closed.append(self.name)

        _clients._clients.clear()
        _clients._clients["bad"] = Boom()
        _clients._clients["good1"] = Fine("good1")
        _clients._clients["good2"] = Fine("good2")

        await _clients.aclose_all()

        assert sorted(closed) == ["good1", "good2"]

    @pytest.mark.asyncio
    async def test_is_idempotent_and_safe_when_empty(self):
        _clients._clients.clear()
        await _clients.aclose_all()
        await _clients.aclose_all()

    @pytest.mark.asyncio
    async def test_cancellation_is_not_swallowed(self, monkeypatch):
        # CancelledError is a BaseException, so the old
        # `except (TimeoutError, Exception)` never caught it either — but the
        # tuple implied otherwise. A cancelled shutdown must not report success.
        class Slow:
            async def aclose(self):
                await asyncio.sleep(3600)

        _clients._clients.clear()
        _clients._clients["p"] = Slow()

        task = asyncio.create_task(_clients.aclose_all())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
