"""Per-provider counters and optional request logging.

In-process metrics for an operator: no dependencies, no endpoint, no
persistence. ``snapshot()`` names every counter; ``grep _stats.incr`` finds
who moves them. Not an MCP tool — agents must not branch on operational data.

Two invariants callers must hold:

- **Key by the module's cache namespace**, so its cache and HTTP counters
  share one row.
- **``incr`` is event-loop-thread only** — an unsynchronised
  read-modify-write, so a caller under ``asyncio.to_thread`` loses counts.
"""

import sys
from collections import defaultdict
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:
    from ._throttle import Throttle

_counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

_PACKAGE_PREFIX = f"{__name__.rsplit('.', 1)[0]}."


def incr(provider: str, metric: str) -> None:
    """Increment a per-provider counter."""
    _counters[provider][metric] += 1


def debug_requests_enabled() -> bool:
    """Whether DEBUG_REQUESTS is set; re-read per call so it flips without a restart."""
    return config.flag("DEBUG_REQUESTS")


def log_request(provider: str, url: str, wait_seconds: float) -> None:
    """Log a throttled GET to stderr when DEBUG_REQUESTS is enabled.

    stderr deliberately — MCP servers speak JSON-RPC on stdout, so
    anything we write there would corrupt the protocol stream.
    """
    if not debug_requests_enabled():
        return
    print(
        f"[academic-tools] {provider} GET {url} (throttle wait {wait_seconds:.3f}s)",
        file=sys.stderr,
        flush=True,
    )


_THROTTLE_MODULE = f"{_PACKAGE_PREFIX}_throttle"


def _is_throttle(value: object) -> bool:
    """Whether ``value`` is a ``Throttle``, without importing the class."""
    return any(
        cls.__name__ == "Throttle" and cls.__module__ == _THROTTLE_MODULE
        for cls in type(value).__mro__
    )


def throttles() -> Iterator["Throttle"]:
    """Yield every ``Throttle`` instance held by an already-imported package module.

    Scanned, never imported: sampling a provider must not load it. Every
    attribute qualifies, not just one named ``_throttle``, so a module that
    grows a second throttle cannot drop out of the reset seam or the in-flight
    sample — deduped by identity, since one instance may be re-exported.
    """
    seen: set[int] = set()
    for name, module in list(sys.modules.items()):
        if not name.startswith(_PACKAGE_PREFIX) or module is None:
            continue
        for value in list(vars(module).values()):
            if not _is_throttle(value) or id(value) in seen:
                continue
            seen.add(id(value))
            yield value


def snapshot() -> dict[str, Any]:
    """Return the counters plus a live in-flight sample.

    ``{"providers": {<namespace>: {<counter>: int}}}``. ``cache_hits`` and
    ``negative_hits`` count lookups served from disk; ``cache_misses`` counts
    lookups that went upstream, booked at the fetch, so the three series
    partition served lookups rather than overlapping. Alongside them:
    ``http_calls``, ``http_retries``, ``backpressure_refusals`` and
    ``cache_write_failures``,
    cumulative since process start (or the last ``reset()``), plus an
    ``in_flight`` sampled live and summed over every ``Throttle`` the namespace
    owns. Rows are copies, so mutating the result cannot corrupt the counters.
    """
    out: dict[str, dict[str, int]] = {
        provider: dict(metrics) for provider, metrics in list(_counters.items())
    }

    for throttle in throttles():
        # Summed, not assigned: a namespace may own more than one throttle.
        row = out.setdefault(throttle.namespace, {})
        row["in_flight"] = row.get("in_flight", 0) + throttle.pending

    return {"providers": out}


def reset() -> None:
    """Zero every counter. Used by tests; safe to call at runtime."""
    _counters.clear()
