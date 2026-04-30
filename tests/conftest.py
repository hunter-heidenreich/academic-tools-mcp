"""Test isolation for module-level pooled state.

The persistent ``httpx.AsyncClient`` pool, single-flight registries, and
backpressure counters all live as module-level state in
``academic_tools_mcp._clients`` and the per-provider modules. Without a
reset between tests, a stale client from one test (often a MagicMock
with the wrong canned response) is reused by the next test, which
either fails confusingly or — worse — passes for the wrong reason.

This autouse fixture clears all of that before each test runs.
"""

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _reset_pooled_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset pooled HTTP clients and per-provider in-flight state.

    Runs before every test in the suite. Idempotent and cheap.
    """
    from academic_tools_mcp import _clients, _singleflight

    # Wipe the per-provider client cache so any test that monkeypatches
    # httpx.AsyncClient sees a fresh build on first use.
    _clients._clients.clear()

    # For every provider module that has its own backpressure counter
    # and single-flight registry, zero them out. Tests that hit the
    # error path can leave _pending elevated if they raise before the
    # finally block (they shouldn't, but defence in depth).
    for module_name in (
        "arxiv",
        "openalex",
        "biorxiv",
        "crossref",
        "opencitations",
        "wikipedia",
    ):
        try:
            module: Any = __import__(
                f"academic_tools_mcp.{module_name}", fromlist=[module_name]
            )
        except ImportError:
            continue
        if hasattr(module, "_pending"):
            monkeypatch.setattr(module, "_pending", 0, raising=False)
        if hasattr(module, "_single_flight"):
            monkeypatch.setattr(
                module, "_single_flight", _singleflight.SingleFlight(),
                raising=False,
            )
