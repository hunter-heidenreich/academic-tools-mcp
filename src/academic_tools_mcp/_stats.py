"""Per-provider counters and optional request logging.

In-process metrics for operational visibility. ``snapshot()`` returns a
plain nested dict suitable for printing or logging; no external
dependencies, no HTTP endpoint, no persistence across restarts.

Wired into:
  - cache.py: ``cache_hits`` / ``cache_misses`` on ``get``,
    ``negative_hits`` on ``get_negative``.
  - per-provider ``_throttled_get``: ``http_calls`` after the throttle
    gap clears, ``backpressure_refusals`` when the burst cap rejects.
  - _http.get_with_retry: ``http_retries`` per transient retry attempt.

Counters are keyed by provider name (the same string each module uses
for its cache namespace), so cache and HTTP stats line up cleanly.

Not exposed as an MCP tool — agents should not branch on operational
data. Operators inspect via ``_stats.snapshot()`` from a debug script
or a future internal endpoint.

DEBUG_REQUESTS env var (``1`` / ``true`` / ``yes`` / ``on``) enables
per-request stderr logging of throttle waits. Runtime-checked so it can
be flipped without restarting.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from typing import Any


_counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))


# Names of provider modules whose ``_pending`` counter we sample for
# real-time in-flight reporting. Kept in sync with the modules listed
# in tests/conftest.py's reset fixture.
_PROVIDER_MODULES = (
    "arxiv",
    "openalex",
    "biorxiv",
    "crossref",
    "opencitations",
    "wikipedia",
    "acl_anthology",
)


def incr(provider: str, metric: str, n: int = 1) -> None:
    """Increment a per-provider counter. Cheap and lock-free.

    Provider names are free-form but should match the cache namespace
    used by that module ("arxiv", "openalex", etc.) so HTTP and cache
    counters line up under the same key in the snapshot.
    """
    _counters[provider][metric] += n


def debug_requests_enabled() -> bool:
    """Re-read DEBUG_REQUESTS from the environment on every call.

    Lets an operator flip the flag without restarting the server, and
    lets tests monkeypatch ``os.environ`` per-case without a fixture.
    """
    return os.environ.get("DEBUG_REQUESTS", "").lower() in ("1", "true", "yes", "on")


def log_request(provider: str, url: str, wait_seconds: float) -> None:
    """Log a throttled GET to stderr when DEBUG_REQUESTS is enabled.

    stderr deliberately — MCP servers speak JSON-RPC on stdout, so
    anything we write there would corrupt the protocol stream.
    """
    if not debug_requests_enabled():
        return
    print(
        f"[academic-tools] {provider} GET {url} "
        f"(throttle wait {wait_seconds:.3f}s)",
        file=sys.stderr,
        flush=True,
    )


def snapshot() -> dict[str, Any]:
    """Return a snapshot of counters plus live in-flight counts.

    Shape::

        {
          "providers": {
            "arxiv": {
              "cache_hits": 42,
              "cache_misses": 5,
              "negative_hits": 1,
              "http_calls": 5,
              "http_retries": 0,
              "backpressure_refusals": 0,
              "in_flight": 0,
            },
            ...
          }
        }

    Counter values are cumulative since process start (or the last
    ``reset()``). ``in_flight`` is sampled live from each provider's
    ``_pending`` counter.
    """
    out: dict[str, dict[str, int]] = {}
    for provider, metrics in _counters.items():
        out[provider] = dict(metrics)

    # Sample live in-flight from each provider module. Modules with no
    # _pending attribute (or that haven't been imported yet) are skipped.
    for module_name in _PROVIDER_MODULES:
        try:
            mod = __import__(
                f"academic_tools_mcp.{module_name}", fromlist=[module_name]
            )
        except ImportError:
            continue
        pending = getattr(mod, "_pending", None)
        if pending is None:
            continue
        out.setdefault(module_name, {})["in_flight"] = int(pending)

    return {"providers": out}


def reset() -> None:
    """Zero every counter. Used by tests; safe to call at runtime."""
    _counters.clear()
