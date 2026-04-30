"""Tests for the shared streaming PDF download helper.

Covers the chunk-streamed write path, the atomic rename via tmp file,
the MAX_PDF_BYTES cap (and its env-var resolver), and error handling
for 404 / transport / partial-write paths.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from academic_tools_mcp import _pdf_download


@contextlib.asynccontextmanager
async def _passthrough_slot():
    """A slot factory that does nothing — for tests that don't need
    to exercise rate-limit gating."""
    yield


def _mock_stream_response(
    status_code: int = 200, chunks: list[bytes] | None = None
):
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
