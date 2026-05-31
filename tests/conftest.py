"""Test isolation for module-level pooled state.

The persistent ``httpx.AsyncClient`` pool, single-flight registries, and
backpressure counters all live as module-level state in
``academic_tools_mcp._clients`` and the per-provider modules. Without a
reset between tests, a stale client from one test (often a MagicMock
with the wrong canned response) is reused by the next test, which
either fails confusingly or — worse — passes for the wrong reason.

This autouse fixture clears all of that before each test runs.
"""

import asyncio
import importlib
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _reset_pooled_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset pooled HTTP clients and per-provider in-flight state.

    Runs before every test in the suite. Idempotent and cheap.
    """
    from academic_tools_mcp import _clients, _singleflight, _stats, cache_search

    # Wipe the per-provider client cache so any test that monkeypatches
    # httpx.AsyncClient sees a fresh build on first use.
    _clients._clients.clear()

    # Reset the in-memory search-index memo. It is keyed by index.json's stat
    # signature, and a per-test tmp cache root starts with no index file (sig
    # None) — which would otherwise collide with a prior test's None-keyed memo
    # and serve stale entries from a different corpus.
    monkeypatch.setattr(cache_search, "_INDEX_MEMO", None, raising=False)
    monkeypatch.setattr(cache_search, "_INDEX_MEMO_SIG", None, raising=False)

    # Zero the stats counters so a test that asserts on hit/miss totals
    # isn't contaminated by counts from prior tests.
    _stats.reset()

    # For every provider module reset the per-throttle state. The locks
    # and semaphores are rebuilt because asyncio.Lock / Semaphore bind to
    # the running event loop on first await — a stale instance from the
    # previous test's loop fails with a "bound to a different event loop"
    # error if reused. Counters are zeroed for the same reason as before:
    # an error path that raised before the finally block could otherwise
    # leak _pending into the next test.
    for module_path in (
        "providers.arxiv",
        "providers.openalex",
        "providers.biorxiv",
        "providers.crossref",
        "providers.opencitations",
        "providers.wikipedia",
        "providers.acl_anthology",
        "oa_download",
    ):
        try:
            module: Any = importlib.import_module(f"academic_tools_mcp.{module_path}")
        except ImportError:
            continue
        if hasattr(module, "_pending"):
            monkeypatch.setattr(module, "_pending", 0, raising=False)
        if hasattr(module, "_last_request_time"):
            monkeypatch.setattr(module, "_last_request_time", 0.0, raising=False)
        if hasattr(module, "_request_lock"):
            monkeypatch.setattr(
                module,
                "_request_lock",
                asyncio.Lock(),
                raising=False,
            )
        if hasattr(module, "_request_sem") and hasattr(module, "_MAX_CONCURRENT"):
            monkeypatch.setattr(
                module,
                "_request_sem",
                asyncio.Semaphore(module._MAX_CONCURRENT),
                raising=False,
            )
        if hasattr(module, "_single_flight"):
            monkeypatch.setattr(
                module,
                "_single_flight",
                _singleflight.SingleFlight(),
                raising=False,
            )
