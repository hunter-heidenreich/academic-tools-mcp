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
    from .throttle import SubGap, Throttle

_counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))


@dataclass(frozen=True)
class Quota:
    """A provider's last advertised budget. ``deadline`` is monotonic; all fields optional.

    ``refused`` records that the response *was* a refusal, not just that it advertised an
    empty budget; both are stored, but only the first gates a later caller.
    """

    limit: int | None
    remaining: int | None
    deadline: float | None
    refused: bool = False

    def blocked_for(self, now: float) -> float | None:
        """Seconds until refill when refused *and* spent, else None. No deadline never blocks.

        Both conjuncts earn their place: without ``refused``, an advertised-empty budget
        locks out a provider that is still answering; without ``remaining``, a 429 from a
        momentary rate burst locks it out until the budget refills, hours later.

        Says *whether* a budget is spent, never *whom that stops* — that is
        ``throttle.Throttle.admit(metered=)``.
        """
        if not self.refused or self.deadline is None:
            return None
        if self.remaining is not None and self.remaining > 0:
            return None
        return wait if (wait := self.deadline - now) > 0 else None


_quotas: dict[str, Quota] = {}

# The package root, not `net.`: `pacers()` scans the whole package, so a
# narrower prefix silently samples no provider at all.
_PACKAGE_PREFIX = f"{__name__.split('.', 1)[0]}."


def incr(provider: str, metric: str) -> None:
    """Increment a per-provider counter."""
    _counters[provider][metric] += 1


def record_quota(
    provider: str,
    *,
    limit: int | None,
    remaining: int | None,
    reset_seconds: float | None,
    refused: bool = False,
) -> None:
    """Store the budget a response advertised. ``reset_seconds`` is seconds-until-refill.

    ``refused`` arms the local lockout; a header alone only populates the operator row.
    """
    if limit is None and remaining is None:
        return
    deadline = None if reset_seconds is None else time.monotonic() + reset_seconds
    _quotas[provider] = Quota(limit=limit, remaining=remaining, deadline=deadline, refused=refused)


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


# A string, not an import — and it cannot become one. `throttle` imports `stats`, so
# even a function-local `from .throttle import ...` registers as a real edge:
# tests/test_layering.py's AST walk descends into function bodies and its cycle check
# rejects it. Invariant: this tracks throttle.py's path — stale, `_is_pacer` matches
# nothing and `pacers()` empties silently, which the discovery tests catch.
_THROTTLE_MODULE = f"{_PACKAGE_PREFIX}net.throttle"


def _is_pacer(value: object) -> bool:
    """Whether ``value`` is a ``Throttle`` or a ``SubGap``, without importing either.

    Resolved through ``sys.modules`` rather than an import statement, so sampling a
    provider still cannot load one — and so this stays real ``isinstance``, subclasses
    included, rather than a walk matching class names.
    """
    module = sys.modules.get(_THROTTLE_MODULE)
    if module is None:
        return False
    return isinstance(value, (module.Throttle, module.SubGap))


def pacers() -> Iterator["Throttle | SubGap"]:
    """Yield every ``Throttle`` and ``SubGap`` held by an already-imported package module.

    The single discovery seam for both gate types: the in-flight sample reads it, and so
    does the per-test reset, so a provider that grows a gap needs no fixture edit. A gate
    reachable only from a local variable is invisible to it.

    Scanned, never imported: sampling a provider must not load it. Any attribute
    qualifies, not just ``_throttle``, so a module that grows a second gate is covered;
    deduped by identity, since one instance may be re-exported.
    """
    seen: set[int] = set()
    for name, module in list(sys.modules.items()):
        if not name.startswith(_PACKAGE_PREFIX) or module is None:
            continue
        for value in list(vars(module).values()):
            if not _is_pacer(value) or id(value) in seen:
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
    ``http_calls``, ``http_retries``, ``backpressure_refusals``,
    ``quota_refusals``, ``quota_window_ignored``, ``client_config_ignored`` and
    ``cache_write_failures`` are cumulative since process start or the last
    ``reset()``; ``in_flight`` is sampled live and summed over every gate in the
    namespace — throttles *and* sub-gaps. A caller queued on a gap has no socket
    open yet, so ``in_flight`` deliberately overstates live requests: it reports
    the number ``max_pending`` gates on. Rows are copies, so mutating the result
    cannot corrupt the counters.

    ``quota`` appears only where a provider advertises one, as
    ``{limit, remaining, resets_in_seconds}``.
    """
    out: dict[str, Any] = {provider: dict(metrics) for provider, metrics in list(_counters.items())}

    for gate in pacers():
        # Summed, not assigned: a namespace may own a throttle and a gap, or two of either.
        row = out.setdefault(gate.namespace, {})
        row["in_flight"] = row.get("in_flight", 0) + gate.pending

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
    """Drop every counter and quota row; the gates' ``in_flight`` is untouched."""
    _counters.clear()
    _quotas.clear()
