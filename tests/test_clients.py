"""Tests for the per-provider client pool: the singleton and the shutdown.

``get_client`` is a lazy singleton whose whole contract is "same name, same
object, configured once" — a second call silently ignores its kwargs, so the
identity and the ignoring are both worth pinning. ``aclose_all``'s value is
that a wedged socket on shutdown can't pin the FastMCP lifespan; those tests
stub the client to confirm the bound actually trips and that one stuck
provider doesn't block the others from closing.
"""

import asyncio
import time

import httpx
import pytest

from academic_tools_mcp import _clients


@pytest.fixture
def spy_client(monkeypatch):
    """Replace ``httpx.AsyncClient`` with a spy; yields the list of ctor kwargs.

    Lets the singleton tests assert on construction without building real
    clients (which nothing in the suite closes) and, because each entry is one
    ``__init__`` call, lets them assert a *second* build never happened.
    """
    calls: list[dict] = []

    class SpyClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            calls.append(kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", SpyClient)
    return calls


class _StubClient:
    """Minimal async client whose ``aclose`` we can rig to hang or fail."""

    def __init__(self, behaviour: str = "ok"):
        self.behaviour = behaviour
        self.closed = False

    async def aclose(self) -> None:
        if self.behaviour == "hang":
            # Sleep well past any timeout a test sets, so the bound has to fire.
            await asyncio.sleep(3600)
        elif self.behaviour == "raise":
            raise RuntimeError("simulated provider close failure")
        self.closed = True


class TestGetClient:
    """The lazy-singleton contract: one client per name, configured once."""

    def test_same_name_returns_the_same_object(self, spy_client):
        first = _clients.get_client("alpha")
        second = _clients.get_client("alpha")

        assert first is second
        # Pooling is the entire point: a second build would mean a second
        # connection pool and a fresh TCP+TLS handshake per call site.
        assert len(spy_client) == 1

    def test_distinct_names_get_distinct_clients(self, spy_client):
        alpha = _clients.get_client("alpha")
        beta = _clients.get_client("beta")

        assert alpha is not beta
        assert len(spy_client) == 2
        assert _clients._POOL.keys() == {"alpha", "beta"}

    def test_repeat_call_ignores_construction_kwargs(self, spy_client):
        """A later call with different headers/timeout is a no-op, not an
        override — providers configure their client in exactly one place, and
        this is the footgun that makes that safe to rely on."""
        first = _clients.get_client("alpha", headers={"User-Agent": "first"}, timeout=1.0)
        second = _clients.get_client("alpha", headers={"User-Agent": "second"}, timeout=99.0)

        assert second is first
        assert len(spy_client) == 1
        assert spy_client[0]["headers"] == {"User-Agent": "first"}
        assert spy_client[0]["timeout"] == 1.0

    def test_bakes_in_the_shared_pool_config(self, spy_client):
        _clients.get_client("alpha")

        assert spy_client[0]["limits"] is _clients._DEFAULT_LIMITS
        # Redirects are on for every provider: arXiv and publisher PDF hosts
        # both bounce the first request. No caller passes this, so nothing else
        # would catch the default flipping.
        assert spy_client[0]["follow_redirects"] is True
        assert spy_client[0]["headers"] == {}

    @pytest.mark.asyncio
    async def test_real_client_round_trip_and_rebuild(self):
        """The only test that drives a real ``httpx.AsyncClient`` through both
        halves — every other one duck-types ``aclose``, so a signature drift in
        httpx would otherwise sail past the suite."""
        client = _clients.get_client("roundtrip")
        assert isinstance(client, httpx.AsyncClient)
        assert not client.is_closed

        await _clients.aclose_all()
        assert client.is_closed

        # The registry was drained, so the next caller gets a usable client
        # rather than the closed one.
        rebuilt = _clients.get_client("roundtrip")
        assert rebuilt is not client
        assert not rebuilt.is_closed

        await _clients.aclose_all()


class TestAcloseAll:
    """Shutdown is best-effort and bounded: no client can pin the lifespan."""

    @pytest.mark.asyncio
    async def test_drains_registry_first(self, monkeypatch):
        """A concurrent ``get_client`` during shutdown must not see a
        half-closed client. ``aclose_all`` clears the registry before
        iterating so the next call rebuilds rather than reusing a mid-close
        object."""
        a = _StubClient()
        b = _StubClient()
        monkeypatch.setattr(_clients, "_POOL", {"a": a, "b": b})

        await _clients.aclose_all()

        assert _clients._POOL == {}
        assert a.closed and b.closed

    @pytest.mark.asyncio
    async def test_does_not_hang_on_wedged_client(self, monkeypatch):
        """A wedged socket on one provider must not block shutdown on the
        others: the hung aclose hits the bound and the second client still
        closes."""
        monkeypatch.setattr(_clients, "_ACLOSE_TIMEOUT_SECONDS", 0.05)

        hung = _StubClient(behaviour="hang")
        healthy = _StubClient()
        monkeypatch.setattr(_clients, "_POOL", {"hung": hung, "ok": healthy})

        # If the bound were ignored this would hang for an hour; the watchdog
        # makes a regression fail fast instead of stalling the suite.
        await asyncio.wait_for(_clients.aclose_all(), timeout=2.0)

        assert healthy.closed, "healthy provider must still close"
        assert _clients._POOL == {}

    @pytest.mark.asyncio
    async def test_a_slow_but_healthy_close_is_allowed_to_finish(self, monkeypatch):
        """The passing side of the bound: a close that takes real time but
        completes inside it must not be cancelled. Every other healthy stub
        closes instantly, so nothing else here would catch the cancel loop
        firing before the wait, or the comparison being inverted."""
        monkeypatch.setattr(_clients, "_ACLOSE_TIMEOUT_SECONDS", 1.0)

        class SlowClient:
            def __init__(self):
                self.closed = False

            async def aclose(self):
                await asyncio.sleep(0.1)
                self.closed = True

        slow = SlowClient()
        monkeypatch.setattr(_clients, "_POOL", {"slow": slow})

        await _clients.aclose_all()

        assert slow.closed, "a close well inside the bound was cut short"

    @pytest.mark.asyncio
    async def test_one_failing_close_does_not_stop_the_others(self, monkeypatch):
        closed = []

        class Boom:
            async def aclose(self):
                raise RuntimeError("transport already gone")

        class Fine:
            def __init__(self, name):
                self.name = name

            async def aclose(self):
                closed.append(self.name)

        monkeypatch.setattr(
            _clients,
            "_POOL",
            {"bad": Boom(), "good1": Fine("good1"), "good2": Fine("good2")},
        )

        await _clients.aclose_all()

        assert sorted(closed) == ["good1", "good2"]
        assert _clients._POOL == {}

    @pytest.mark.asyncio
    async def test_is_idempotent_and_safe_when_empty(self, monkeypatch):
        monkeypatch.setattr(_clients, "_POOL", {})

        await _clients.aclose_all()
        await _clients.aclose_all()

        assert _clients._POOL == {}

    @pytest.mark.asyncio
    async def test_cancellation_is_not_swallowed(self, monkeypatch):
        # CancelledError is a BaseException, so the per-client `except Exception`
        # does not catch it. A cancelled shutdown must not report success.
        monkeypatch.setattr(_clients, "_POOL", {"p": _StubClient(behaviour="hang")})

        task = asyncio.create_task(_clients.aclose_all())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_wedged_clients_do_not_add_up(self, monkeypatch):
        """Regression: closes were issued one at a time, each with its own 5s
        timeout, so eight wedged sockets took up to 40s — exactly the
        lifespan-pinning the timeout exists to prevent."""
        monkeypatch.setattr(_clients, "_ACLOSE_TIMEOUT_SECONDS", 0.15)
        monkeypatch.setattr(
            _clients,
            "_POOL",
            {f"p{i}": _StubClient(behaviour="hang") for i in range(6)},
        )

        start = time.monotonic()
        await _clients.aclose_all()
        elapsed = time.monotonic() - start

        # Serial would be ~0.9s; concurrent is ~0.15s. Assert well below the
        # serial figure rather than pinning an exact duration.
        assert elapsed < 0.5, f"closes did not overlap ({elapsed:.2f}s)"
        assert _clients._POOL == {}

    @pytest.mark.asyncio
    async def test_bound_holds_against_a_close_that_outlives_cancellation(self, monkeypatch):
        """Regression: the bound was ``asyncio.wait_for`` per client, which
        awaits the coroutine it just cancelled — so a teardown that keeps
        awaiting past cancellation blocked shutdown for as long as it liked.
        """
        linger = 0.5
        monkeypatch.setattr(_clients, "_ACLOSE_TIMEOUT_SECONDS", 0.05)

        class LingeringClient:
            """A transport whose teardown keeps awaiting after cancellation is
            requested — the wedged-TLS case a per-client wait_for cannot bound."""

            async def aclose(self):
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await asyncio.sleep(linger)
                    raise

        monkeypatch.setattr(_clients, "_POOL", {"lingering": LingeringClient()})

        start = time.monotonic()
        await _clients.aclose_all()
        elapsed = time.monotonic() - start

        assert elapsed < 0.3, f"shutdown waited on the lingering close ({elapsed:.2f}s)"

        # Let the orphaned close finish so it isn't garbage-collected while
        # still pending, which would log a spurious warning at loop close.
        await asyncio.sleep(linger)
