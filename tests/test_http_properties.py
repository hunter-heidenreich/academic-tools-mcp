"""Property-based tests for the retry/backoff arithmetic in ``_http``.

Two invariants here are stronger than any example, and both are stated as
prose in the module docstrings today:

1. ``_retry_after_seconds`` returns either ``None`` or a value that is safe to
   hand to ``asyncio.sleep`` — finite and strictly positive. Every other
   outcome (missing, malformed, a past date, ``inf``, ``-5``) collapses to
   ``None`` so the caller's own backoff applies.
2. Every value ``get_with_retry`` sleeps for sits in
   ``[backoff_seconds, _MAX_RETRY_AFTER_SECONDS]`` — the provider's own
   throttle gap is a floor no server can talk us under, and the ceiling is
   what stops a misconfigured ``Retry-After: 86400`` pinning the throttle.

Async paths run via ``asyncio.run`` inside a sync ``@given`` rather than
``@pytest.mark.asyncio``: hypothesis re-runs the body many times, and a
function-scoped async fixture would be reused across examples.
"""

import asyncio
import math
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import _http

# Header values must be latin-1 encodable and free of control characters, or
# httpx rejects them before _retry_after_seconds ever sees the string.
_HEADER_TEXT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    max_size=40,
)

# Drawn explicitly rather than left to the text/float strategies: hypothesis
# will not reliably produce "inf" or "nan" from st.floats().map(str), and those
# are precisely the values that parse as floats without being a wait
# instruction. Verified by mutation — deleting the isfinite guard fails only
# once these are in the pool.
_PATHOLOGICAL = st.sampled_from(
    ["inf", "-inf", "nan", "NaN", "Infinity", "0", "-1", "1e3", "  90  ", "", "soon"]
)

# The realistic shapes alongside arbitrary junk: both RFC 9110 forms, the
# numeric edge cases, and free text.
_RETRY_AFTER_VALUES = st.one_of(
    _PATHOLOGICAL,
    _HEADER_TEXT,
    st.integers(min_value=-10_000, max_value=10_000).map(str),
    st.floats(allow_nan=True, allow_infinity=True, width=32).map(str),
    st.integers(min_value=-86_400, max_value=86_400).map(
        lambda offset: format_datetime(datetime.now(UTC) + timedelta(seconds=offset), usegmt=True)
    ),
)


def _response_with(value: str | None, status: int = 429) -> httpx.Response:
    headers = {} if value is None else {"retry-after": value}
    return httpx.Response(status, headers=headers, request=httpx.Request("GET", "https://x"))


@given(value=_RETRY_AFTER_VALUES)
def test_parsed_retry_after_is_none_or_a_usable_sleep(value):
    """No parse path can yield a value that breaks ``asyncio.sleep``.

    A NaN sleeps forever, an infinity raises, and a negative one is not an
    instruction to wait — each must read as "no advice" instead.
    """
    result = _http._retry_after_seconds(_response_with(value))
    if result is None:
        return
    assert isinstance(result, float)
    assert math.isfinite(result)
    assert result > 0


@given(value=_RETRY_AFTER_VALUES)
def test_the_agent_facing_hint_never_exceeds_the_ceiling(value):
    """``error_dict``'s hint honours the same ceiling as the internal retry
    path — the invariant the "change one, change both" comment pins."""
    exc = httpx.HTTPStatusError(
        "429",
        request=httpx.Request("GET", "https://x"),
        response=_response_with(value),
    )
    result = _http.error_dict("Test", exc)
    if "retry_after_seconds" not in result:
        return
    hint = result["retry_after_seconds"]
    assert 0 < hint <= _http._MAX_RETRY_AFTER_SECONDS


class _AlwaysRetryable:
    """Replays one retryable response forever, recording each request."""

    def __init__(self, retry_after: str | None, status: int = 503):
        self._retry_after = retry_after
        self._status = status
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        return _response_with(self._retry_after, status=self._status)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50)
@given(
    retry_after=st.one_of(st.none(), _RETRY_AFTER_VALUES),
    backoff=st.floats(min_value=0.0, max_value=5_000.0, allow_nan=False, allow_infinity=False),
    max_attempts=st.integers(min_value=1, max_value=5),
)
def test_every_sleep_sits_between_the_backoff_floor_and_the_ceiling(
    monkeypatch, retry_after, backoff, max_attempts
):
    """The sleep formula's two bounds, over arbitrary inputs.

    Floor: ``Throttle.get`` passes the provider's own gap as ``backoff_seconds``
    so a retry can never undercut the documented rate, whatever the server
    asks for. Ceiling: ``_MAX_RETRY_AFTER_SECONDS``, so a bad header cannot
    hold the throttle slot open for hours.
    """
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(_http.asyncio, "sleep", fake_sleep)

    client = _AlwaysRetryable(retry_after)
    asyncio.run(
        _http.get_with_retry(client, "u", max_attempts=max_attempts, backoff_seconds=backoff)
    )

    # One sleep per failed-but-not-final attempt; the last failure is returned.
    assert client.calls == max_attempts
    assert len(slept) == max_attempts - 1
    for value in slept:
        assert math.isfinite(value)
        assert value <= _http._MAX_RETRY_AFTER_SECONDS
        assert value >= min(backoff, _http._MAX_RETRY_AFTER_SECONDS)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=25)
@given(
    backoff=st.floats(min_value=0.01, max_value=100.0, allow_nan=False, allow_infinity=False),
    max_attempts=st.integers(min_value=2, max_value=5),
)
def test_backoff_is_non_decreasing_across_attempts(monkeypatch, backoff, max_attempts):
    """Successive retries never tighten the gap.

    The point of the exponential term is that later retries straddle a
    cooldown instead of all landing inside the same throttled window; a
    shrinking gap would defeat it.
    """
    assume(backoff * 2 ** (max_attempts - 2) <= _http._MAX_RETRY_AFTER_SECONDS)
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(_http.asyncio, "sleep", fake_sleep)

    # No Retry-After, so the backoff term alone drives the sleep.
    client = _AlwaysRetryable(None)
    asyncio.run(
        _http.get_with_retry(client, "u", max_attempts=max_attempts, backoff_seconds=backoff)
    )

    assert slept == sorted(slept)
    assert slept[0] == pytest.approx(backoff)
