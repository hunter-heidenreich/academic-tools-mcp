"""Tests for `download/streaming.py` — the wire transport every provider shares."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import tempfile
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import httpx
import pytest

from academic_tools_mcp.download import protocol, streaming
from academic_tools_mcp.net import stats
from tests.helpers.download_fakes import TIMEOUT as _TIMEOUT
from tests.helpers.download_fakes import mock_stream_response as _mock_stream_response
from tests.helpers.download_fakes import passthrough_slot as _passthrough_slot
from tests.helpers.download_fakes import streaming_client as _streaming_client


class TestResolveMaxPdfBytes:
    def test_default_returned_when_unset(self, monkeypatch):
        monkeypatch.delenv("MAX_PDF_BYTES", raising=False)
        assert streaming.resolve_max_pdf_bytes() == streaming._DEFAULT_MAX_PDF_BYTES

    @pytest.mark.parametrize("disabled", ["none", "off", "disabled", "0", "NONE"])
    def test_disabled_strings(self, monkeypatch, disabled):
        monkeypatch.setenv("MAX_PDF_BYTES", disabled)
        assert streaming.resolve_max_pdf_bytes() is None

    def test_explicit_value(self, monkeypatch):
        monkeypatch.setenv("MAX_PDF_BYTES", "1048576")
        assert streaming.resolve_max_pdf_bytes() == 1_048_576

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("MAX_PDF_BYTES", "not-a-number")
        assert streaming.resolve_max_pdf_bytes() == streaming._DEFAULT_MAX_PDF_BYTES

    @pytest.mark.parametrize("negative", ["-1", "-200000000"])
    def test_a_negative_cap_does_not_disable_the_guard(self, monkeypatch, negative):
        """``-1`` reads as an "unlimited" idiom in other tools. Here it is a
        typo, and honouring it would silently remove the disk guard — the one
        thing this cap exists to provide. Falls back to the default instead."""
        monkeypatch.setenv("MAX_PDF_BYTES", negative)
        assert streaming.resolve_max_pdf_bytes() == streaming._DEFAULT_MAX_PDF_BYTES

    @pytest.mark.parametrize("raw", ["inf", "nan"])
    def test_non_finite_does_not_disable_the_guard(self, monkeypatch, raw):
        monkeypatch.setenv("MAX_PDF_BYTES", raw)
        assert streaming.resolve_max_pdf_bytes() == streaming._DEFAULT_MAX_PDF_BYTES


class TestStreamToFile:
    @pytest.mark.asyncio
    async def test_writes_chunks_atomically(self, tmp_path: Path):
        dest = tmp_path / "out.pdf"
        chunks = [b"%PDF-1.4 ", b"hello ", b"world"]
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(chunks=chunks)())

        result = await streaming.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="probe",
            provider_label="Test",
            timeout=_TIMEOUT,
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

        result = await streaming.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="probe",
            provider_label="Test",
            not_found_message="No PDF found.",
            timeout=_TIMEOUT,
        )

        assert result == {"error": "No PDF found.", "retryable": False}
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

        result = await streaming.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="probe",
            provider_label="Test",
            timeout=_TIMEOUT,
        )

        assert "error" in result
        assert "MAX_PDF_BYTES" in result["error"]
        assert result["max_bytes"] == 10
        assert result["retryable"] is False
        assert not dest.exists()
        assert not list(tmp_path.glob("*.tmp"))

    @pytest.mark.asyncio
    async def test_exactly_at_the_cap_succeeds(self, tmp_path: Path, monkeypatch):
        """The other side of the boundary. The abort condition is
        ``written + len(chunk) > max_bytes``, so a PDF of exactly
        MAX_PDF_BYTES must land — off-by-one here rejects legitimate papers
        that happen to sit on the limit."""
        monkeypatch.setenv("MAX_PDF_BYTES", "10")
        dest = tmp_path / "exact.pdf"
        chunks = [b"%PDF-1.4 ", b"x"]  # 9 + 1 == 10
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(chunks=chunks)())

        result = await streaming.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="probe",
            provider_label="Test",
            timeout=_TIMEOUT,
        )

        assert "error" not in result
        assert result["size_bytes"] == 10
        assert dest.stat().st_size == 10

    @pytest.mark.asyncio
    async def test_one_byte_past_the_cap_aborts(self, tmp_path: Path, monkeypatch):
        """...and one byte past it must not."""
        monkeypatch.setenv("MAX_PDF_BYTES", "10")
        dest = tmp_path / "over.pdf"
        chunks = [b"%PDF-1.4 ", b"xx"]  # 9 + 2 == 11
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(chunks=chunks)())

        result = await streaming.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="probe",
            provider_label="Test",
            timeout=_TIMEOUT,
        )

        assert result["max_bytes"] == 10
        assert not dest.exists()
        assert not list(tmp_path.glob("*.tmp"))

    @pytest.mark.asyncio
    async def test_a_write_failure_returns_an_error_not_an_oserror(self, tmp_path: Path):
        """A full or read-only disk must reach the agent as ``{error,
        retryable: True}``.

        ``cache.put`` already refuses to let an ENOSPC escape as a raised
        OSError out of an MCP tool; the PDF write path is the other place
        this server touches the disk after paying for a response, and it
        owes the same contract. Retryable, so it also stays out of the
        negative cache — a full disk is not a fact about the paper.
        """
        dest = tmp_path / "nospace.pdf"
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response()())

        real_open = tempfile.NamedTemporaryFile

        class _FullDisk:
            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.name = wrapped.name

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._wrapped.close()
                return False

            def write(self, _data):
                raise OSError(errno.ENOSPC, "No space left on device")

        def _fake(*args, **kwargs):
            return _FullDisk(real_open(*args, **kwargs))

        with mock.patch.object(tempfile, "NamedTemporaryFile", _fake):
            result = await streaming.stream_to_file(
                client,
                "http://example.com/x.pdf",
                dest,
                slot_factory=_passthrough_slot,
                namespace="arxiv",
                provider_label="arXiv",
                timeout=_TIMEOUT,
            )

        assert "error" in result
        assert "arXiv" in result["error"]
        assert result["retryable"] is True
        assert not protocol.is_definitive_failure(result), (
            "a full disk must not be negative-cached against the paper"
        )
        assert not dest.exists()
        assert not list(tmp_path.glob("*.tmp")), "the partial temp file was left behind"

        # Counted under the cache namespace, not the "arXiv" label: a disk
        # failure has to land in the row already holding this provider's cache
        # counters, or an operator diagnosing a full disk sees two half-rows.
        counters = stats.snapshot()["providers"]["arxiv"]
        assert counters["cache_write_failures"] == 1, counters

    @pytest.mark.asyncio
    async def test_a_rejected_response_never_touches_the_disk(self, tmp_path: Path):
        """The temp file is created only once the response is worth writing,
        so a 404 leaves the destination directory uncreated entirely."""
        dest = tmp_path / "nested" / "missing.pdf"
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(status_code=404)())

        result = await streaming.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="probe",
            provider_label="Test",
            timeout=_TIMEOUT,
        )

        assert "error" in result
        assert not dest.parent.exists()

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

        result = await streaming.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="probe",
            provider_label="Test",
            timeout=_TIMEOUT,
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

        result = await streaming.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="probe",
            provider_label="Test",
            timeout=_TIMEOUT,
        )
        assert "error" not in result
        assert result["size_bytes"] == 1024 * 1024
        assert dest.stat().st_size == 1024 * 1024


# --- streaming error responses ---------------------------------------------


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
        result = await streaming.stream_to_file(
            client,
            "https://publisher.example/paper.pdf",
            tmp_path / "out.pdf",
            slot_factory=_passthrough_slot,
            namespace="oa_download",
            provider_label="OA download",
            require_pdf=True,
            timeout=_TIMEOUT,
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
        result = await streaming.stream_to_file(
            client,
            "https://publisher.example/paper.pdf",
            tmp_path / "out.pdf",
            slot_factory=_passthrough_slot,
            namespace="oa_download",
            provider_label="OA download",
            not_found_message="Open-access PDF not found",
            timeout=_TIMEOUT,
        )
    finally:
        await client.aclose()

    assert result == {"error": "Open-access PDF not found", "retryable": False}


@pytest.mark.asyncio
async def test_a_streaming_success_is_never_buffered(tmp_path):
    """The fix must read only error bodies; a 200 PDF stays streamed."""
    client = _streaming_client(200, b"%PDF-1.4 real content", content_type="application/pdf")
    try:
        result = await streaming.stream_to_file(
            client,
            "https://publisher.example/paper.pdf",
            tmp_path / "out.pdf",
            slot_factory=_passthrough_slot,
            namespace="oa_download",
            provider_label="OA download",
            require_pdf=True,
            timeout=_TIMEOUT,
        )
    finally:
        await client.aclose()

    assert result["size_bytes"] == len(b"%PDF-1.4 real content")
    assert (tmp_path / "out.pdf").read_bytes() == b"%PDF-1.4 real content"


class TestEmptyBodyRejected:
    """A 200 with no body must not be installed as a successful download.

    Regression: with no chunks the write loop never ran, the ``%PDF-`` sniff
    never fired, and ``os.replace`` installed a 0-byte file returned as
    ``{"size_bytes": 0, "cached": False}``. Every downstream ``dest.exists()``
    then treated it as cached forever and convert_paper handed it to the
    converter.
    """

    @pytest.mark.asyncio
    async def test_zero_byte_response_errors_and_writes_nothing(self, tmp_path: Path):
        dest = tmp_path / "empty.pdf"

        # NB: _mock_stream_response does `chunks or [default]`, so an empty
        # list would silently become the default body. Build it inline.
        async def aiter_bytes(chunk_size):
            return
            yield  # pragma: no cover - makes this an async generator

        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.aiter_bytes = aiter_bytes

        @contextlib.asynccontextmanager
        async def stream_cm():
            yield response

        client = MagicMock()
        client.stream = MagicMock(return_value=stream_cm())

        result = await streaming.stream_to_file(
            client,
            "https://example.org/empty.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="arxiv",
            provider_label="arXiv",
            timeout=_TIMEOUT,
        )

        assert "error" in result
        assert "empty body" in result["error"]
        assert result["retryable"] is True
        assert not dest.exists(), "a 0-byte file was installed at the destination"
        assert list(tmp_path.iterdir()) == [], "a temp file was left behind"

    @pytest.mark.asyncio
    async def test_nonempty_response_still_succeeds(self, tmp_path: Path):
        dest = tmp_path / "ok.pdf"
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(chunks=[b"%PDF-1.7\nbody"])())

        result = await streaming.stream_to_file(
            client,
            "https://example.org/ok.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="arxiv",
            provider_label="arXiv",
            timeout=_TIMEOUT,
        )

        assert "error" not in result
        assert result["size_bytes"] == len(b"%PDF-1.7\nbody")
        assert dest.read_bytes().startswith(b"%PDF-")


class TestNonSuccessStatus:
    """The failure path is bounded, and a 3xx is not mistaken for a body."""

    @staticmethod
    def _client(status: int, body: bytes, *, headers: dict[str, str] | None = None):
        """A streaming client that records how much of the body was consumed."""
        consumed: list[int] = []

        class CountingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for start in range(0, len(body), 8192):
                    chunk = body[start : start + 8192]
                    consumed.append(len(chunk))
                    yield chunk

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
                headers={"content-type": "text/html", **(headers or {})},
                stream=CountingStream(),
            )

        return httpx.AsyncClient(transport=httpx.MockTransport(handler)), consumed

    async def _run(self, client, tmp_path: Path, url: str = "https://pub.example/p.pdf"):
        try:
            return await streaming.stream_to_file(
                client,
                url,
                tmp_path / "out.pdf",
                slot_factory=_passthrough_slot,
                namespace="oa_download",
                provider_label="OA download",
                require_pdf=True,
                timeout=_TIMEOUT,
            )
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_a_large_error_body_is_not_buffered_whole(self, tmp_path: Path):
        """Regression: `aread()` buffered the entire body to use 200 bytes of it."""
        client, consumed = self._client(502, b"x" * 5_000_000)

        result = await self._run(client, tmp_path)

        assert "502" in result["error"]
        assert result["retryable"] is True, "502 is in _RETRYABLE_STATUSES"
        assert sum(consumed) < 100_000, f"read {sum(consumed)} bytes of a 5 MB error body"
        assert not (tmp_path / "out.pdf").exists()

    @pytest.mark.asyncio
    async def test_an_unclassified_4xx_stays_unflagged(self, tmp_path: Path):
        """`is_definitive_failure` is an allowlist, so a 403 must carry no verdict."""
        client, _ = self._client(403, b"<html>Forbidden</html>")

        result = await self._run(client, tmp_path)

        assert result["error"] == "OA download HTTP 403: <html>Forbidden</html>"
        assert "retryable" not in result
        assert not protocol.is_definitive_failure(result)

    @pytest.mark.asyncio
    async def test_retry_after_survives_the_streaming_path(self, tmp_path: Path):
        client, _ = self._client(503, b"busy", headers={"retry-after": "30"})

        result = await self._run(client, tmp_path)

        assert result["retryable"] is True
        assert result["retry_after_seconds"] == 30.0

    @pytest.mark.asyncio
    async def test_a_location_less_redirect_is_an_error_not_a_landing_page(self, tmp_path: Path):
        """httpx follows only a 3xx carrying a Location, so this one reaches us.

        A `>= 400` gate would sniff the body for `%PDF-`, call it a paywall and
        negative-cache that verdict against the paper.
        """
        client, _ = self._client(300, b"<html>Multiple Choices</html>")

        result = await self._run(client, tmp_path)

        assert "HTTP 300" in result["error"]
        assert "retryable" not in result
        assert not protocol.is_definitive_failure(result), "a redirect is not a paper verdict"
        assert not (tmp_path / "out.pdf").exists()


class TestCancellationCleansUp:
    """The `finally` exists for cancellation; the exception path alone never proved it."""

    @pytest.mark.asyncio
    async def test_cancelling_mid_stream_leaves_no_temp_file(self, tmp_path: Path):
        dest = tmp_path / "slow.pdf"
        started = asyncio.Event()

        async def aiter_bytes(_chunk_size=None):
            yield b"%PDF-1.4 first"
            started.set()
            await asyncio.sleep(3600)

        response = MagicMock()
        response.status_code = 200
        response.headers = {"content-type": "application/pdf"}
        response.aiter_bytes = aiter_bytes

        @contextlib.asynccontextmanager
        async def stream_cm():
            yield response

        client = MagicMock()
        client.stream = MagicMock(return_value=stream_cm())

        task = asyncio.create_task(
            streaming.stream_to_file(
                client,
                "https://example.org/slow.pdf",
                dest,
                slot_factory=_passthrough_slot,
                namespace="arxiv",
                provider_label="arXiv",
                timeout=_TIMEOUT,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not dest.exists()
        assert not list(tmp_path.glob("*.tmp")), "a temp file survived cancellation"


class TestPdfHeaderSniff:
    """The stream sniff accepts a PDF that does not start at byte 0."""

    _LEADING = b"\xef\xbb\xbf\n  %PDF-1.7\nbody"

    @pytest.mark.asyncio
    async def test_a_leading_bom_is_still_a_pdf(self, tmp_path: Path):
        dest = tmp_path / "bom.pdf"
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(chunks=[self._LEADING])())

        result = await streaming.stream_to_file(
            client,
            "https://pub.example/bom.pdf",
            dest,
            slot_factory=_passthrough_slot,
            namespace="oa_download",
            provider_label="OA download",
            require_pdf=True,
            timeout=_TIMEOUT,
        )

        assert "error" not in result, result.get("error")
        assert dest.read_bytes() == self._LEADING
