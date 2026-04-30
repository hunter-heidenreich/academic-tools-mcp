"""Tests for the shared HTTP error normalization helper."""

import httpx
import pytest

from academic_tools_mcp import _http


def _build_status_error(status: int, body: str = "", headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/api")
    response = httpx.Response(status, headers=headers, content=body.encode(), request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class TestErrorDict:
    def test_429_includes_retry_after_when_present(self):
        exc = _build_status_error(429, headers={"retry-after": "12"})
        result = _http.error_dict("Crossref", exc)
        assert "rate limit" in result["error"].lower()
        assert "Crossref" in result["error"]
        # Field is named *_seconds, so the value must be numeric — the
        # raw header string would TypeError if an agent passed it to
        # asyncio.sleep().
        assert result["retry_after_seconds"] == 12.0
        assert isinstance(result["retry_after_seconds"], float)

    def test_429_omits_retry_after_when_absent(self):
        exc = _build_status_error(429)
        result = _http.error_dict("OpenAlex", exc)
        assert "rate limit" in result["error"].lower()
        assert "retry_after_seconds" not in result

    def test_429_ignores_non_numeric_retry_after(self):
        # HTTP-date forms get dropped rather than returned as a string —
        # consistent with get_with_retry's behaviour.
        exc = _build_status_error(
            429, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}
        )
        result = _http.error_dict("Crossref", exc)
        assert "retry_after_seconds" not in result

    def test_5xx_marks_transient(self):
        for status in (500, 502, 503, 504):
            exc = _build_status_error(status)
            result = _http.error_dict("arXiv", exc)
            assert "server error" in result["error"].lower()
            assert "transient" in result["error"].lower()
            assert str(status) in result["error"]

    def test_other_4xx_includes_body_snippet(self):
        exc = _build_status_error(400, body="bad request: missing field foo")
        result = _http.error_dict("Crossref", exc)
        assert "400" in result["error"]
        assert "missing field foo" in result["error"]

    def test_body_snippet_is_truncated(self):
        long_body = "x" * 1000
        exc = _build_status_error(400, body=long_body)
        result = _http.error_dict("Crossref", exc)
        # Snippet capped at 200 chars; surrounding text adds a bit
        assert len(result["error"]) < 300

    def test_timeout_is_transient(self):
        exc = httpx.ReadTimeout("read timeout", request=httpx.Request("GET", "https://x"))
        result = _http.error_dict("Wikipedia", exc)
        assert "timed out" in result["error"].lower()
        assert "transient" in result["error"].lower()

    def test_connect_error(self):
        exc = httpx.ConnectError("dns failed", request=httpx.Request("GET", "https://x"))
        result = _http.error_dict("bioRxiv", exc)
        assert "network error" in result["error"].lower()
        assert "bioRxiv" in result["error"]

    def test_local_backpressure_is_retryable(self):
        # Backpressure is the local throttle saying "you're queueing
        # too deep, slow down" — it's transient and the agent should
        # back off and retry, not give up.
        exc = _http.LocalBackpressureError("arXiv", pending=5, max_pending=5)
        result = _http.error_dict("arXiv", exc)
        assert "backpressure" in result["error"].lower()
        assert "5" in result["error"]
        assert result["retryable"] is True
        assert result["backpressure"] is True

    def test_backpressure_surfaces_concrete_remediation(self):
        """The error must tell the agent how long to wait (the throttle
        gap) and how many parallel calls are safe (the cap), not just
        say 'backpressure'. Both are exposed as structured fields so
        agents can branch on them without parsing the message string."""
        exc = _http.LocalBackpressureError(
            "arXiv", pending=5, max_pending=5, min_gap_seconds=3.0
        )
        result = _http.error_dict("arXiv", exc)

        # Structured fields the agent can read directly.
        assert result["max_concurrency"] == 5
        assert result["retry_after_seconds"] == 3.0

        # Human-readable hint embedded in the message for agents that
        # only parse the error string.
        msg = result["error"]
        assert "≥3.00s" in msg or "3.00s" in msg
        assert "≤5" in msg or "5 parallel" in msg

    def test_backpressure_with_zero_gap_omits_retry_after(self):
        """Providers like ACL Anthology have no documented rate limit
        and run with min_gap=0; the error should still be useful (cap
        + retry hint) without claiming a fictional retry interval."""
        exc = _http.LocalBackpressureError(
            "ACL Anthology", pending=5, max_pending=5, min_gap_seconds=0.0
        )
        result = _http.error_dict("ACL Anthology", exc)

        assert result["max_concurrency"] == 5
        assert "retry_after_seconds" not in result, (
            "no advertised gap → no retry_after, so agents don't pin "
            "to a fabricated interval"
        )


class TestExceptionTuple:
    def test_includes_status_timeout_and_request(self):
        # The contract: callers use HTTPX_ERRORS to narrow their except clause.
        assert httpx.HTTPStatusError in _http.HTTPX_ERRORS
        assert httpx.TimeoutException in _http.HTTPX_ERRORS
        assert httpx.RequestError in _http.HTTPX_ERRORS

    def test_includes_local_backpressure(self):
        # Caller `try/except HTTPX_ERRORS` blocks must catch our local
        # backpressure error too so it routes through error_dict like
        # any other transient failure.
        assert _http.LocalBackpressureError in _http.HTTPX_ERRORS


# ---------------------------------------------------------------------------
# get_with_retry
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal AsyncClient stub. Plays back a sequence of outcomes for
    successive ``get`` calls. An outcome is either an ``httpx.Response``
    (returned) or an ``Exception`` (raised).
    """

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _response(status: int, headers: dict | None = None) -> httpx.Response:
    request = httpx.Request("GET", "https://example.com/api")
    return httpx.Response(status, headers=headers or {}, request=request)


class TestGetWithRetry:
    """One transparent retry on transient failures. Sleep is patched out
    so each test runs in microseconds; what matters is the call count
    and the value passed to asyncio.sleep, not the wall time.
    """

    @pytest.fixture(autouse=True)
    def _patch_sleep(self, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(_http.asyncio, "sleep", fake_sleep)
        self.slept = slept

    @pytest.mark.asyncio
    async def test_returns_2xx_on_first_attempt_no_sleep(self):
        client = _FakeClient([_response(200)])
        resp = await _http.get_with_retry(client,"u")
        assert resp.status_code == 200
        assert len(client.calls) == 1
        assert self.slept == []

    @pytest.mark.asyncio
    async def test_does_not_retry_on_404(self):
        # 404 is the caller's responsibility (real "not found"); we
        # must not waste a retry on it.
        client = _FakeClient([_response(404)])
        resp = await _http.get_with_retry(client,"u")
        assert resp.status_code == 404
        assert len(client.calls) == 1

    @pytest.mark.asyncio
    async def test_does_not_retry_on_400(self):
        client = _FakeClient([_response(400)])
        resp = await _http.get_with_retry(client,"u")
        assert resp.status_code == 400
        assert len(client.calls) == 1

    @pytest.mark.asyncio
    async def test_retries_on_429_and_returns_success(self):
        client = _FakeClient([_response(429), _response(200)])
        resp = await _http.get_with_retry(client,"u")
        assert resp.status_code == 200
        assert len(client.calls) == 2
        assert self.slept == [1.0]  # default backoff_seconds

    @pytest.mark.asyncio
    async def test_retries_on_503_and_returns_success(self):
        client = _FakeClient([_response(503), _response(200)])
        resp = await _http.get_with_retry(client,"u")
        assert resp.status_code == 200
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_retries_on_each_5xx(self):
        # Spot-check that the standard 5xx range is all retryable.
        for status in (500, 502, 503, 504):
            client = _FakeClient([_response(status), _response(200)])
            resp = await _http.get_with_retry(client,"u")
            assert resp.status_code == 200, status
            assert len(client.calls) == 2, status

    @pytest.mark.asyncio
    async def test_retries_on_timeout(self):
        timeout = httpx.ReadTimeout(
            "slow", request=httpx.Request("GET", "https://x")
        )
        client = _FakeClient([timeout, _response(200)])
        resp = await _http.get_with_retry(client,"u")
        assert resp.status_code == 200
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_retries_on_connect_error(self):
        connect = httpx.ConnectError(
            "dns", request=httpx.Request("GET", "https://x")
        )
        client = _FakeClient([connect, _response(200)])
        resp = await _http.get_with_retry(client,"u")
        assert resp.status_code == 200
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_returns_final_failure_response_after_exhausting_retries(self):
        # Two 503s back-to-back: the second one is returned, NOT raised,
        # so the caller's existing raise_for_status() / status branch
        # surfaces it the same way it always has.
        client = _FakeClient([_response(503), _response(503)])
        resp = await _http.get_with_retry(client,"u")
        assert resp.status_code == 503
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_raises_final_exception_after_exhausting_retries(self):
        timeout = httpx.ReadTimeout(
            "slow", request=httpx.Request("GET", "https://x")
        )
        client = _FakeClient([timeout, timeout])
        with pytest.raises(httpx.ReadTimeout):
            await _http.get_with_retry(client,"u")
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_retry_after_header_extends_backoff(self):
        # A larger Retry-After should win over our default backoff.
        client = _FakeClient([
            _response(429, headers={"Retry-After": "12"}),
            _response(200),
        ])
        await _http.get_with_retry(client,"u")
        assert self.slept == [12.0]

    @pytest.mark.asyncio
    async def test_retry_after_smaller_than_backoff_uses_backoff(self):
        # backoff_seconds is the floor — we never go below the
        # provider's own throttle gap even if the server says it's OK.
        client = _FakeClient([
            _response(429, headers={"Retry-After": "0.5"}),
            _response(200),
        ])
        await _http.get_with_retry(
            client, "u", backoff_seconds=3.0
        )
        assert self.slept == [3.0]

    @pytest.mark.asyncio
    async def test_retry_after_capped_to_avoid_indefinite_pin(self):
        # A misconfigured server returning a huge Retry-After must not
        # pin our throttle for hours; the cap is backoff * 30.
        client = _FakeClient([
            _response(503, headers={"Retry-After": "999999"}),
            _response(200),
        ])
        await _http.get_with_retry(
            client, "u", backoff_seconds=1.0
        )
        assert self.slept == [30.0]

    @pytest.mark.asyncio
    async def test_non_numeric_retry_after_falls_back_to_backoff(self):
        # HTTP-date Retry-After is the other RFC form; we just ignore
        # it and use our own backoff. This is documented behaviour.
        client = _FakeClient([
            _response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
            _response(200),
        ])
        await _http.get_with_retry(
            client, "u", backoff_seconds=2.0
        )
        assert self.slept == [2.0]

    @pytest.mark.asyncio
    async def test_kwargs_forwarded_to_request(self):
        client = _FakeClient([_response(200)])
        await _http.get_with_retry(
            client, "u",
            params={"q": "hi"},
            headers={"X-Test": "1"},
        )
        assert client.calls[0][1]["params"] == {"q": "hi"}
        assert client.calls[0][1]["headers"] == {"X-Test": "1"}
