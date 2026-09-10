"""Per-provider counters and optional request logging.

In-process operator metrics: no third-party dependency, no endpoint, no
persistence. ``snapshot()`` names every counter; ``grep stats.incr`` finds who
moves them. An agent sees it only through ``server.get_server_stats``, gated on
``ENABLE_DEBUG_TOOLS``.

Callers key by the module's cache namespace, so its cache and HTTP counters
share one row. ``incr`` is an unsynchronised read-modify-write: a count raced
from an ``asyncio.to_thread`` worker can be lost.
"""

import sys
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..util import config

if TYPE_CHECKING:
    from .throttle import Throttle

_counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))


@dataclass(frozen=True)
class Quota:
    """A provider's last advertised budget. ``deadline`` is monotonic; all fields optional."""

    limit: int | None
    remaining: int | None
    deadline: float | None

    def blocked_for(self, now: float) -> float | None:
        """Seconds until refill when spent, else None. No deadline never blocks."""
        if self.remaining is None or self.remaining > 0 or self.deadline is None:
            return None
        return remaining if (remaining := self.deadline - now) > 0 else None


_quotas: dict[str, Quota] = {}

# The package root, not `net.`: `throttles()` scans the whole package, so a
# narrower prefix silently samples no provider at all.
_PACKAGE_PREFIX = f"{__name__.split('.', 1)[0]}."


def incr(provider: str, metric: str) -> None:
    """Increment a per-provider counter."""
    _counters[provider][metric] += 1


def record_quota(
    provider: str, *, limit: int | None, remaining: int | None, reset_seconds: float | None
) -> None:
    """Store the budget a response advertised. ``reset_seconds`` is seconds-until-refill."""
    if limit is None and remaining is None:
        return
    deadline = None if reset_seconds is None else time.monotonic() + reset_seconds
    _quotas[provider] = Quota(limit=limit, remaining=remaining, deadline=deadline)


def quota_refusal(provider: str) -> tuple[float, int | None] | None:
    """``(seconds_to_wait, limit)`` when the budget is spent, else None."""
    quota = _quotas.get(provider)
    if quota is None or (wait := quota.blocked_for(time.monotonic())) is None:
        return None
    return wait, quota.limit


def debug_requests_enabled() -> bool:
    """Whether ``DEBUG_REQUESTS`` is enabled; re-read per call, so it flips without a restart."""
    return config.flag("DEBUG_REQUESTS")


def log_request(provider: str, url: str, wait_seconds: float) -> None:
    """Log a throttled GET to stderr when ``DEBUG_REQUESTS`` is enabled.

    stderr deliberately: stdout carries the JSON-RPC stream.
    """
    if not debug_requests_enabled():
        return
    print(
        f"[academic-tools] {provider} GET {url} (throttle wait {wait_seconds:.3f}s)",
        file=sys.stderr,
        flush=True,
    )


# A string, not an import: `throttle` imports `stats`. Invariant: it tracks
# throttle.py's path — stale, `_is_throttle` matches nothing and `throttles()`
# empties silently, which the discovery tests in tests/net/test_stats.py catch.
_THROTTLE_MODULE = f"{_PACKAGE_PREFIX}net.throttle"


def _is_throttle(value: object) -> bool:
    """Whether ``value`` is a ``Throttle``, without importing the class."""
    return any(
        cls.__name__ == "Throttle" and cls.__module__ == _THROTTLE_MODULE
        for cls in type(value).__mro__
    )


def throttles() -> Iterator["Throttle"]:
    """Yield every ``Throttle`` held by an already-imported package module.

    Scanned, never imported: sampling a provider must not load it. Any
    attribute qualifies, not just ``_throttle``, so a module that grows a second
    throttle stays in the reset seam and the in-flight sample; deduped by
    identity, since one instance may be re-exported.
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

    ``{"providers": {<namespace>: {<counter>: int}}, "env_file": str | None}``.

    ``env_file`` is the ``.env`` that won at import, or None — an operator's
    only view of which file their settings came from. ``cache_hits`` and
    ``negative_hits`` count lookups served from disk, ``cache_misses`` those
    booked at the fetch on the way upstream, so no lookup moves two of them;
    ``http_calls``, ``http_retries``, ``backpressure_refusals`` and
    ``cache_write_failures`` are cumulative since process start or the last
    ``reset()``; ``in_flight`` is sampled live and summed over every
    ``Throttle`` in the namespace. Rows are copies, so mutating the result
    cannot corrupt the counters.

    ``quota`` appears only where a provider advertises one, as
    ``{limit, remaining, resets_in_seconds}``.
    """
    out: dict[str, Any] = {provider: dict(metrics) for provider, metrics in list(_counters.items())}

    for throttle in throttles():
        # Summed, not assigned: a namespace may own more than one throttle.
        row = out.setdefault(throttle.namespace, {})
        row["in_flight"] = row.get("in_flight", 0) + throttle.pending

    now = time.monotonic()
    for provider, quota in list(_quotas.items()):
        # An interval, not the monotonic deadline, which means nothing outside this process.
        resets_in = None if quota.deadline is None else max(0.0, quota.deadline - now)
        out.setdefault(provider, {})["quota"] = {
            "limit": quota.limit,
            "remaining": quota.remaining,
            "resets_in_seconds": resets_in,
        }

    return {
        "providers": out,
        "env_file": str(config.ENV_FILE) if config.ENV_FILE else None,
    }


def reset() -> None:
    """Drop every counter and quota row; the throttles' ``in_flight`` is untouched."""
    _counters.clear()
    _quotas.clear()
