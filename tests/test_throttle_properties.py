"""Property-based tests for the pacing arithmetic in ``_throttle``.

Three invariants the module states as prose, held over generated inputs:

1. **Pacing.** Successive starts for one key are ``min_gap_seconds`` apart for
   any interleaving of keys; in global mode the key is ignored entirely.
2. **Admission.** For any arrival pattern, ``max_concurrent`` bodies run at
   once at most, ``pending`` stays inside ``max_pending``, every refusal is a
   ``LocalBackpressureError``, and ``pending`` returns to zero.
3. **Pruning.** ``_prune`` never exceeds the cap, never invents an entry, and —
   whenever the live entries fit — drops only expired ones. That last clause is
   what makes the age sweep semantics-preserving rather than a heuristic.

Pacing runs on a fake clock so it can assert exact arithmetic. ``asyncio.run``
inside a sync ``@given`` follows ``test_http_properties``: a function-scoped
async fixture would be reused across examples.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from academic_tools_mcp import _http, _throttle
from academic_tools_mcp._throttle import _MAX_TRACKED_HOSTS, Throttle

_REAL_SLEEP = asyncio.sleep

_GAPS = st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False)
_KEY_SEQUENCES = st.lists(st.sampled_from(["a", "b", "c"]), min_size=1, max_size=12)


class _FakeClock:
    """A monotonic clock that only advances when the code under test sleeps."""

    def __init__(self) -> None:
        # Starts at zero deliberately: a clock reading 0.0 is a real instant,
        # not "this key was never seen", and the gap check must tell them apart.
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        # Still yield: the reservation design depends on the sleep being a real
        # suspension point, outside the lock.
        await _REAL_SLEEP(0)


@contextlib.contextmanager
def _fake_clock():
    """Swap ``_throttle``'s own ``time`` / ``asyncio`` bindings for a fake clock.

    Scoped to the module's globals rather than patching ``asyncio.sleep``
    process-wide, which would also silence the test harness's own waits.
    """
    clock = _FakeClock()
    real_time, real_asyncio = _throttle.time, _throttle.asyncio
    _throttle.time = SimpleNamespace(monotonic=clock.monotonic)
    _throttle.asyncio = SimpleNamespace(
        sleep=clock.sleep,
        Semaphore=asyncio.Semaphore,
        Lock=asyncio.Lock,
    )
    try:
        yield clock
    finally:
        _throttle.time, _throttle.asyncio = real_time, real_asyncio


@settings(max_examples=50)
@given(gap=_GAPS, keys=_KEY_SEQUENCES, per_host=st.booleans())
def test_starts_for_one_key_are_never_closer_than_the_gap(gap, keys, per_host):
    async def scenario(clock) -> dict[str, list[float]]:
        t = Throttle(
            namespace="probe",
            label="Probe",
            max_concurrent=1,
            min_gap_seconds=gap,
            max_pending=len(keys) + 1,
            per_host=per_host,
        )
        starts: dict[str, list[float]] = {}
        for key in keys:
            async with t.slot(f"http://{key}.example/x"):
                starts.setdefault(key, []).append(clock.now)
        return starts

    with _fake_clock() as clock:
        starts = asyncio.run(scenario(clock))

    if not per_host:
        # One timestamp for the whole stream: flatten and pace as one series.
        starts = {"": sorted(v for series in starts.values() for v in series)}

    # The epsilon is float addition on a clock value, not slack in the gap:
    # ``now + gap`` rounds, so the recorded delta can land an ulp under it.
    for key, series in starts.items():
        for earlier, later in itertools.pairwise(series):
            assert later - earlier >= gap - 1e-9, f"{key}: {later - earlier} < {gap}"


@settings(max_examples=25, deadline=None)
@given(
    callers=st.integers(min_value=1, max_value=12),
    max_concurrent=st.integers(min_value=1, max_value=4),
    max_pending=st.integers(min_value=1, max_value=6),
)
def test_admission_limits_hold_for_any_arrival_pattern(callers, max_concurrent, max_pending):
    async def scenario() -> tuple[int, int, list[BaseException]]:
        t = Throttle(
            namespace="probe",
            label="Probe",
            max_concurrent=max_concurrent,
            min_gap_seconds=0.0,
            max_pending=max_pending,
        )
        in_flight = 0
        peak_in_flight = 0
        peak_pending = 0

        async def worker() -> None:
            nonlocal in_flight, peak_in_flight, peak_pending
            async with t.slot("http://a.example/x"):
                in_flight += 1
                peak_in_flight = max(peak_in_flight, in_flight)
                peak_pending = max(peak_pending, t.pending)
                await asyncio.sleep(0)
                in_flight -= 1

        results = await asyncio.gather(*[worker() for _ in range(callers)], return_exceptions=True)
        assert t.pending == 0
        return peak_in_flight, peak_pending, [r for r in results if isinstance(r, BaseException)]

    peak_in_flight, peak_pending, errors = asyncio.run(scenario())

    assert peak_in_flight <= max_concurrent
    assert peak_pending <= max_pending
    for error in errors:
        assert isinstance(error, _http.LocalBackpressureError)


@settings(max_examples=50)
@given(
    ages=st.lists(
        st.floats(min_value=-30.0, max_value=30.0, allow_nan=False),
        max_size=_MAX_TRACKED_HOSTS + 20,
    ),
    gap=_GAPS,
)
def test_prune_bounds_the_map_without_dropping_a_live_entry(ages, gap):
    now = 1000.0
    t = Throttle(
        namespace="probe",
        label="Probe",
        max_concurrent=1,
        min_gap_seconds=gap,
        per_host=True,
    )
    t._last_start = {f"h{i}.example": now + age for i, age in enumerate(ages)}
    before = dict(t._last_start)
    # An entry can still pace a caller arriving at `now` while now - t < gap.
    live = {k for k, v in before.items() if now - v < gap}

    t._prune(now)

    assert len(t._last_start) <= _MAX_TRACKED_HOSTS
    assert t._last_start.items() <= before.items(), "prune invented or edited an entry"
    if len(before) <= _MAX_TRACKED_HOSTS:
        assert t._last_start == before, "prune ran below the cap"
    elif len(live) <= _MAX_TRACKED_HOSTS:
        assert live <= set(t._last_start), "the age sweep dropped a live entry"


def test_prune_only_falls_back_to_dropping_live_entries_past_the_cap():
    """The lossy branch is reachable, so the property above is not vacuous."""
    t = Throttle(
        namespace="probe",
        label="Probe",
        max_concurrent=1,
        min_gap_seconds=3600.0,
        per_host=True,
    )
    now = 1000.0
    t._last_start = {f"h{i}.example": now for i in range(_MAX_TRACKED_HOSTS + 5)}

    t._prune(now)

    assert len(t._last_start) == _MAX_TRACKED_HOSTS


@pytest.mark.asyncio
async def test_fake_clock_is_restored():
    """The patch is scoped: a leak would freeze every later test's pacing."""
    with _fake_clock():
        pass
    assert _throttle.time.monotonic() > 0
    assert _throttle.asyncio is asyncio
