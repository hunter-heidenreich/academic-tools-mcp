"""Tests for the gated open-access PDF download path.

`download_pdf(identifier, allow_oa_url=True)` fetches a generic publisher
DOI from the open-access PDF URL OpenAlex reports for it — never an
arbitrary URL. Covers the URL-extraction helper, the OA download function,
the `%PDF-` / Content-Type content guard in `stream_to_file`, and the
server-level dispatch gating.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx
import pytest

from academic_tools_mcp import (
    _clients,
    _pdf_download,
    cache,
    manual,
    oa_download,
    server,
)
from academic_tools_mcp.providers import acl_anthology, arxiv, biorxiv, openalex

from ._download_fakes import TIMEOUT as _TIMEOUT
from ._download_fakes import install_stream as _install_stream
from ._download_fakes import mock_stream_response as _mock_stream_response
from ._download_fakes import passthrough_slot as _passthrough_slot
from ._download_fakes import streaming_client as _streaming_client

_DOI = "10.1162/tacl_a_00001"


# --- shared mock plumbing --------------------------------------------------


def _oa_dest() -> Path:
    return Path(manual.resolve_target(_DOI)["pdf_path"])


def _stub_get_work(monkeypatch, work: dict) -> None:
    async def fake_get_work(_doi, **_kw):
        return work

    monkeypatch.setattr(openalex, "get_work", fake_get_work)


# --- best_pdf_url ----------------------------------------------------------


class TestBestPdfUrl:
    def test_prefers_best_oa_location(self):
        work = {
            "best_oa_location": {"pdf_url": "http://x/best.pdf"},
            "primary_location": {"pdf_url": "http://x/primary.pdf"},
            "open_access": {"oa_url": "http://x/landing"},
        }
        assert openalex.best_pdf_url(work) == "http://x/best.pdf"

    def test_falls_back_to_primary_location(self):
        work = {
            "best_oa_location": {"pdf_url": None},
            "primary_location": {"pdf_url": "http://x/primary.pdf"},
            "open_access": {"oa_url": "http://x/landing"},
        }
        assert openalex.best_pdf_url(work) == "http://x/primary.pdf"

    def test_falls_back_to_oa_url(self):
        work = {
            "best_oa_location": {"pdf_url": None},
            "primary_location": {},
            "open_access": {"oa_url": "http://x/landing"},
        }
        assert openalex.best_pdf_url(work) == "http://x/landing"

    def test_returns_none_when_all_absent(self):
        assert openalex.best_pdf_url({"open_access": {"oa_url": None}}) is None

    def test_tolerates_explicit_null_subobjects(self):
        # OpenAlex returns these keys as explicit null for closed-access works.
        work = {
            "best_oa_location": None,
            "primary_location": None,
            "open_access": None,
        }
        assert openalex.best_pdf_url(work) is None


# --- metadata surfaces pdf_url ---------------------------------------------


class TestMetadataPdfUrl:
    def test_format_openalex_metadata_includes_pdf_url(self):
        work = {
            "title": "T",
            "doi": "https://doi.org/" + _DOI,
            "best_oa_location": {"pdf_url": "http://x/best.pdf"},
            "open_access": {"is_oa": True, "oa_status": "gold", "oa_url": "http://x/l"},
        }
        out = server._format_openalex_metadata(work, _DOI)
        assert out["pdf_url"] == "http://x/best.pdf"
        assert out["oa_url"] == "http://x/l"  # existing field preserved

    def test_pdf_url_none_for_closed_access(self):
        work = {"title": "T", "open_access": {"is_oa": False}}
        assert server._format_openalex_metadata(work, _DOI)["pdf_url"] is None


# --- oa_download.download_pdf ----------------------------------------------


class TestOaDownload:
    @pytest.mark.asyncio
    async def test_success_writes_pdf_to_manual_namespace(self, monkeypatch):
        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "http://x/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        fresh = [b"%PDF-1.4 ", b"OA ", b"BODY"]
        _install_stream(monkeypatch, _mock_stream_response(chunks=fresh))

        result = await oa_download.download_pdf(_DOI)

        dest = _oa_dest()
        assert result["cached"] is False
        assert dest.exists()
        assert dest.read_bytes() == b"".join(fresh)

    @pytest.mark.asyncio
    async def test_cache_hit_skips_network(self, monkeypatch):
        dest = _oa_dest()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 cached")

        def boom(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("network hit on cache hit")

        monkeypatch.setattr(openalex, "get_work", boom)

        result = await oa_download.download_pdf(_DOI)
        assert result["cached"] is True
        assert result["size_bytes"] == len(b"%PDF-1.4 cached")

    @pytest.mark.asyncio
    async def test_no_oa_url_returns_error(self, monkeypatch):
        _stub_get_work(
            monkeypatch,
            {"open_access": {"is_oa": False, "oa_status": "closed"}},
        )
        result = await oa_download.download_pdf(_DOI)
        assert "error" in result
        assert "no open-access pdf url" in result["error"].lower()
        assert "import_paper" in result["suggestion"]
        # Closed-access is a definitive, non-retryable condition for this paper.
        assert result["retryable"] is False
        assert not _oa_dest().exists()

    @pytest.mark.asyncio
    async def test_not_in_openalex_propagates_error(self, monkeypatch):
        # Mirrors openalex.get_work's real 404 payload, not_found flag included
        # — that flag is what marks the miss definitive.
        _stub_get_work(monkeypatch, {"error": f"No work found for DOI: {_DOI}", "not_found": True})
        result = await oa_download.download_pdf(_DOI)
        assert result["error"] == f"No work found for DOI: {_DOI}"
        # A definitive (non-retryable) OpenAlex miss keeps the import escape hatch.
        assert "import_paper" in result["suggestion"]
        assert not _oa_dest().exists()

    @pytest.mark.asyncio
    async def test_transient_openalex_error_has_no_import_suggestion(self, monkeypatch):
        # A retryable OpenAlex error (timeout / 5xx) is surfaced as-is: the
        # agent should retry, NOT be told to go fetch the PDF by hand.
        _stub_get_work(monkeypatch, {"error": "upstream timeout", "retryable": True})
        result = await oa_download.download_pdf(_DOI)
        assert result["error"] == "upstream timeout"
        assert result["retryable"] is True
        assert "suggestion" not in result
        assert not _oa_dest().exists()

    @pytest.mark.asyncio
    async def test_no_oa_url_failure_is_negative_cached(self, monkeypatch):
        _stub_get_work(
            monkeypatch,
            {"open_access": {"is_oa": False, "oa_status": "closed"}},
        )
        result1 = await oa_download.download_pdf(_DOI)
        assert "error" in result1

        # Second call must be served from the negative cache — no OpenAlex hit.
        def boom(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("network hit on negative-cache hit")

        monkeypatch.setattr(openalex, "get_work", boom)
        result2 = await oa_download.download_pdf(_DOI)
        assert result2 == result1
        assert "_expires_at" not in result2

    @pytest.mark.asyncio
    async def test_landing_page_failure_is_negative_cached(self, monkeypatch):
        # OpenAlex only knows an HTML landing page (oa_url, not a pdf_url).
        # stream_to_file rejects it; the rejection must be negative-cached so
        # a retrying agent doesn't re-fetch-and-reject the page every call.
        _stub_get_work(monkeypatch, {"open_access": {"oa_url": "http://x/landing"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(
            monkeypatch,
            _mock_stream_response(chunks=[b"<html>paywall</html>"], content_type="text/html"),
        )
        result1 = await oa_download.download_pdf(_DOI)
        assert "error" in result1
        assert not _oa_dest().exists()

        # Second call: served from negative cache — neither OpenAlex nor the
        # publisher host is touched.
        def boom(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("network hit on negative-cache hit")

        monkeypatch.setattr(openalex, "get_work", boom)
        monkeypatch.setattr(_clients, "get_client", boom)
        result2 = await oa_download.download_pdf(_DOI)
        assert result2 == result1
        assert "_expires_at" not in result2

    @pytest.mark.asyncio
    async def test_size_cap_failure_is_not_negative_cached(self, monkeypatch):
        # MAX_PDF_BYTES is a config knob, not a property of the paper. A
        # size-cap abort must NOT be negative-cached: raising the cap should
        # let the next call succeed without force_refresh.
        monkeypatch.setenv("MAX_PDF_BYTES", "4")
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(
            monkeypatch,
            _mock_stream_response(chunks=[b"%PDF-1.4 way over the four-byte cap"]),
        )

        calls = {"n": 0}

        async def counting_get_work(_doi, **_kw):
            calls["n"] += 1
            return {"best_oa_location": {"pdf_url": "http://x/p.pdf"}}

        monkeypatch.setattr(openalex, "get_work", counting_get_work)

        result1 = await oa_download.download_pdf(_DOI)
        assert "max_bytes" in result1

        # Second call re-resolves OpenAlex rather than serving a negative hit.
        result2 = await oa_download.download_pdf(_DOI)
        assert "max_bytes" in result2
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_force_refresh_ignores_negative_cache(self, monkeypatch):
        # Seed a negative entry with a first failed (closed-access) call.
        _stub_get_work(
            monkeypatch,
            {"open_access": {"is_oa": False, "oa_status": "closed"}},
        )
        first = await oa_download.download_pdf(_DOI)
        assert "error" in first

        # force_refresh must bypass (and clear) the negative entry: a now-OA
        # paper with a valid stream succeeds instead of returning the stale miss.
        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "http://x/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(monkeypatch, _mock_stream_response(chunks=[b"%PDF-1.4 fresh"]))
        result = await oa_download.download_pdf(_DOI, force_refresh=True)
        assert result["cached"] is False
        assert _oa_dest().read_bytes() == b"%PDF-1.4 fresh"

    @pytest.mark.asyncio
    async def test_a_transport_error_is_not_negative_cached(self, monkeypatch):
        """A timeout is not a fact about the paper. It was cached for 24h
        anyway: the predicate asked ``retryable is not True``, and
        ``_http.error_dict`` sets ``retryable`` on backpressure alone — so a
        timeout, a connection error, a 5xx and a 429 all arrived with no
        ``retryable`` key and were classified permanent. The predicate is now
        an allowlist (``retryable is False``)."""
        resolves = 0

        async def counting_get_work(doi, **kwargs):
            nonlocal resolves
            resolves += 1
            return {"best_oa_location": {"pdf_url": "http://x/p.pdf"}}

        monkeypatch.setattr(openalex, "get_work", counting_get_work)
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)

        class ExplodingClient:
            def stream(self, *_args, **_kwargs):
                raise httpx.ReadTimeout("boom")

        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: ExplodingClient())

        first = await oa_download.download_pdf(_DOI)
        assert "error" in first
        assert "timed out" in first["error"]

        # The second call must re-resolve and re-attempt, not serve a cached
        # verdict that the paper has no open-access copy.
        second = await oa_download.download_pdf(_DOI)
        assert "error" in second
        assert resolves == 2, "a transient failure was negative-cached"

    @pytest.mark.asyncio
    async def test_a_definitive_failure_is_still_negative_cached(self, monkeypatch):
        """The allowlist must not swing the other way: a real "no OA copy"
        verdict still short-circuits, so a retrying agent doesn't re-resolve
        OpenAlex on every call."""
        resolves = 0

        async def counting_get_work(doi, **kwargs):
            nonlocal resolves
            resolves += 1
            return {"open_access": {"is_oa": False, "oa_status": "closed"}}

        monkeypatch.setattr(openalex, "get_work", counting_get_work)

        assert "error" in await oa_download.download_pdf(_DOI)
        assert "error" in await oa_download.download_pdf(_DOI)
        assert resolves == 1, "a definitive miss should be served from the negative cache"

    @pytest.mark.asyncio
    async def test_zero_byte_cached_file_is_a_miss(self, monkeypatch):
        # A 0-byte leftover from an interrupted write must not be served as a
        # cache hit — it's re-downloaded like the manual import path does.
        dest = _oa_dest()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"")

        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "http://x/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(monkeypatch, _mock_stream_response(chunks=[b"%PDF-1.4 fresh"]))

        result = await oa_download.download_pdf(_DOI)
        assert result["cached"] is False
        assert dest.read_bytes() == b"%PDF-1.4 fresh"

    @pytest.mark.asyncio
    async def test_an_unclassified_openalex_4xx_has_no_import_suggestion(self, monkeypatch):
        """Regression: an OpenAlex 403 used to be read as a definitive miss.

        ``_http.error_dict`` leaves every non-retryable 4xx *unflagged* — no
        ``retryable`` key at all — so a denylist (``retryable is not True``)
        classified a 403, a 451 and the defensive fallback as dead ends and
        told the agent to go fetch the PDF by hand. The classifier is an
        allowlist: only ``not_found`` or ``retryable is False`` earns the hatch.
        """
        resolves = 0

        async def counting_get_work(_doi, **_kw):
            nonlocal resolves
            resolves += 1
            return {"error": "OpenAlex HTTP 403: <html>Forbidden</html>"}

        monkeypatch.setattr(openalex, "get_work", counting_get_work)

        result = await oa_download.download_pdf(_DOI)
        assert result["error"].startswith("OpenAlex HTTP 403")
        assert "suggestion" not in result, "an unknown verdict is not a dead end"

        # And it is not negative-cached, so the next call really re-resolves.
        await oa_download.download_pdf(_DOI)
        assert resolves == 2

    @pytest.mark.asyncio
    async def test_slot_is_keyed_on_the_resolved_publisher_url(self, monkeypatch):
        """The throttle is ``per_host=True``, so the argument handed to
        ``_request_slot`` is what per-host pacing keys on. Passing the DOI, the
        dest path or a constant would silently collapse every publisher onto
        one key while every other test stayed green."""
        seen = []

        @contextlib.asynccontextmanager
        async def recording_slot(url):
            seen.append(url)
            yield

        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "https://pub.example/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", recording_slot)
        _install_stream(monkeypatch, _mock_stream_response(chunks=[b"%PDF-1.4 x"]))

        await oa_download.download_pdf(_DOI)
        assert seen == ["https://pub.example/p.pdf"]

    @pytest.mark.asyncio
    async def test_request_slot_paces_per_publisher_host(self):
        """The real wrapper, not the passthrough every other test installs:
        `per_host=True` means the last-start map is keyed by netloc, so two
        publishers never pace each other."""
        async with oa_download._request_slot("https://a.example/one.pdf"):
            pass
        async with oa_download._request_slot("https://b.example:8443/two.pdf"):
            pass

        assert set(oa_download._throttle._last_start) == {"a.example", "b.example:8443"}

    @pytest.mark.asyncio
    async def test_force_refresh_preserves_the_cached_pdf_on_404(self, monkeypatch):
        # The OA dest lives in the shared `manual` namespace, so a destroyed
        # file may be the operator's own imported paper.
        dest = _oa_dest()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 OLD cached bytes")

        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "http://x/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(monkeypatch, _mock_stream_response(status_code=404))

        result = await oa_download.download_pdf(_DOI, force_refresh=True)
        assert result["retryable"] is False
        assert dest.read_bytes() == b"%PDF-1.4 OLD cached bytes"
        assert not list(dest.parent.glob("*.tmp")), "temp file leaked"

    @pytest.mark.asyncio
    async def test_force_refresh_preserves_the_cached_pdf_on_transport_error(self, monkeypatch):
        dest = _oa_dest()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 OLD cached bytes")

        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "http://x/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)

        class ExplodingClient:
            def stream(self, *_args, **_kwargs):
                raise httpx.ConnectError("boom")

        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: ExplodingClient())

        result = await oa_download.download_pdf(_DOI, force_refresh=True)
        assert result["retryable"] is True
        assert dest.read_bytes() == b"%PDF-1.4 OLD cached bytes"
        assert not list(dest.parent.glob("*.tmp")), "temp file leaked"

    @pytest.mark.asyncio
    async def test_force_refresh_preserves_the_cached_pdf_on_size_cap(self, monkeypatch):
        dest = _oa_dest()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 OLD cached bytes")

        monkeypatch.setenv("MAX_PDF_BYTES", "4")
        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "http://x/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(monkeypatch, _mock_stream_response(chunks=[b"%PDF-1.4 far too long"]))

        result = await oa_download.download_pdf(_DOI, force_refresh=True)
        assert result["max_bytes"] == 4
        assert "suggestion" not in result, "raise the cap, don't fetch it by hand"
        assert dest.read_bytes() == b"%PDF-1.4 OLD cached bytes"
        assert not list(dest.parent.glob("*.tmp")), "temp file leaked"

    @pytest.mark.asyncio
    async def test_force_refresh_restreams_over_a_usable_cached_pdf(self, monkeypatch):
        """``cached_download`` skips its in-slot re-check under force_refresh —
        without that skip a refresh is a no-op exactly when a usable PDF is
        already on disk, which is the case force_refresh exists to fix."""
        dest = _oa_dest()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 OLD cached bytes")

        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "http://x/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(monkeypatch, _mock_stream_response(chunks=[b"%PDF-1.4 NEW"]))

        result = await oa_download.download_pdf(_DOI, force_refresh=True)
        assert result["cached"] is False
        assert dest.read_bytes() == b"%PDF-1.4 NEW"

    @pytest.mark.asyncio
    async def test_force_refresh_clears_the_negative_entry(self, monkeypatch):
        # Not merely bypasses it: a later *plain* call must not be served the
        # stale verdict the refresh already disproved.
        _stub_get_work(monkeypatch, {"open_access": {"is_oa": False, "oa_status": "closed"}})
        assert "error" in await oa_download.download_pdf(_DOI)

        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "http://x/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(monkeypatch, _mock_stream_response(chunks=[b"%PDF-1.4 fresh"]))
        await oa_download.download_pdf(_DOI, force_refresh=True)

        _oa_dest().unlink()  # force the plain call past the artifact short-circuit
        plain = await oa_download.download_pdf(_DOI)
        assert "error" not in plain, "the cleared negative entry came back"

    @pytest.mark.asyncio
    async def test_magic_bytes_reject_an_html_interstitial_labelled_as_pdf(self, monkeypatch):
        """``require_pdf`` is this path's trust boundary and the Content-Type is
        only advisory — a publisher serving an interstitial under
        ``application/pdf`` headers is the case the magic-byte sniff exists for."""
        _stub_get_work(monkeypatch, {"open_access": {"oa_url": "http://x/landing"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(
            monkeypatch,
            _mock_stream_response(
                chunks=[b"<html>Sign in to continue</html>"], content_type="application/pdf"
            ),
        )

        result = await oa_download.download_pdf(_DOI)
        assert "%PDF-" in result["error"]
        assert result["retryable"] is False
        assert not _oa_dest().exists()
        assert "import_paper" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_a_publisher_404_names_the_url_and_is_negative_cached(self, monkeypatch):
        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "http://pub.example/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(monkeypatch, _mock_stream_response(status_code=404))

        first = await oa_download.download_pdf(_DOI)
        # oa_download passes its own not_found_message; both halves are read by
        # the agent to tell "wrong URL" apart from "wrong paper".
        assert "http://pub.example/p.pdf" in first["error"]
        assert _DOI in first["error"]
        assert first["retryable"] is False
        assert "import_paper" in first["suggestion"], "a dead OA URL earns the hatch"

        def boom(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("network hit on negative-cache hit")

        monkeypatch.setattr(openalex, "get_work", boom)
        monkeypatch.setattr(_clients, "get_client", boom)
        assert await oa_download.download_pdf(_DOI) == first

    @pytest.mark.asyncio
    async def test_an_empty_body_is_retryable_and_not_negative_cached(self, monkeypatch):
        """A 200 with no bytes is a blip, not a fact about the paper — the
        %PDF- sniff cannot catch it because the loop body never runs."""
        resolves = 0

        async def counting_get_work(_doi, **_kw):
            nonlocal resolves
            resolves += 1
            return {"best_oa_location": {"pdf_url": "http://x/p.pdf"}}

        monkeypatch.setattr(openalex, "get_work", counting_get_work)
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        _install_stream(monkeypatch, _mock_stream_response(chunks=[]))

        result = await oa_download.download_pdf(_DOI)
        assert result["retryable"] is True
        assert "empty body" in result["error"]
        assert "suggestion" not in result, "a blip is not hand-fetch work"
        assert not _oa_dest().exists()

        await oa_download.download_pdf(_DOI)
        assert resolves == 2

    @pytest.mark.asyncio
    async def test_get_client_bakes_in_the_user_agent_and_pdf_timeout(self):
        client = oa_download._get_client()
        assert client.headers["user-agent"].startswith("academic-tools-mcp/")
        assert client.timeout.read == oa_download._PDF_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_the_pdf_timeout_reaches_the_request(self, monkeypatch):
        """``stream_to_file``'s ``timeout`` has no default so every provider
        states one; this pins that oa_download's actually rides the request."""
        requests: list[httpx.Request] = []
        client = _streaming_client(
            200, b"%PDF-1.4 body", content_type="application/pdf", requests=requests
        )
        _stub_get_work(monkeypatch, {"best_oa_location": {"pdf_url": "http://pub.example/p.pdf"}})
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: client)
        try:
            result = await oa_download.download_pdf(_DOI)
        finally:
            await client.aclose()

        assert result["cached"] is False
        assert requests[0].extensions["timeout"]["read"] == oa_download._PDF_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_concurrent_downloads_collapse_to_one_stream(self, monkeypatch):
        # Single-flight: five agents asking for the same DOI cost one OpenAlex
        # lookup and one publisher stream, not five of each.
        resolves = 0
        streams = 0
        gate = asyncio.Event()

        async def counting_get_work(_doi, **_kw):
            nonlocal resolves
            resolves += 1
            return {"best_oa_location": {"pdf_url": "http://x/p.pdf"}}

        def make_stream(*_args, **_kwargs):
            @contextlib.asynccontextmanager
            async def cm():
                nonlocal streams
                streams += 1
                response = _mock_stream_response(chunks=[b"%PDF-1.4 shared"])
                async with response() as inner:
                    # Hold the stream open so followers pile up behind the
                    # single-flight slot before the leader resolves.
                    await gate.wait()
                    yield inner

            return cm()

        class GatedClient:
            stream = staticmethod(make_stream)

        monkeypatch.setattr(openalex, "get_work", counting_get_work)
        monkeypatch.setattr(oa_download, "_request_slot", _passthrough_slot)
        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: GatedClient())

        tasks = [asyncio.create_task(oa_download.download_pdf(_DOI)) for _ in range(5)]
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*tasks)

        assert streams == 1
        assert resolves == 1
        assert all(r["size_bytes"] == len(b"%PDF-1.4 shared") for r in results)


