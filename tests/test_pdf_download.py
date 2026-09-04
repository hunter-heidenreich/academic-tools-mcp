"""Tests for the shared streaming PDF download helper.

Covers the chunk-streamed write path, the atomic rename via tmp file,
the MAX_PDF_BYTES cap (and its env-var resolver), and error handling
for 404 / transport / partial-write paths.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from academic_tools_mcp import _pdf_download


@contextlib.asynccontextmanager
async def _passthrough_slot():
    """A slot factory that does nothing — for tests that don't need
    to exercise rate-limit gating."""
    yield


def _mock_stream_response(status_code: int = 200, chunks: list[bytes] | None = None):
    """Build a mock async-context-manager that yields a streaming response."""
    chunks = chunks or [b"%PDF-1.4 fake content"]

    async def aiter_bytes(chunk_size):
        for c in chunks:
            yield c

    response = MagicMock()
    response.status_code = status_code
    response.raise_for_status = MagicMock()
    response.aiter_bytes = aiter_bytes

    @contextlib.asynccontextmanager
    async def stream_cm():
        yield response

    return stream_cm


class TestResolveMaxPdfBytes:
    def test_default_returned_when_unset(self, monkeypatch):
        monkeypatch.delenv("MAX_PDF_BYTES", raising=False)
        assert _pdf_download.resolve_max_pdf_bytes() == _pdf_download._DEFAULT_MAX_PDF_BYTES

    @pytest.mark.parametrize("disabled", ["none", "off", "disabled", "0", "NONE"])
    def test_disabled_strings(self, monkeypatch, disabled):
        monkeypatch.setenv("MAX_PDF_BYTES", disabled)
        assert _pdf_download.resolve_max_pdf_bytes() is None

    def test_explicit_value(self, monkeypatch):
        monkeypatch.setenv("MAX_PDF_BYTES", "1048576")
        assert _pdf_download.resolve_max_pdf_bytes() == 1_048_576

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("MAX_PDF_BYTES", "not-a-number")
        assert _pdf_download.resolve_max_pdf_bytes() == _pdf_download._DEFAULT_MAX_PDF_BYTES


class TestStreamToFile:
    @pytest.mark.asyncio
    async def test_writes_chunks_atomically(self, tmp_path: Path):
        dest = tmp_path / "out.pdf"
        chunks = [b"%PDF-1.4 ", b"hello ", b"world"]
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(chunks=chunks)())

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            provider_label="Test",
        )

        assert "error" not in result
        assert result["cached"] is False
        assert result["size_bytes"] == sum(len(c) for c in chunks)
        assert dest.exists()
        assert dest.read_bytes() == b"".join(chunks)
        # No leftover .tmp files in the parent directory
        assert not list(tmp_path.glob("*.tmp"))

    @pytest.mark.asyncio
    async def test_404_returns_error_no_file(self, tmp_path: Path):
        dest = tmp_path / "missing.pdf"
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(status_code=404)())

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            provider_label="Test",
            not_found_message="No PDF found.",
        )

        assert result == {"error": "No PDF found."}
        assert not dest.exists()
        assert not list(tmp_path.glob("*.tmp"))

    @pytest.mark.asyncio
    async def test_size_cap_aborts_partway(self, tmp_path: Path, monkeypatch):
        """A download that would exceed MAX_PDF_BYTES is aborted; the
        partial temp file is unlinked and dest is never created."""
        monkeypatch.setenv("MAX_PDF_BYTES", "10")
        dest = tmp_path / "huge.pdf"
        # 30 bytes total split into three 10-byte chunks. The third
        # would push us past 10 bytes, so it's rejected.
        chunks = [b"a" * 5, b"b" * 5, b"c" * 5]
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(chunks=chunks)())

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            provider_label="Test",
        )

        assert "error" in result
        assert "MAX_PDF_BYTES" in result["error"]
        assert result["max_bytes"] == 10
        assert result["retryable"] is False
        assert not dest.exists()
        assert not list(tmp_path.glob("*.tmp"))

    @pytest.mark.asyncio
    async def test_transport_error_cleans_up(self, tmp_path: Path):
        """A transport error mid-stream returns an error dict and the
        temp file is unlinked (no half-written canonical file left)."""
        dest = tmp_path / "broken.pdf"
        client = MagicMock()

        @contextlib.asynccontextmanager
        async def boom():
            raise httpx.ConnectError("connection refused")
            yield  # unreachable, but makes this a generator

        client.stream = MagicMock(return_value=boom())

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            provider_label="Test",
        )

        assert "error" in result
        assert "Test" in result["error"]
        assert not dest.exists()
        assert not list(tmp_path.glob("*.tmp"))

    @pytest.mark.asyncio
    async def test_disabled_cap_writes_arbitrary_size(self, tmp_path: Path, monkeypatch):
        """MAX_PDF_BYTES=none allows any size."""
        monkeypatch.setenv("MAX_PDF_BYTES", "none")
        dest = tmp_path / "big.pdf"
        chunks = [b"x" * 1024 * 1024]  # 1 MiB
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(chunks=chunks)())

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            provider_label="Test",
        )
        assert "error" not in result
        assert result["size_bytes"] == 1024 * 1024
        assert dest.stat().st_size == 1024 * 1024


# --- streaming error responses ---------------------------------------------


class _UnreadStream(httpx.AsyncByteStream):
    """A response body that is genuinely streamed.

    ``httpx.MockTransport`` with ``text=``/``content=`` hands back a response
    whose content is already buffered, so ``.text`` works and the bug under
    test cannot reproduce. A real stream is required.
    """

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def __aiter__(self):
        yield self._payload


def _streaming_client(status_code: int, body: bytes, content_type: str = "text/html"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"content-type": content_type},
            stream=_UnreadStream(body),
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_a_streaming_4xx_returns_the_status_not_a_response_not_read(tmp_path):
    """Regression: a publisher 403 on the open-access path used to escape as
    httpx.ResponseNotRead.

    ``error_dict`` reads ``exc.response.text`` for the 4xx snippet, which is
    unavailable on an unread streaming response. ResponseNotRead subclasses
    RuntimeError, not HTTPError, so ``except HTTPX_ERRORS`` did not catch it
    and it propagated out of the download entirely — the caller saw
    "Attempted to access streaming response content" instead of "HTTP 403".
    """
    client = _streaming_client(403, b"<html>Forbidden</html>")
    try:
        result = await _pdf_download.stream_to_file(
            client,
            "https://publisher.example/paper.pdf",
            tmp_path / "out.pdf",
            slot_factory=_passthrough_slot,
            provider_label="OA download",
            require_pdf=True,
        )
    finally:
        await client.aclose()

    assert "403" in result["error"]
    assert "Forbidden" in result["error"], "the body snippet still reaches the caller"
    assert not (tmp_path / "out.pdf").exists()


@pytest.mark.asyncio
async def test_a_streaming_404_still_short_circuits_before_the_body_read(tmp_path):
    client = _streaming_client(404, b"<html>nope</html>")
    try:
        result = await _pdf_download.stream_to_file(
            client,
            "https://publisher.example/paper.pdf",
            tmp_path / "out.pdf",
            slot_factory=_passthrough_slot,
            provider_label="OA download",
            not_found_message="Open-access PDF not found",
        )
    finally:
        await client.aclose()

    assert result == {"error": "Open-access PDF not found"}


@pytest.mark.asyncio
async def test_a_streaming_success_is_never_buffered(tmp_path):
    """The fix must read only error bodies; a 200 PDF stays streamed."""
    client = _streaming_client(200, b"%PDF-1.4 real content", content_type="application/pdf")
    try:
        result = await _pdf_download.stream_to_file(
            client,
            "https://publisher.example/paper.pdf",
            tmp_path / "out.pdf",
            slot_factory=_passthrough_slot,
            provider_label="OA download",
            require_pdf=True,
        )
    finally:
        await client.aclose()

    assert result["size_bytes"] == len(b"%PDF-1.4 real content")
    assert (tmp_path / "out.pdf").read_bytes() == b"%PDF-1.4 real content"
