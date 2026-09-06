"""Tests for the shared HTTP error normalization helper."""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from academic_tools_mcp import _http, _stats


def _build_status_error(
    status: int, body: str = "", headers: dict | None = None
) -> httpx.HTTPStatusError:
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
        assert result["retryable"] is True

    def test_429_ignores_unparseable_retry_after(self):
        # A value in neither RFC 9110 form is dropped rather than returned as
        # a string — consistent with get_with_retry's behaviour. (Both *valid*
        # forms, delay-seconds and HTTP-date, do parse; see
        # TestRetryAfterHttpDate in test_politeness.py.)
        exc = _build_status_error(429, headers={"retry-after": "soon-ish"})
        result = _http.error_dict("Crossref", exc)
        assert "retry_after_seconds" not in result

    def test_5xx_marks_transient(self):
        for status in (500, 502, 503, 504):
            exc = _build_status_error(status)
            result = _http.error_dict("arXiv", exc)
            assert "server error" in result["error"].lower()
            assert "transient" in result["error"].lower()
            assert str(status) in result["error"]
            assert result["retryable"] is True

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
        assert result["retryable"] is True

    def test_connect_error(self):
        exc = httpx.ConnectError("dns failed", request=httpx.Request("GET", "https://x"))
        result = _http.error_dict("bioRxiv", exc)
        assert "network error" in result["error"].lower()
        assert "bioRxiv" in result["error"]
        assert result["retryable"] is True

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
        exc = _http.LocalBackpressureError("arXiv", pending=5, max_pending=5, min_gap_seconds=3.0)
        result = _http.error_dict("arXiv", exc)

        # Structured fields the agent can read directly.
        assert result["max_concurrency"] == 5
        assert result["retry_after_seconds"] == 3.0

        # Human-readable hint embedded in the message for agents that
        # only parse the error string.
        msg = result["error"]
        assert "≥3.00s" in msg or "3.00s" in msg
        assert "≤5" in msg or "5 parallel" in msg

    def test_backpressure_name_comes_from_the_throttle_label(self):
        """A Throttle's ``label`` is the agent-facing provider name.

        ``error_dict``'s own argument is the call site's literal; the two agree
        for every provider today, so nothing else would catch a regression
        here — and the rules file promises callers that ``label`` is what an
        agent sees.
        """
        exc = _http.LocalBackpressureError("ACL Anthology", pending=5, max_pending=5)
        result = _http.error_dict("some-other-name", exc)
        assert "ACL Anthology" in result["error"]
        assert "some-other-name" not in result["error"]

    def test_backpressure_falls_back_to_the_argument(self):
        # A hand-built error with no name still reads sensibly.
        exc = _http.LocalBackpressureError("", pending=5, max_pending=5)
        assert "Crossref" in _http.error_dict("Crossref", exc)["error"]

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
            "no advertised gap → no retry_after, so agents don't pin to a fabricated interval"
        )