# --- stream_to_file content guard ------------------------------------------


def _stub_stream_client(stream_cm):
    class StubClient:
        def stream(self, *_args, **_kwargs):
            return stream_cm()

    return StubClient()


class TestRequirePdfGuard:
    @pytest.mark.asyncio
    async def test_rejects_html_content_type(self, tmp_path):
        dest = tmp_path / "out.pdf"
        client = _stub_stream_client(
            _mock_stream_response(
                chunks=[b"<html>paywall</html>"], content_type="text/html; charset=utf-8"
            )
        )
        result = await _pdf_download.stream_to_file(
            client,
            "http://x/landing",
            dest,
            slot_factory=_passthrough_slot,
            namespace="oa_download",
            provider_label="OA download",
            require_pdf=True,
            timeout=_TIMEOUT,
        )
        assert "error" in result and result["retryable"] is False
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_rejects_missing_magic_bytes(self, tmp_path):
        dest = tmp_path / "out.pdf"
        # Content-Type lies (says octet-stream) but body isn't a PDF.
        client = _stub_stream_client(
            _mock_stream_response(
                chunks=[b"not a pdf at all"], content_type="application/octet-stream"
            )
        )
        result = await _pdf_download.stream_to_file(
            client,
            "http://x/x",
            dest,
            slot_factory=_passthrough_slot,
            namespace="oa_download",
            provider_label="OA download",
            require_pdf=True,
            timeout=_TIMEOUT,
        )
        assert "error" in result and result["retryable"] is False
        assert "%PDF-" in result["error"]
        assert not dest.exists()

    @pytest.mark.asyncio
    async def test_accepts_octet_stream_with_valid_magic(self, tmp_path):
        dest = tmp_path / "out.pdf"
        client = _stub_stream_client(
            _mock_stream_response(
                chunks=[b"%PDF-1.5 body"], content_type="application/octet-stream"
            )
        )
        result = await _pdf_download.stream_to_file(
            client,
            "http://x/x",
            dest,
            slot_factory=_passthrough_slot,
            namespace="oa_download",
            provider_label="OA download",
            require_pdf=True,
            timeout=_TIMEOUT,
        )
        assert "error" not in result
        assert dest.read_bytes() == b"%PDF-1.5 body"

    @pytest.mark.asyncio
    async def test_accepts_missing_content_type_with_valid_magic(self, tmp_path):
        dest = tmp_path / "out.pdf"
        client = _stub_stream_client(
            _mock_stream_response(chunks=[b"%PDF-1.5 body"], content_type="")
        )
        result = await _pdf_download.stream_to_file(
            client,
            "http://x/x",
            dest,
            slot_factory=_passthrough_slot,
            namespace="oa_download",
            provider_label="OA download",
            require_pdf=True,
            timeout=_TIMEOUT,
        )
        assert "error" not in result
        assert dest.read_bytes() == b"%PDF-1.5 body"


