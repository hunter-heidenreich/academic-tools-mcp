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


# ---------------------------------------------------------------------------
# is_definitive_failure / cached_download — the shared protocol
# ---------------------------------------------------------------------------
#
# Verified once here rather than per provider, mirroring how test_throttle.py
# covers the gating primitive.


class TestIsDefinitiveFailure:
    """An allowlist, not a denylist — and the difference is a live bug class.

    ``_http.error_dict`` sets ``retryable`` on ``LocalBackpressureError``
    alone. Every other branch (429, 5xx, other 4xx, timeout, transport) says
    "Transient — retry." in prose and carries no such key. A denylist
    ("anything not marked retryable") therefore classifies all of them as
    permanent and negative-caches them for the full TTL.
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
    def test_transport_errors_carry_no_retryable_key_and_must_not_count(self, exc):
        from academic_tools_mcp import _http

        result = _http.error_dict("Test", exc)
        assert "retryable" not in result, "the premise of this test changed"
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
