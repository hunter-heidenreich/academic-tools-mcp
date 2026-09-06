"""Tests for the shared streaming PDF download helper.

Covers the chunk-streamed write path, the atomic rename via tmp file,
the MAX_PDF_BYTES cap (and its env-var resolver), and error handling
for 404 / transport / partial-write paths.
"""

from __future__ import annotations

import contextlib
import errno
import tempfile
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

import httpx
import pytest

from academic_tools_mcp import _pdf_download

# stream_to_file takes an explicit timeout — no default, so every caller states
# one. Short here: nothing in these tests reaches a real socket.
_TIMEOUT = 5.0


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

    @pytest.mark.parametrize("negative", ["-1", "-200000000"])
    def test_a_negative_cap_does_not_disable_the_guard(self, monkeypatch, negative):
        """``-1`` reads as an "unlimited" idiom in other tools. Here it is a
        typo, and honouring it would silently remove the disk guard — the one
        thing this cap exists to provide. Falls back to the default instead."""
        monkeypatch.setenv("MAX_PDF_BYTES", negative)
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

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
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

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
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

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
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

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
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
            result = await _pdf_download.stream_to_file(
                client,
                "http://example.com/x.pdf",
                dest,
                slot_factory=_passthrough_slot,
                provider_label="arXiv",
                timeout=_TIMEOUT,
            )

        assert "error" in result
        assert "arXiv" in result["error"]
        assert result["retryable"] is True
        assert not _pdf_download.is_definitive_failure(result), (
            "a full disk must not be negative-cached against the paper"
        )
        assert not dest.exists()
        assert not list(tmp_path.glob("*.tmp")), "the partial temp file was left behind"

    @pytest.mark.asyncio
    async def test_a_rejected_response_never_touches_the_disk(self, tmp_path: Path):
        """The temp file is created only once the response is worth writing,
        so a 404 leaves the destination directory uncreated entirely."""
        dest = tmp_path / "nested" / "missing.pdf"
        client = MagicMock()
        client.stream = MagicMock(return_value=_mock_stream_response(status_code=404)())

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
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

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
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

        result = await _pdf_download.stream_to_file(
            client,
            "http://example.com/x.pdf",
            dest,
            slot_factory=_passthrough_slot,
            provider_label="Test",
            timeout=_TIMEOUT,
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
        result = await _pdf_download.stream_to_file(
            client,
            "https://publisher.example/paper.pdf",
            tmp_path / "out.pdf",
            slot_factory=_passthrough_slot,
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
        result = await _pdf_download.stream_to_file(
            client,
            "https://publisher.example/paper.pdf",
            tmp_path / "out.pdf",
            slot_factory=_passthrough_slot,
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

        result = await _pdf_download.stream_to_file(
            client,
            "https://example.org/empty.pdf",
            dest,
            slot_factory=_passthrough_slot,
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

        result = await _pdf_download.stream_to_file(
            client,
            "https://example.org/ok.pdf",
            dest,
            slot_factory=_passthrough_slot,
            provider_label="arXiv",
            timeout=_TIMEOUT,
        )

        assert "error" not in result
        assert result["size_bytes"] == len(b"%PDF-1.7\nbody")
        assert dest.read_bytes().startswith(b"%PDF-")


class TestIsUsablePdf:
    def test_missing_file(self, tmp_path):
        assert _pdf_download.is_usable_pdf(tmp_path / "nope.pdf") is False

    def test_zero_byte_file(self, tmp_path):
        p = tmp_path / "empty.pdf"
        p.write_bytes(b"")
        assert _pdf_download.is_usable_pdf(p) is False

    def test_html_landing_page(self, tmp_path):
        p = tmp_path / "landing.pdf"
        p.write_bytes(b"<!DOCTYPE html><html>Paywall</html>")
        assert _pdf_download.is_usable_pdf(p) is False

    def test_real_pdf_header(self, tmp_path):
        p = tmp_path / "real.pdf"
        p.write_bytes(b"%PDF-1.4\n...")
        assert _pdf_download.is_usable_pdf(p) is True

    def test_directory_is_not_usable(self, tmp_path):
        d = tmp_path / "adir.pdf"
        d.mkdir()
        assert _pdf_download.is_usable_pdf(d) is False


class TestCachedHit:
    def test_returns_none_for_zero_byte(self, tmp_path):
        p = tmp_path / "empty.pdf"
        p.write_bytes(b"")
        assert _pdf_download.cached_hit(p) is None

    def test_returns_payload_for_real_pdf(self, tmp_path):
        p = tmp_path / "real.pdf"
        p.write_bytes(b"%PDF-1.4\nxyz")
        hit = _pdf_download.cached_hit(p)
        assert hit == {"path": str(p), "size_bytes": 12, "cached": True}

    def test_an_unlink_between_the_check_and_the_stat_is_a_miss(self, tmp_path, monkeypatch):
        """The race cached_hit exists to absorb: a concurrent unlink after
        ``is_usable_pdf`` says yes but before the size read. Callers must get
        a miss they can re-download, not an OSError out of an MCP tool."""
        p = tmp_path / "real.pdf"
        p.write_bytes(b"%PDF-1.4\nxyz")

        real_stat = Path.stat
        seen = {"n": 0}

        def vanishing_stat(self, *args, **kwargs):
            seen["n"] += 1
            # Let is_usable_pdf's stat through; fail the size read after it.
            if seen["n"] > 1 and self == p:
                raise OSError("file vanished")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", vanishing_stat)
        assert _pdf_download.cached_hit(p) is None


# ---------------------------------------------------------------------------
# is_definitive_failure / cached_download — the shared protocol
# ---------------------------------------------------------------------------
#
# Verified once here rather than per provider, mirroring how test_throttle.py
# covers the gating primitive.


class TestIsDefinitiveFailure:
    """An allowlist, not a denylist — and the difference is a live bug class.

    ``_http.error_dict`` marks every *transient* branch ``retryable: True``,
    but its "other 4xx" branch carries no ``retryable`` key at all: a 403 from
    a paywall is not something we know is permanent and paper-intrinsic. Under
    a denylist ("anything not marked retryable") that unknown is classified as
    definitive and negative-cached for the full TTL.
    """

    def test_an_explicit_non_retryable_error_counts(self):
        assert _pdf_download.is_definitive_failure({"error": "gone", "retryable": False})

    def test_an_explicit_retryable_error_does_not(self):
        assert not _pdf_download.is_definitive_failure({"error": "blip", "retryable": True})

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ReadTimeout("boom"),
            httpx.ConnectError("boom"),
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(503, request=httpx.Request("GET", "http://x")),
            ),
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(429, request=httpx.Request("GET", "http://x")),
            ),
        ],
    )
    def test_transient_errors_are_flagged_retryable_and_must_not_count(self, exc):
        from academic_tools_mcp import _http

        result = _http.error_dict("Test", exc)
        assert result["retryable"] is True, (
            "every transient branch of error_dict must carry the flag; "
            "oa_download and tools/graph branch on it"
        )
        assert not _pdf_download.is_definitive_failure(result)

    def test_an_unclassified_4xx_must_not_count(self):
        """The case that makes the allowlist load-bearing.

        A 403 gets no ``retryable`` key either way — we don't know whether the
        paywall is permanent. A denylist would negative-cache it for the TTL.
        """
        from academic_tools_mcp import _http

        request = httpx.Request("GET", "http://x")
        result = _http.error_dict(
            "Test",
            httpx.HTTPStatusError(
                "x", request=request, response=httpx.Response(403, request=request)
            ),
        )
        assert "retryable" not in result
        assert not _pdf_download.is_definitive_failure(result)

    def test_a_size_cap_abort_does_not_count(self):
        # Non-retryable, but a config choice a cap bump fixes — not a fact
        # about the paper. Caching it would strand the caller behind a stale
        # miss until the TTL expired.
        assert not _pdf_download.is_definitive_failure(
            {"error": "too big", "retryable": False, "max_bytes": 100}
        )

    def test_a_success_does_not_count(self):
        assert not _pdf_download.is_definitive_failure({"path": "/x", "cached": False})

    def test_a_404_from_stream_to_file_counts(self):
        # The shape stream_to_file actually emits, so the classifier and the
        # producer cannot drift apart on this one.
        assert _pdf_download.is_definitive_failure(
            {"error": "arXiv: PDF not found at http://x", "retryable": False}
        )


