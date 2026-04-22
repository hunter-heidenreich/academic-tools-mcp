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
        assert result["retry_after_seconds"] == "12"

    def test_429_omits_retry_after_when_absent(self):
        exc = _build_status_error(429)
        result = _http.error_dict("OpenAlex", exc)
        assert "rate limit" in result["error"].lower()
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


class TestExceptionTuple:
    def test_includes_status_timeout_and_request(self):
        # The contract: callers use HTTPX_ERRORS to narrow their except clause.
        assert httpx.HTTPStatusError in _http.HTTPX_ERRORS
        assert httpx.TimeoutException in _http.HTTPX_ERRORS
        assert httpx.RequestError in _http.HTTPX_ERRORS