class TestRetryableFlag:
    """``retryable: True`` is the machine-readable half of "Transient — retry."

    ``oa_download._resolve_and_download`` and ``tools/graph._source_error``
    both branch on the key, not the prose. Without it a timeout is
    indistinguishable from a permanent failure: oa_download tells the agent to
    go fetch the PDF by hand, which is exactly wrong for a blip.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            _build_status_error(429),
            _build_status_error(500),
            _build_status_error(503),
            httpx.ReadTimeout("slow", request=httpx.Request("GET", "https://x")),
            httpx.ConnectError("dns", request=httpx.Request("GET", "https://x")),
            _http.LocalBackpressureError("arXiv", pending=5, max_pending=5),
        ],
    )
    def test_every_transient_branch_is_flagged(self, exc):
        assert _http.error_dict("Test", exc)["retryable"] is True

    @pytest.mark.parametrize("status", [408, 425])
    def test_retryable_4xx_is_flagged(self, status):
        """408 and 425 are in the allowlist, so `get_with_retry` retries them;
        `error_dict` must agree or the agent is told to give up on a failure
        the client itself considers worth retrying."""
        result = _http.error_dict("Test", _build_status_error(status))
        assert result["retryable"] is True
        # A 4xx is not a server error, however transient it is.
        assert "server error" not in result["error"]

    @pytest.mark.parametrize("status", [501, 505, 507])
    def test_non_allowlisted_5xx_is_not_flagged(self, status):
        """`_RETRYABLE_STATUSES` is the single definition of transient. A 501
        Not Implemented is a permanent answer; flagging it retryable sends the
        agent into a retry loop the retry helper itself declines to run."""
        result = _http.error_dict("Test", _build_status_error(status))
        assert "retryable" not in result
        assert status not in _http._RETRYABLE_STATUSES

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
    def test_other_4xx_is_left_unclassified(self, status):
        """Not ``retryable: False`` — that value is an explicit "definitive,
        safe to negative-cache" signal (``_pdf_download.is_definitive_failure``
        allowlists on it), and a paywalled 403 is not something we know that
        about."""
        assert "retryable" not in _http.error_dict("Test", _build_status_error(status))


class TestRetryAfterOnAnyTransientStatus:
    def test_503_retry_after_reaches_the_agent(self):
        # get_with_retry honours Retry-After on every retryable status, so
        # error_dict must too: a 503 maintenance window advertises it as often
        # as a 429 does, and reading it only on 429 discarded the advice.
        exc = _build_status_error(503, headers={"retry-after": "300"})
        assert _http.error_dict("Crossref", exc)["retry_after_seconds"] == 300.0

    def test_5xx_retry_after_honours_the_same_ceiling_as_429(self):
        exc = _build_status_error(500, headers={"retry-after": "86400"})
        result = _http.error_dict("Crossref", exc)
        assert result["retry_after_seconds"] == _http._MAX_RETRY_AFTER_SECONDS

    def test_5xx_without_the_header_omits_the_key(self):
        assert "retry_after_seconds" not in _http.error_dict("Crossref", _build_status_error(503))


class TestParseErrorDict:
    """The single home for "a 200 body that won't parse"; five providers
    delegate to it rather than spelling the shape themselves."""

    def test_default_detail(self):
        result = _http.parse_error_dict("OpenAlex")
        assert result == {
            "error": "OpenAlex returned a response that could not be parsed.",
            "retryable": True,
        }

    def test_custom_detail_for_a_non_json_provider(self):
        # arXiv speaks XML; the detail is the only provider-specific part.
        result = _http.parse_error_dict("arXiv", detail="could not be parsed as XML")
        assert "arXiv" in result["error"]
        assert "XML" in result["error"]

    def test_always_retryable(self):
        """A truncated or garbled body says nothing about whether the
        identifier exists, so it must never be negative-cached."""
        assert _http.parse_error_dict("Crossref")["retryable"] is True

    def test_a_fresh_dict_each_call(self):
        # A single-flight follower shares the returned object with the leader;
        # a shared dict would let one mutate the other's result.
        first = _http.parse_error_dict("Wikipedia")
        second = _http.parse_error_dict("Wikipedia")
        assert first == second
        assert first is not second
        first["error"] = "mutated"
        assert second["error"] != "mutated"


class TestJsonParseErrors:
    def test_includes_json_decode_error(self):
        # Single-homed so a new JSON provider can't forget one of the types.
        assert json.JSONDecodeError in _http.JSON_PARSE_ERRORS


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
        resp = await _http.get_with_retry(client, "u")
        assert resp.status_code == 200
        assert len(client.calls) == 1
        assert self.slept == []

    @pytest.mark.asyncio
    async def test_does_not_retry_on_404(self):
        # 404 is the caller's responsibility (real "not found"); we
        # must not waste a retry on it.
        client = _FakeClient([_response(404)])
        resp = await _http.get_with_retry(client, "u")
        assert resp.status_code == 404
        assert len(client.calls) == 1

    @pytest.mark.asyncio
    async def test_does_not_retry_on_400(self):
        client = _FakeClient([_response(400)])
        resp = await _http.get_with_retry(client, "u")
        assert resp.status_code == 400
        assert len(client.calls) == 1

    @pytest.mark.asyncio
    async def test_retries_on_429_and_returns_success(self):
        client = _FakeClient([_response(429), _response(200)])
        resp = await _http.get_with_retry(client, "u")
        assert resp.status_code == 200
        assert len(client.calls) == 2
        assert self.slept == [1.0]  # default backoff_seconds

    @pytest.mark.asyncio
    async def test_retries_on_503_and_returns_success(self):
        client = _FakeClient([_response(503), _response(200)])
        resp = await _http.get_with_retry(client, "u")
        assert resp.status_code == 200
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_retries_on_each_5xx(self):
        # Spot-check that the standard 5xx range is all retryable.
        for status in (500, 502, 503, 504):
            client = _FakeClient([_response(status), _response(200)])
            resp = await _http.get_with_retry(client, "u")
            assert resp.status_code == 200, status
            assert len(client.calls) == 2, status

    @pytest.mark.asyncio
    async def test_retries_on_timeout(self):
        timeout = httpx.ReadTimeout("slow", request=httpx.Request("GET", "https://x"))
        client = _FakeClient([timeout, _response(200)])
        resp = await _http.get_with_retry(client, "u")
        assert resp.status_code == 200
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_retries_on_connect_error(self):
        connect = httpx.ConnectError("dns", request=httpx.Request("GET", "https://x"))
        client = _FakeClient([connect, _response(200)])
        resp = await _http.get_with_retry(client, "u")
        assert resp.status_code == 200
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_returns_final_failure_response_after_exhausting_retries(self):
        # Two 503s back-to-back: the second one is returned, NOT raised,
        # so the caller's existing raise_for_status() / status branch
        # surfaces it the same way it always has.
        client = _FakeClient([_response(503), _response(503)])
        resp = await _http.get_with_retry(client, "u")
        assert resp.status_code == 503
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_raises_final_exception_after_exhausting_retries(self):
        timeout = httpx.ReadTimeout("slow", request=httpx.Request("GET", "https://x"))
        client = _FakeClient([timeout, timeout])
        with pytest.raises(httpx.ReadTimeout):
            await _http.get_with_retry(client, "u")
        assert len(client.calls) == 2

    @pytest.mark.asyncio
    async def test_retry_after_header_extends_backoff(self):
        # A larger Retry-After should win over our default backoff.
        client = _FakeClient(
            [
                _response(429, headers={"Retry-After": "12"}),
                _response(200),
            ]
        )
        await _http.get_with_retry(client, "u")
        assert self.slept == [12.0]

    @pytest.mark.asyncio
    async def test_retry_after_smaller_than_backoff_uses_backoff(self):
        # backoff_seconds is the floor — we never go below the
        # provider's own throttle gap even if the server says it's OK.
        client = _FakeClient(
            [
                _response(429, headers={"Retry-After": "0.5"}),
                _response(200),
            ]
        )
        await _http.get_with_retry(client, "u", backoff_seconds=3.0)
        assert self.slept == [3.0]

    @pytest.mark.asyncio
    async def test_retry_after_long_cooldown_respected(self):
        # A genuine multi-minute Retry-After is honoured (it sits below the
        # 10-minute absolute ceiling). This used to clamp to 30s.
        client = _FakeClient(
            [
                _response(429, headers={"Retry-After": "300"}),
                _response(200),
            ]
        )
        await _http.get_with_retry(client, "u")
        assert self.slept == [300.0]

    @pytest.mark.asyncio
    async def test_retry_after_capped_to_avoid_indefinite_pin(self):
        # A misconfigured server returning a huge Retry-After must not
        # pin our throttle for hours; the absolute ceiling is 600s (10 min).
        client = _FakeClient(
            [
                _response(503, headers={"Retry-After": "999999"}),
                _response(200),
            ]
        )
        await _http.get_with_retry(client, "u", backoff_seconds=1.0)
        assert self.slept == [600.0]

    @pytest.mark.asyncio
    async def test_unparseable_retry_after_falls_back_to_backoff(self):
        # A value in neither RFC 9110 form is ignored and our own backoff
        # applies. A *past* HTTP-date lands here too: it parses, but yields a
        # non-positive wait, which is not an instruction to sleep.
        client = _FakeClient(
            [
                _response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
                _response(200),
            ]
        )
        await _http.get_with_retry(client, "u", backoff_seconds=2.0)
        assert self.slept == [2.0]

    @pytest.mark.asyncio
    async def test_future_http_date_retry_after_extends_the_sleep(self):
        # The HTTP-date form is the other RFC 9110 spelling and reaches the
        # sleep, not just _retry_after_seconds: a Cloudflare-fronted 429 that
        # asks for two minutes must not be served by a 1.0s backoff.
        when = datetime.now(UTC) + timedelta(seconds=120)
        client = _FakeClient(
            [
                _response(429, headers={"Retry-After": format_datetime(when, usegmt=True)}),
                _response(200),
            ]
        )
        await _http.get_with_retry(client, "u", backoff_seconds=1.0)
        assert len(self.slept) == 1
        assert 110 < self.slept[0] <= 121

    @pytest.mark.asyncio
    async def test_multiple_attempts_back_off_exponentially(self):
        # A provider that opts into more attempts (e.g. arXiv) gets a widening
        # gap between retries: backoff, then 2×backoff. Two retryable responses
        # without Retry-After, then success.
        client = _FakeClient([_response(429), _response(503), _response(200)])
        resp = await _http.get_with_retry(client, "u", max_attempts=3, backoff_seconds=3.0)
        assert resp.status_code == 200
        assert len(client.calls) == 3
        assert self.slept == [3.0, 6.0]

    @pytest.mark.asyncio
    async def test_exponential_backoff_respects_ceiling(self):
        # The per-attempt exponential growth is still clamped to the 10-minute
        # ceiling so a high max_attempts can't produce an absurd sleep.
        client = _FakeClient([_response(503), _response(503), _response(503), _response(200)])
        await _http.get_with_retry(client, "u", max_attempts=4, backoff_seconds=400.0)
        # 400, then min(800, 600), then min(1600, 600).
        assert self.slept == [400.0, 600.0, 600.0]

    @pytest.mark.asyncio
    async def test_exponential_backoff_applies_to_network_errors(self):
        # The widening gap covers the transport-exception path too, not just
        # retryable status codes.
        timeout = httpx.ReadTimeout("slow", request=httpx.Request("GET", "https://x"))
        client = _FakeClient([timeout, timeout, _response(200)])
        resp = await _http.get_with_retry(client, "u", max_attempts=3, backoff_seconds=2.0)
        assert resp.status_code == 200
        assert self.slept == [2.0, 4.0]

    @pytest.mark.asyncio
    async def test_a_non_positive_max_attempts_still_issues_one_request(self):
        """A misconfigured Throttle must degrade to "no retries", not crash.

        Falling out of the loop leaves ``response`` unbound; the resulting
        UnboundLocalError is a NameError, so it is not in HTTPX_ERRORS and
        escapes the provider instead of becoming an {error} dict.
        """
        for attempts in (0, -1):
            client = _FakeClient([_response(200)])
            resp = await _http.get_with_retry(client, "u", max_attempts=attempts)
            assert resp.status_code == 200
            assert len(client.calls) == 1
        assert self.slept == []

    @pytest.mark.asyncio
    async def test_kwargs_forwarded_to_request(self):
        client = _FakeClient([_response(200)])
        await _http.get_with_retry(
            client,
            "u",
            params={"q": "hi"},
            headers={"X-Test": "1"},
        )
        assert client.calls[0][1]["params"] == {"q": "hi"}
        assert client.calls[0][1]["headers"] == {"X-Test": "1"}


class TestRetryStats:
    """``http_calls`` / ``http_retries`` are what an operator reads to audit
    outbound volume; ``http_calls`` earned a regression test after being wrong
    and ``http_retries`` never had one.
    """

    @pytest.fixture(autouse=True)
    def _patch_sleep(self, monkeypatch):
        async def fake_sleep(seconds):
            return None

        monkeypatch.setattr(_http.asyncio, "sleep", fake_sleep)
        _stats.reset()

    @pytest.mark.asyncio
    async def test_counts_a_retry_on_a_retryable_status(self):
        client = _FakeClient([_response(503), _response(200)])
        await _http.get_with_retry(client, "u", provider="probe")
        counters = _stats.snapshot()["providers"]["probe"]
        assert counters["http_calls"] == 2
        assert counters["http_retries"] == 1

    @pytest.mark.asyncio
    async def test_counts_a_retry_on_a_transport_exception(self):
        # The exception path has its own incr site; only the status path was
        # ever exercised.
        timeout = httpx.ReadTimeout("slow", request=httpx.Request("GET", "https://x"))
        client = _FakeClient([timeout, _response(200)])
        await _http.get_with_retry(client, "u", provider="probe")
        counters = _stats.snapshot()["providers"]["probe"]
        assert counters["http_calls"] == 2
        assert counters["http_retries"] == 1

    @pytest.mark.asyncio
    async def test_a_first_attempt_success_records_no_retry(self):
        client = _FakeClient([_response(200)])
        await _http.get_with_retry(client, "u", provider="probe")
        counters = _stats.snapshot()["providers"]["probe"]
        assert counters["http_calls"] == 1
        assert counters.get("http_retries", 0) == 0

    @pytest.mark.asyncio
    async def test_the_final_attempt_is_a_call_not_a_retry(self):
        # Three attempts = 3 calls and 2 retries: the last failure is not
        # followed by a retry, so counting it as one would overstate the ratio.
        client = _FakeClient([_response(503), _response(503), _response(503)])
        await _http.get_with_retry(client, "u", provider="probe", max_attempts=3)
        counters = _stats.snapshot()["providers"]["probe"]
        assert counters["http_calls"] == 3
        assert counters["http_retries"] == 2

    @pytest.mark.asyncio
    async def test_no_provider_records_nothing(self):
        client = _FakeClient([_response(503), _response(200)])
        await _http.get_with_retry(client, "u")
        # Only live in-flight rows, no counter rows: an unnamed caller is not
        # attributed to a provider rather than to a bogus one.
        providers = _stats.snapshot()["providers"]
        assert all(set(row) == {"in_flight"} for row in providers.values()), providers


class _UnreadStream(httpx.AsyncByteStream):
    """A genuinely streamed body: `.text` raises until aread() is called."""

    async def __aiter__(self):
        yield b"<html>Forbidden</html>"


def test_error_dict_survives_an_unread_streaming_body():
    """`error_dict` must not raise when the 4xx snippet is unavailable.

    httpx.ResponseNotRead subclasses RuntimeError rather than HTTPError, so it
    is not caught by the `except HTTPX_ERRORS` blocks that wrap every call to
    this helper: an uncaught one escapes the provider and replaces the status
    code with a stream-access message.
    """
    response = httpx.Response(
        403,
        headers={"content-type": "text/html"},
        stream=_UnreadStream(),
        request=httpx.Request("GET", "https://publisher.example/paper.pdf"),
    )
    exc = httpx.HTTPStatusError("403", request=response.request, response=response)

    result = _http.error_dict("OA download", exc)

    assert "403" in result["error"]
    assert "not read" in result["error"]
