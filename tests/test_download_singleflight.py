"""Concurrent ``download_pdf`` calls for the same identifier must collapse
into a single streaming download.

arXiv and bioRxiv previously guarded ``download_pdf`` only with a
``dest.exists()`` check, so two parallel calls for the same id could both miss
the check and both stream the file (atomic rename kept the *result* correct,
but doubled bandwidth and throttle cost). They now wrap the fetch in
single-flight — keyed ``("pdf", canonical)`` so the inner ``get_paper`` call,
which is single-flighted on the bare ``canonical``, doesn't deadlock on the
download's own slot. ACL already had this; these tests pin it for arXiv and
bioRxiv.

The conftest autouse fixture installs a fresh ``_single_flight`` per provider
before each test, so no manual reset is needed here.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from academic_tools_mcp import _clients, cache
from academic_tools_mcp.providers import arxiv, biorxiv

from ._download_fakes import passthrough_slot as _passthrough_slot

_ARXIV_ID = "2301.00001"
_BIORXIV_DOI = "10.1101/2020.01.01.000001"


def _dest(mod, identifier: str) -> Path:
    canonical = (
        arxiv.canonical_arxiv_id(identifier) if mod is arxiv else biorxiv.canonical_key(identifier)
    )
    return cache.cache_dir(mod.NAMESPACE, "pdfs") / mod._pdf_filename(canonical)


def _setup(mod, identifier: str, monkeypatch) -> None:
    """Stub the metadata lookup and rate-limit slot for one provider."""
    if mod is arxiv:

        async def fake_get_paper(_id, **_kw):
            return {"links": [{"title": "pdf", "href": "http://example.com/x.pdf"}]}
    else:

        async def fake_get_paper(_doi, **_kw):
            return {"pdf_url": "http://example.com/x.pdf"}

    monkeypatch.setattr(mod, "get_paper", fake_get_paper)
    monkeypatch.setattr(mod, "_request_slot", _passthrough_slot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mod", "identifier"),
    [(arxiv, _ARXIV_ID), (biorxiv, _BIORXIV_DOI)],
    ids=["arxiv", "biorxiv"],
)
async def test_concurrent_downloads_collapse_to_one_stream(mod, identifier, monkeypatch):
    _setup(mod, identifier, monkeypatch)

    stream_calls = 0
    gate = asyncio.Event()

    def make_stream(*_args, **_kwargs):
        @contextlib.asynccontextmanager
        async def cm():
            nonlocal stream_calls
            stream_calls += 1
            response = MagicMock()
            response.status_code = 200
            response.raise_for_status = MagicMock()

            async def aiter_bytes(_chunk_size):
                # Hold the stream open until released, so followers pile up
                # behind the single-flight slot before the leader resolves.
                await gate.wait()
                yield b"%PDF-1.4 the bytes"

            response.aiter_bytes = aiter_bytes
            yield response

        return cm()

    class StubClient:
        def stream(self, *args, **kwargs):
            return make_stream(*args, **kwargs)

    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())

    tasks = [asyncio.create_task(mod.download_pdf(identifier)) for _ in range(5)]
    for _ in range(5):  # let all five register on the same in-flight slot
        await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert stream_calls == 1, (
        f"single-flight should have coalesced 5 downloads into 1 stream, got {stream_calls}"
    )
    assert all(r.get("cached") is False for r in results)
    assert all(r["path"] == str(_dest(mod, identifier)) for r in results)
    assert _dest(mod, identifier).read_bytes() == b"%PDF-1.4 the bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mod", "identifier"),
    [(arxiv, _ARXIV_ID), (biorxiv, _BIORXIV_DOI)],
    ids=["arxiv", "biorxiv"],
)
async def test_download_slot_does_not_deadlock_on_get_paper(mod, identifier, monkeypatch):
    """A regression guard for the keying choice: the real ``get_paper`` is
    single-flighted on the bare canonical id. If ``download_pdf`` shared that
    key, its inner ``get_paper`` would await the download's own future forever.
    Here ``get_paper`` routes through the *real* single-flight to prove the two
    slots are distinct."""
    if mod is arxiv:

        async def stub_get(_id, **_kw):
            return {"links": [{"title": "pdf", "href": "http://example.com/x.pdf"}]}
    else:

        async def stub_get(_doi, **_kw):
            return {"pdf_url": "http://example.com/x.pdf"}

    # Wrap the metadata fetch in the provider's *real* single-flight, mirroring
    # production, so a shared key would deadlock.
    async def fake_get_paper(ident, **_kw):
        canonical = (
            arxiv.canonical_arxiv_id(ident) if mod is arxiv else biorxiv.canonical_key(ident)
        )
        return await mod._single_flight.do(canonical, lambda: stub_get(ident))

    monkeypatch.setattr(mod, "get_paper", fake_get_paper)
    monkeypatch.setattr(mod, "_request_slot", _passthrough_slot)

    async def aiter_bytes(_chunk_size):
        yield b"%PDF-1.4 ok"

    @contextlib.asynccontextmanager
    async def stream_cm(*_a, **_kw):
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.aiter_bytes = aiter_bytes
        yield response

    class StubClient:
        def stream(self, *a, **kw):
            return stream_cm(*a, **kw)

    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())

    result = await asyncio.wait_for(mod.download_pdf(identifier), timeout=2.0)

    assert result.get("cached") is False
    assert result["path"] == str(_dest(mod, identifier))