def _pdf(dest: Path, body: bytes = b"%PDF-1.4 body") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return dest


class TestCachedDownload:
    """The file-artifact sibling of ``cache.cached_lookup``."""

    @staticmethod
    def _call(dest, fetch, **overrides):
        from academic_tools_mcp import _singleflight

        kwargs = {
            "single_flight": _singleflight.SingleFlight(),
            "namespace": "testns",
            "entity": "downloads",
            "canonical": "10.1234/x",
            "dest": dest,
            "fetch": fetch,
            "neg_ttl": 3600.0,
        }
        kwargs.update(overrides)
        return _pdf_download.cached_download(**kwargs)

    @pytest.mark.asyncio
    async def test_a_usable_cached_pdf_short_circuits(self, tmp_path):
        dest = _pdf(tmp_path / "p.pdf")
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            return {"path": str(dest), "cached": False}

        result = await self._call(dest, fetch)

        assert result["cached"] is True
        assert calls == 0

    @pytest.mark.asyncio
    async def test_a_zero_byte_file_is_a_miss(self, tmp_path):
        dest = _pdf(tmp_path / "p.pdf", b"")

        async def fetch():
            return {"path": str(dest), "size_bytes": 3, "cached": False}

        assert (await self._call(dest, fetch))["cached"] is False

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_fetch(self, tmp_path):
        import asyncio

        dest = tmp_path / "p.pdf"
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return {"path": str(dest), "cached": False}

        from academic_tools_mcp import _singleflight

        sf = _singleflight.SingleFlight()
        await asyncio.gather(*(self._call(dest, fetch, single_flight=sf) for _ in range(4)))

        assert calls == 1

    @pytest.mark.asyncio
    async def test_the_in_slot_recheck_serves_a_file_written_after_the_outer_check(
        self, tmp_path, monkeypatch
    ):
        """The protocol's core concurrency guarantee.

        A caller misses the outer check, then waits on the single-flight slot
        while a leader for the same key lands the PDF. Re-checking *inside*
        the slot is what makes it pick up those bytes instead of re-streaming
        a file that is already on disk. Simulated by failing only the outer
        ``cached_hit`` — the same window the leader writes into.
        """
        dest = tmp_path / "p.pdf"
        real_cached_hit = _pdf_download.cached_hit
        checks = {"n": 0}

        def leader_writes_between_the_checks(path):
            checks["n"] += 1
            if checks["n"] == 1:
                return None  # outer check: the leader has not written yet
            _pdf(dest)  # ...and lands the file before we re-check in the slot
            return real_cached_hit(path)

        monkeypatch.setattr(_pdf_download, "cached_hit", leader_writes_between_the_checks)

        async def fetch():
            raise AssertionError("the in-slot re-check must short-circuit before fetch")

        result = await self._call(dest, fetch)

        assert result["cached"] is True
        assert checks["n"] == 2, "both the outer check and the in-slot re-check must run"

    @pytest.mark.asyncio
    async def test_the_in_slot_recheck_serves_a_negative_entry_written_after_the_outer_check(
        self, tmp_path, monkeypatch
    ):
        """Same window, negative half: a leader that recorded a definitive
        failure while this caller waited must not be re-fetched."""
        from academic_tools_mcp import cache

        dest = tmp_path / "p.pdf"  # never created — cached_hit misses naturally
        checks = {"n": 0}

        def leader_records_between_the_checks(*args, **kwargs):
            checks["n"] += 1
            if checks["n"] == 1:
                return None
            return {"error": "gone", "retryable": False}

        monkeypatch.setattr(cache, "get_negative", leader_records_between_the_checks)

        async def fetch():
            raise AssertionError("the in-slot re-check must short-circuit before fetch")

        result = await self._call(dest, fetch)

        assert result == {"error": "gone", "retryable": False}
        assert checks["n"] == 2

    @pytest.mark.asyncio
    async def test_force_refresh_skips_the_in_slot_recheck(self, tmp_path, monkeypatch):
        """The deliberate divergence from ``cache.cached_lookup``.

        Re-checking under force_refresh would make a refresh a no-op whenever
        a usable PDF is already on disk — exactly the case force_refresh
        exists to fix (a corrupt or superseded cached file). So the forced
        path must reach ``fetch`` even with a good file and a negative entry
        both present.
        """
        from academic_tools_mcp import cache

        dest = _pdf(tmp_path / "p.pdf", b"%PDF-1.4 stale")
        cache.put_negative("testns", "downloads", "10.1234/x", {"error": "gone"})

        def must_not_be_consulted(path):
            raise AssertionError("force_refresh must not check the cached artifact")

        monkeypatch.setattr(_pdf_download, "cached_hit", must_not_be_consulted)

        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            dest.write_bytes(b"%PDF-1.4 fresh")
            return {"path": str(dest), "size_bytes": 14, "cached": False}

        result = await self._call(dest, fetch, force_refresh=True)

        assert calls == 1
        assert result["cached"] is False
        assert dest.read_bytes() == b"%PDF-1.4 fresh"

    @pytest.mark.asyncio
    async def test_a_definitive_failure_is_negative_cached(self, tmp_path):
        dest = tmp_path / "p.pdf"
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            return {"error": "gone", "retryable": False}

        assert "error" in await self._call(dest, fetch)
        assert "error" in await self._call(dest, fetch)
        assert calls == 1, "the second call should be served from the negative cache"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            {"error": "blip", "retryable": True},
            {"error": "timed out"},  # no retryable key at all — the live bug
            {"error": "too big", "retryable": False, "max_bytes": 10},
        ],
    )
    async def test_a_non_definitive_failure_is_not_cached(self, tmp_path, failure):
        dest = tmp_path / "p.pdf"
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            return dict(failure)

        await self._call(dest, fetch)
        await self._call(dest, fetch)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_a_usable_pdf_wins_over_a_negative_entry(self, tmp_path):
        """Ordering is load-bearing: a force_refresh that 404s writes a
        negative entry while a perfectly good PDF is still on disk (
        ``stream_to_file`` only replaces dest on success). The next plain call
        must serve the file, not the stale error."""
        from academic_tools_mcp import cache

        dest = _pdf(tmp_path / "p.pdf")
        cache.put_negative("testns", "downloads", "10.1234/x", {"error": "gone"})

        async def fetch():
            raise AssertionError("must not fetch")

        assert (await self._call(dest, fetch))["cached"] is True

    @pytest.mark.asyncio
    async def test_force_refresh_clears_the_negative_entry_and_refetches(self, tmp_path):
        from academic_tools_mcp import cache

        dest = tmp_path / "p.pdf"
        cache.put_negative("testns", "downloads", "10.1234/x", {"error": "gone"})

        async def fetch():
            return {"path": str(dest), "cached": False}

        assert (await self._call(dest, fetch, force_refresh=True))["cached"] is False
        assert cache.get_negative("testns", "downloads", "10.1234/x") is None

    @pytest.mark.asyncio
    async def test_extra_fields_decorate_both_success_branches(self, tmp_path):
        # ACL's provenance must be identical on the fresh and cached paths.
        # They used to be two hand-copied blocks that could drift.
        dest = tmp_path / "p.pdf"
        extra = {"anthology_id": "P16-1160"}

        async def fetch():
            _pdf(dest)
            return {"path": str(dest), "size_bytes": 13, "cached": False}

        fresh = await self._call(dest, fetch, extra_fields=extra)

        async def no_fetch():
            raise AssertionError("must not fetch")

        cached = await self._call(dest, no_fetch, extra_fields=extra)

        assert fresh["anthology_id"] == cached["anthology_id"] == "P16-1160"
        assert fresh["cached"] is False
        assert cached["cached"] is True

    @pytest.mark.asyncio
    async def test_extra_fields_do_not_decorate_errors(self, tmp_path):
        dest = tmp_path / "p.pdf"

        async def fetch():
            return {"error": "gone", "retryable": False}

        result = await self._call(dest, fetch, extra_fields={"anthology_id": "P16-1160"})

        assert "anthology_id" not in result

    @pytest.mark.asyncio
    async def test_callers_receive_independent_objects(self, tmp_path):
        # Single-flight followers share the leader's object, and
        # tools/pipeline writes `cascaded_invalidated` into what it gets back.
        import asyncio

        from academic_tools_mcp import _singleflight

        dest = tmp_path / "p.pdf"

        async def fetch():
            await asyncio.sleep(0.01)
            return {"path": str(dest), "cached": False}

        sf = _singleflight.SingleFlight()
        a, b = await asyncio.gather(
            self._call(dest, fetch, single_flight=sf),
            self._call(dest, fetch, single_flight=sf),
        )

        assert a is not b
        a["cascaded_invalidated"] = ["markdown"]
        assert "cascaded_invalidated" not in b
