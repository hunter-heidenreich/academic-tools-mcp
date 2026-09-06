"""Test isolation for module-level pooled state.

The persistent ``httpx.AsyncClient`` pool, single-flight registries, and
backpressure counters all live as module-level state in
``academic_tools_mcp._clients`` and the per-provider modules. Without a
reset between tests, a stale client from one test (often a MagicMock
with the wrong canned response) is reused by the next test, which
either fails confusingly or — worse — passes for the wrong reason.

This autouse fixture clears all of that before each test runs.
"""

import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest

_PACKAGE_PREFIX = "academic_tools_mcp."


def _imported_package_modules() -> Iterator[ModuleType]:
    """Yield every already-imported module of the package.

    A scan rather than a hand-maintained list of provider paths: that list had
    to stay in sync with ``_stats``' own copy and nothing enforced it. Imports
    nothing, so the fixture can't pull half the package into a test that never
    touches it.
    """
    for name, module in list(sys.modules.items()):
        if name.startswith(_PACKAGE_PREFIX):
            yield module


@pytest.fixture(autouse=True)
def _reset_pooled_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset pooled HTTP clients and per-provider in-flight state.

    Runs before every test in the suite. Idempotent and cheap.
    """
    from academic_tools_mcp import _clients, _singleflight, _stats

    # Wipe the per-provider client cache so any test that monkeypatches
    # httpx.AsyncClient sees a fresh build on first use. This drops the
    # clients without awaiting `_clients.aclose_all()` — a sync fixture can't
    # — so a test that built a *real* client (test_politeness reads the baked-in
    # User-Agent off one) leaks it. Harmless: an unused client has opened no
    # socket. A test that actually connects must close its own clients.
    _clients._POOL.clear()

    # Zero the stats counters so a test that asserts on hit/miss totals
    # isn't contaminated by counts from prior tests.
    _stats.reset()

    # Throttle.reset() rebuilds the lock + semaphore because asyncio.Lock /
    # Semaphore bind to the running event loop on first await — a stale
    # instance from the previous test's loop fails with a "bound to a different
    # event loop" error if reused — and zeroes pending / the last-start map so an
    # error path that raised before the finally block can't leak `pending` into
    # the next test. Same discovery seam the snapshot samples through, so a new
    # provider is covered here without an edit.
    for throttle in _stats.throttles():
        throttle.reset()

    # Single-flight registries and crossref's search gate hang off the module,
    # not the throttle, so they need the wider scan.
    for module in _imported_package_modules():
        # Crossref paces search separately from singles (different upstream
        # limit); its lock binds to the running event loop just as a
        # Throttle's does, so it needs the same per-test rebuild.
        reset_search_pacing = getattr(module, "reset_search_pacing", None)
        if reset_search_pacing is not None:
            reset_search_pacing()
        if hasattr(module, "_single_flight"):
            monkeypatch.setattr(
                module,
                "_single_flight",
                _singleflight.SingleFlight(),
                raising=False,
            )


@pytest.fixture(autouse=True)
def _isolate_cache_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Point the on-disk cache at this test's private ``tmp_path``.

    A safety net, not a convenience: 12 of the test modules never patched
    ``cache.CACHE_ROOT`` themselves, so a single missed monkeypatch wrote
    into the operator's real ``.cache/`` (which reaches tens of GB on a
    working install). Tests that need a different layout still override this
    — a later ``monkeypatch.setattr`` in the test body wins, and the many
    that set it to this same ``tmp_path`` are now simply redundant.

    ``cache_search`` reads ``cache.CACHE_ROOT`` at call time rather than
    caching it, so patching the one attribute covers the search index too.
    """
    from academic_tools_mcp import cache

    monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)


@pytest.fixture(autouse=True)
def _reset_conversion_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset ``papers``' module-level conversion state between tests.

    ``_global_convert_lock`` binds to the running event loop the first time
    it is *contended*, so a lock left over from a previous test's loop is a
    latent "bound to a different event loop" failure. Today that is masked
    by the ``if _global_convert_lock.locked()`` short-circuit in
    ``convert_pdf`` (which means the lock is only ever taken uncontended),
    so this fixture also stops that accident from being load-bearing.

    ``_current_conversion`` and ``_section_locks`` are reset for the same
    reason ``_reset_pooled_state`` resets throttles: a test that errors
    mid-conversion must not leak "busy" state into the next one.
    """
    import asyncio

    from academic_tools_mcp import papers

    monkeypatch.setattr(papers, "_global_convert_lock", asyncio.Lock())
    monkeypatch.setattr(papers, "_current_conversion", None)
    monkeypatch.setattr(papers, "_section_locks", type(papers._section_locks)())


@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly on any real outbound connection.

    The suite mocks at the ``httpx`` layer, so a stubbed-out client that is
    missed (or a code path that builds its own) would silently reach the
    live API — polite-pool budget spent from a test run, and a flake in CI
    that depends on someone else's rate limiter. Loopback stays open so a
    local fixture server remains possible.
    """
    import socket

    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def _is_local(address: Any) -> bool:
        if not isinstance(address, tuple) or not address:
            return True  # AF_UNIX and friends: not a network hop
        host = address[0]
        return host in ("127.0.0.1", "::1", "localhost", "")

    def guarded_connect(self: Any, address: Any) -> Any:
        if not _is_local(address):
            raise RuntimeError(
                f"Blocked real network connection to {address!r} during tests. "
                "Mock the provider's httpx client instead of reaching upstream."
            )
        return real_connect(self, address)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_local(address):
            raise RuntimeError(
                f"Blocked real network connection to {address!r} during tests. "
                "Mock the provider's httpx client instead of reaching upstream."
            )
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
