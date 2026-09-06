"""Regression tests for ``download_pdf(force_refresh=True)`` data safety.

All three PDF providers used to ``dest.unlink()``
the cached PDF *before* the re-download was attempted, so any failed refetch
(404, transport error, MAX_PDF_BYTES abort) destroyed the only copy. The fix
drops the up-front unlink and lets ``_pdf_download.stream_to_file``'s atomic
``os.replace`` overwrite the file only once the new bytes are in hand.

These tests exercise each provider's ``download_pdf`` end-to-end with a stubbed
HTTP client and assert the cached file survives every failure path and is
correctly overwritten on success.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import httpx
import pytest

from academic_tools_mcp import cache
from academic_tools_mcp.providers import acl_anthology, arxiv, biorxiv

from ._download_fakes import install_stream as _install_stream
from ._download_fakes import mock_stream_response as _mock_stream_response
from ._download_fakes import passthrough_slot as _passthrough_slot


def _connect_error_stream():
    """A client.stream(...) that raises a transport error on enter."""

    @contextlib.asynccontextmanager
    async def boom():
        raise httpx.ConnectError("connection refused")
        yield  # unreachable; makes this an (async) generator

    return boom()


# --- Per-provider plumbing -------------------------------------------------
#
# Each entry knows its identifier, how to compute the cached PDF path, and how
# to satisfy the metadata lookup that download_pdf does before streaming.

_ARXIV_ID = "2301.00001"
_BIORXIV_DOI = "10.1101/2020.01.01.000001"
_ACL_DOI = "10.18653/v1/P16-1160"


def _arxiv_dest() -> Path:
    canonical = arxiv.canonical_arxiv_id(_ARXIV_ID)
    return cache.cache_dir(arxiv.NAMESPACE, "pdfs") / arxiv._pdf_filename(canonical)


def _biorxiv_dest() -> Path:
    canonical = biorxiv.canonical_key(_BIORXIV_DOI)
    return cache.cache_dir(biorxiv.NAMESPACE, "pdfs") / biorxiv._pdf_filename(canonical)


def _acl_dest() -> Path:
    aid = acl_anthology.doi_to_anthology_id(_ACL_DOI)
    return cache.cache_dir(acl_anthology.NAMESPACE, "pdfs") / acl_anthology._pdf_filename(aid)


def _setup_provider(name: str, monkeypatch) -> tuple[Path, callable]:
    """Wire up one provider for testing and return (cached PDF dest, call).

    ``call`` invokes that provider's ``download_pdf(force_refresh=True)``.
    The metadata lookup is stubbed so only the PDF fetch can fail.
    """
    if name == "arxiv":
        mod, identifier, dest = arxiv, _ARXIV_ID, _arxiv_dest()

        async def fake_get_paper(_id, **_kw):
            return {"links": [{"title": "pdf", "href": "http://example.com/x.pdf"}]}

        monkeypatch.setattr(arxiv, "get_paper", fake_get_paper)
    elif name == "biorxiv":
        mod, identifier, dest = biorxiv, _BIORXIV_DOI, _biorxiv_dest()

        async def fake_get_paper(_doi, **_kw):
            return {"pdf_url": "http://example.com/x.pdf"}

        monkeypatch.setattr(biorxiv, "get_paper", fake_get_paper)
    elif name == "acl":
        mod, identifier, dest = acl_anthology, _ACL_DOI, _acl_dest()
        # ACL builds the URL deterministically — no metadata lookup.
    else:  # pragma: no cover - guard
        raise ValueError(name)

    # Skip the real rate-limit slot (and its sleeps).
    monkeypatch.setattr(mod, "_request_slot", _passthrough_slot)

    async def call():
        return await mod.download_pdf(identifier, force_refresh=True)

    return dest, call


_PROVIDERS = ["arxiv", "biorxiv", "acl"]


def _seed_cached_pdf(dest: Path, body: bytes = b"%PDF-1.4 OLD cached bytes") -> bytes:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return body


class TestForceRefreshPreservesOnFailure:
    """A failed force_refresh re-download must leave the old PDF intact."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", _PROVIDERS)
    async def test_404_preserves_cached_pdf(self, provider, monkeypatch):
        dest, call = _setup_provider(provider, monkeypatch)
        old = _seed_cached_pdf(dest)
        _install_stream(monkeypatch, _mock_stream_response(status_code=404))

        result = await call()

        assert "error" in result
        assert dest.exists(), "cached PDF was destroyed by a failed refresh"
        assert dest.read_bytes() == old

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", _PROVIDERS)
    async def test_transport_error_preserves_cached_pdf(self, provider, monkeypatch):
        dest, call = _setup_provider(provider, monkeypatch)
        old = _seed_cached_pdf(dest)
        _install_stream(monkeypatch, _connect_error_stream)

        result = await call()

        assert "error" in result
        assert dest.exists()
        assert dest.read_bytes() == old

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", _PROVIDERS)
    async def test_size_cap_abort_preserves_cached_pdf(self, provider, monkeypatch):
        monkeypatch.setenv("MAX_PDF_BYTES", "10")
        dest, call = _setup_provider(provider, monkeypatch)
        old = _seed_cached_pdf(dest)
        # 15 bytes across three 5-byte chunks; the third trips the 10-byte cap.
        chunks = [b"a" * 5, b"b" * 5, b"c" * 5]
        _install_stream(monkeypatch, _mock_stream_response(chunks=chunks))

        result = await call()

        assert "error" in result and result.get("retryable") is False
        assert dest.exists()
        assert dest.read_bytes() == old


class TestForceRefreshOverwritesOnSuccess:
    """A successful force_refresh must atomically replace the old bytes."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", _PROVIDERS)
    async def test_success_replaces_cached_pdf(self, provider, monkeypatch):
        dest, call = _setup_provider(provider, monkeypatch)
        _seed_cached_pdf(dest)
        fresh = [b"%PDF-1.4 ", b"BRAND ", b"NEW"]
        _install_stream(monkeypatch, _mock_stream_response(chunks=fresh))

        result = await call()

        assert "error" not in result
        assert result["cached"] is False
        assert dest.read_bytes() == b"".join(fresh)
        assert not list(dest.parent.glob("*.tmp")), "temp file leaked"


class TestAclForceRefreshBypassesInnerRecheck:
    """ACL's single-flight _fetch re-checks dest existence; under
    force_refresh that re-check must NOT short-circuit to the cached
    file, or the refresh would be a no-op when a cached copy exists."""

    @pytest.mark.asyncio
    async def test_inner_recheck_does_not_serve_stale(self, monkeypatch):
        dest, call = _setup_provider("acl", monkeypatch)
        _seed_cached_pdf(dest)
        fresh = [b"%PDF-1.4 ", b"camera-ready v2"]
        _install_stream(monkeypatch, _mock_stream_response(chunks=fresh))

        result = await call()

        # A real download happened (cached=False) and replaced the bytes,
        # rather than the inner `if dest.exists()` returning cached=True.
        assert result.get("cached") is False
        assert dest.read_bytes() == b"".join(fresh)