# --- server dispatch gating ------------------------------------------------


class TestServerDispatch:
    @pytest.mark.asyncio
    async def test_refusal_when_not_opted_in(self):
        result = await server._download_pdf_by_provider(_DOI, allow_oa_url=False)
        assert "Cannot auto-download" in result["error"]
        assert "allow_oa_url=True" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_routes_through_oa_download_when_opted_in(self, monkeypatch):
        called = {}

        async def fake_oa_download(identifier, *, force_refresh=False):
            called["id"] = identifier
            return {"path": "/x.pdf", "size_bytes": 10, "cached": False}

        monkeypatch.setattr(oa_download, "download_pdf", fake_oa_download)
        result = await server._download_pdf_by_provider(_DOI, allow_oa_url=True)
        assert called["id"] == _DOI
        assert result["cached"] is False

    @pytest.mark.asyncio
    async def test_force_refresh_cascade_on_oa_path(self, monkeypatch):
        # A real OA re-download (cached=False) must drop the manual-namespace
        # markdown + section index, just like the native providers.
        from academic_tools_mcp import papers

        target = manual.resolve_target(_DOI)
        ns, canonical = target["namespace"], target["canonical"]
        md_path = papers.markdown_path(ns, canonical)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("stale markdown")
        cache.put(ns, "sections", papers.sections_key(canonical), {"sections": []})

        async def fake_oa_download(identifier, *, force_refresh=False):
            return {"path": "/x.pdf", "size_bytes": 10, "cached": False}

        monkeypatch.setattr(oa_download, "download_pdf", fake_oa_download)

        result = await server._download_pdf_by_provider(_DOI, force_refresh=True, allow_oa_url=True)

        assert result["cascaded_invalidated"] == ["markdown", "sections"]
        assert not md_path.exists()
        assert cache.get(ns, "sections", papers.sections_key(canonical)) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "identifier",
        ["2301.00001", "10.1101/2020.01.01.000001", "10.18653/v1/2020.acl-main.1"],
        ids=["arxiv", "biorxiv", "acl"],
    )
    async def test_native_identifiers_never_reach_oa_download(self, identifier, monkeypatch):
        """``allow_oa_url=True`` is the generic-DOI escape hatch, not a
        provider override. ``oa_download.download_pdf`` resolves its dest
        through ``manual.resolve_target`` unconditionally, so a native id
        reaching it would write the PDF into that provider's namespace while
        filing the negative verdict under ``oa_download``."""

        def boom(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("a native identifier reached the OA path")

        monkeypatch.setattr(oa_download, "download_pdf", boom)

        async def fake_native(_id, *, force_refresh=False):
            return {"path": "/x.pdf", "size_bytes": 10, "cached": True}

        for mod in (arxiv, biorxiv, acl_anthology):
            monkeypatch.setattr(mod, "download_pdf", fake_native)

        result = await server._download_pdf_by_provider(identifier, allow_oa_url=True)
        assert result["cached"] is True
