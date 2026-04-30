"""Tests for the audit-driven feature changes.

Covers:
  - The download_pdf → markdown/sections cascade in
    ``server._download_pdf_by_provider`` (item 3 of the audit).
  - ``openalex.get_works_batch`` and the ``get_papers_metadata`` MCP
    tool (item 4).
  - ``papers.find_in_markdown`` and the ``find_in_paper`` MCP tool
    (item 5).

The throttle / streaming primitives have their own focused test
modules (``test_concurrency.py`` and ``test_pdf_download.py``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_tools_mcp import cache, openalex, papers, server


# ---------------------------------------------------------------------------
# Cascade: re-downloading a PDF should drop cached markdown + sections
# ---------------------------------------------------------------------------


class TestDownloadPdfCascade:
    @pytest.mark.asyncio
    async def test_force_refresh_drops_markdown_and_sections(
        self, tmp_path: Path, monkeypatch
    ):
        """force_refresh=True with cached=False in the result invalidates
        the converted markdown and section index for that paper."""
        # Stub arxiv.download_pdf to claim a successful re-download
        async def fake_download(arxiv_id, *, force_refresh=False):
            return {
                "path": "/tmp/dummy.pdf",
                "size_bytes": 1234,
                "cached": False,
            }

        monkeypatch.setattr(server.arxiv, "download_pdf", fake_download)

        # Place a fake markdown + sections cache for the canonical id
        canonical = "2301.00001"
        md_path = papers._markdown_path("arxiv", canonical)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# Stale\n\nold markdown content\n")
        cache.put(
            "arxiv",
            "sections",
            papers._sections_key(canonical),
            {"sections": [{"index": 0, "title": "Stale"}], "markdown_checksum": "x"},
        )
        assert md_path.exists()

        try:
            result = await server._download_pdf_by_provider(
                "2301.00001", force_refresh=True
            )

            # The cascade must have happened
            assert "error" not in result
            assert result.get("cached") is False
            assert result.get("cascaded_invalidated") == ["markdown", "sections"]
            assert not md_path.exists(), "Markdown should have been deleted"
            assert (
                cache.get("arxiv", "sections", papers._sections_key(canonical))
                is None
            ), "Sections cache should have been invalidated"
        finally:
            md_path.unlink(missing_ok=True)
            cache.invalidate("arxiv", "sections", papers._sections_key(canonical))

    @pytest.mark.asyncio
    async def test_no_cascade_on_cache_hit(self, tmp_path: Path, monkeypatch):
        """When the PDF was served from cache (cached=True), no cascade —
        the existing markdown is still consistent with the bytes on disk."""
        async def fake_download(arxiv_id, *, force_refresh=False):
            return {
                "path": "/tmp/dummy.pdf",
                "size_bytes": 1234,
                "cached": True,
            }

        monkeypatch.setattr(server.arxiv, "download_pdf", fake_download)

        canonical = "2301.00002"
        md_path = papers._markdown_path("arxiv", canonical)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# Fresh\n\nstill valid\n")

        try:
            result = await server._download_pdf_by_provider(
                "2301.00002", force_refresh=True
            )
            assert result.get("cached") is True
            assert "cascaded_invalidated" not in result
            assert md_path.exists(), "Cached-hit must NOT delete markdown"
        finally:
            md_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_no_cascade_when_force_refresh_false(self, monkeypatch):
        """Without force_refresh, cascade never fires even on cache miss."""
        async def fake_download(arxiv_id, *, force_refresh=False):
            return {"path": "/tmp/dummy.pdf", "size_bytes": 100, "cached": False}

        monkeypatch.setattr(server.arxiv, "download_pdf", fake_download)

        canonical = "2301.00003"
        md_path = papers._markdown_path("arxiv", canonical)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# Untouched\n")

        try:
            result = await server._download_pdf_by_provider(
                "2301.00003", force_refresh=False
            )
            assert "cascaded_invalidated" not in result
            assert md_path.exists()
        finally:
            md_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# openalex.get_works_batch
# ---------------------------------------------------------------------------


class TestGetWorksBatch:
    @pytest.mark.asyncio
    async def test_serves_cached_without_http(self, monkeypatch):
        """If every input DOI is already cached, no HTTP call is made."""
        canonicals = ["10.1/x", "10.2/y"]
        for c in canonicals:
            cache.put("openalex", "works", c, {"id": c, "title": f"work {c}"})

        called = []

        async def trap(*args, **kwargs):
            called.append((args, kwargs))
            raise AssertionError("Should not have hit the network")

        monkeypatch.setattr(openalex, "_throttled_get", trap)

        try:
            out = await openalex.get_works_batch(canonicals)
            assert set(out.keys()) == set(canonicals)
            assert out["10.1/x"]["title"] == "work 10.1/x"
            assert called == []
        finally:
            for c in canonicals:
                cache.invalidate("openalex", "works", c)

    @pytest.mark.asyncio
    async def test_fetches_misses_in_batch(self, monkeypatch):
        """Cache misses go out as one /works?filter=doi:...|... call."""
        # Pretend response: two works keyed by their DOIs
        async def fake_throttled(url, **kwargs):
            params = kwargs["params"]
            assert "filter" in params
            assert params["filter"].startswith("doi:")
            assert "10.1/a" in params["filter"]
            assert "10.2/b" in params["filter"]
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={
                "results": [
                    {"id": "W1", "doi": "https://doi.org/10.1/a", "title": "A"},
                    {"id": "W2", "doi": "https://doi.org/10.2/b", "title": "B"},
                ]
            })
            return resp

        monkeypatch.setattr(openalex, "_throttled_get", fake_throttled)

        for c in ("10.1/a", "10.2/b"):
            cache.invalidate("openalex", "works", c)

        try:
            out = await openalex.get_works_batch(["10.1/a", "10.2/b"])
            assert out["10.1/a"]["title"] == "A"
            assert out["10.2/b"]["title"] == "B"
            # Each result is also written to the singleton cache
            assert cache.get("openalex", "works", "10.1/a")["title"] == "A"
            assert cache.get("openalex", "works", "10.2/b")["title"] == "B"
        finally:
            for c in ("10.1/a", "10.2/b"):
                cache.invalidate("openalex", "works", c)

    @pytest.mark.asyncio
    async def test_unmatched_dois_become_negative_cache(self, monkeypatch):
        """A DOI requested in the batch but absent from the response is
        cached negatively, same as a singleton 404."""
        async def fake_throttled(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            # Only return the first DOI
            resp.json = MagicMock(return_value={
                "results": [
                    {"id": "W1", "doi": "https://doi.org/10.1/found", "title": "F"},
                ]
            })
            return resp

        monkeypatch.setattr(openalex, "_throttled_get", fake_throttled)

        for c in ("10.1/found", "10.1/missing"):
            cache.invalidate("openalex", "works", c)

        try:
            out = await openalex.get_works_batch(["10.1/found", "10.1/missing"])
            assert out["10.1/found"]["title"] == "F"
            assert "error" in out["10.1/missing"]
            # Negative cache populated for the missing one
            neg = cache.get_negative("openalex", "works", "10.1/missing")
            assert neg is not None
            assert "error" in neg
        finally:
            for c in ("10.1/found", "10.1/missing"):
                cache.invalidate("openalex", "works", c)

    @pytest.mark.asyncio
    async def test_force_refresh_drops_cached_first(self, monkeypatch):
        cache.put(
            "openalex", "works", "10.1/x", {"id": "old", "doi": "https://doi.org/10.1/x"}
        )

        async def fake_throttled(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={
                "results": [
                    {"id": "new", "doi": "https://doi.org/10.1/x", "title": "Fresh"},
                ]
            })
            return resp

        monkeypatch.setattr(openalex, "_throttled_get", fake_throttled)
        try:
            out = await openalex.get_works_batch(["10.1/x"], force_refresh=True)
            assert out["10.1/x"]["id"] == "new"
        finally:
            cache.invalidate("openalex", "works", "10.1/x")

    @pytest.mark.asyncio
    async def test_dedupes_input(self, monkeypatch):
        """Repeated DOIs in the input collapse to one fetch."""
        urls_called = []

        async def fake_throttled(url, **kwargs):
            urls_called.append(kwargs["params"]["filter"])
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={
                "results": [
                    {"id": "W", "doi": "https://doi.org/10.1/x", "title": "X"},
                ]
            })
            return resp

        monkeypatch.setattr(openalex, "_throttled_get", fake_throttled)
        cache.invalidate("openalex", "works", "10.1/x")
        try:
            out = await openalex.get_works_batch(["10.1/x", "10.1/x", "10.1/X"])
            assert out["10.1/x"]["title"] == "X"
            # All three inputs canonicalise to one DOI; only one fetch
            assert len(urls_called) == 1
            assert urls_called[0].count("|") == 0  # one DOI in the filter
        finally:
            cache.invalidate("openalex", "works", "10.1/x")


# ---------------------------------------------------------------------------
# server.get_papers_metadata
# ---------------------------------------------------------------------------


class TestGetPapersMetadataTool:
    @pytest.mark.asyncio
    async def test_mixed_sources_dispatched_correctly(self, monkeypatch):
        """A mix of arxiv ID + DOI dispatches each to the right backend
        and returns one entry per input in order."""
        # Stub the underlying provider getters
        async def fake_arxiv(ident, *, force_refresh=False):
            return {
                "id": f"http://arxiv.org/abs/{ident}",
                "title": "ArXiv Paper",
                "authors": [{"name": "A"}],
                "links": [{"title": "pdf", "href": f"http://arxiv.org/pdf/{ident}"}],
                "published": "2023-01-01T00:00:00Z",
            }

        async def fake_batch(dois, *, force_refresh=False):
            return {
                openalex._canonical_doi(d): {
                    "doi": f"https://doi.org/{openalex._canonical_doi(d)}",
                    "title": f"OA {d}",
                    "primary_location": {"source": {"display_name": "Journal"}},
                    "open_access": {"is_oa": True, "oa_status": "gold"},
                }
                for d in dois
            }

        monkeypatch.setattr(server.arxiv, "get_paper", fake_arxiv)
        monkeypatch.setattr(server.openalex, "get_works_batch", fake_batch)

        result = await server.get_papers_metadata(
            identifiers=["2301.00001", "10.1234/foo", "10.5678/bar"]
        )

        assert result["count"] == 3
        papers_out = result["papers"]
        assert len(papers_out) == 3
        # Order preserved
        assert papers_out[0]["_input"] == "2301.00001"
        assert papers_out[0]["_source"] == "arxiv"
        assert papers_out[1]["_input"] == "10.1234/foo"
        assert papers_out[1]["_source"] == "openalex"
        assert papers_out[2]["_input"] == "10.5678/bar"
        assert papers_out[2]["_source"] == "openalex"

    @pytest.mark.asyncio
    async def test_unknown_identifier_returns_per_input_error(self):
        result = await server.get_papers_metadata(identifiers=["totally garbage"])
        assert result["count"] == 1
        assert "error" in result["papers"][0]
        assert result["papers"][0]["_input"] == "totally garbage"

    @pytest.mark.asyncio
    async def test_per_paper_failure_isolates(self, monkeypatch):
        """One failing identifier doesn't fail the whole batch."""
        async def fake_arxiv(ident, *, force_refresh=False):
            if ident == "2301.fail":
                return {"error": "No paper found"}
            return {
                "id": f"http://arxiv.org/abs/{ident}",
                "title": "OK",
                "authors": [],
                "links": [],
                "published": "",
            }

        monkeypatch.setattr(server.arxiv, "get_paper", fake_arxiv)
        result = await server.get_papers_metadata(
            identifiers=["2301.0001", "2301.fail", "2301.0002"]
        )
        assert result["count"] == 3
        assert result["papers"][0]["_source"] == "arxiv"
        assert "error" in result["papers"][1]
        assert result["papers"][1]["_input"] == "2301.fail"
        assert result["papers"][2]["_source"] == "arxiv"


# ---------------------------------------------------------------------------
# papers.find_in_markdown + server.find_in_paper
# ---------------------------------------------------------------------------


_FIND_DOC = """\
## Introduction

We use a transformer architecture for token prediction.

## Methods

### Architecture

The transformer has multi-head attention.

### Training

Training uses transformer-friendly batching and self-attention.

## Results

The transformer outperforms baselines on every set.
"""


class TestFindInMarkdown:
    def test_returns_hits_with_section_and_offset(self):
        hits = papers.find_in_markdown(_FIND_DOC, "transformer")
        assert len(hits) >= 4
        assert all("section" in h for h in hits)
        assert all("char_offset" in h for h in hits)
        assert all("snippet" in h for h in hits)
        assert all(h["match"] == "transformer" for h in hits)
        # First hit lands in the Introduction
        assert hits[0]["section"] == "Introduction"

    def test_case_insensitive_default(self):
        hits = papers.find_in_markdown(_FIND_DOC, "TRANSFORMER")
        assert len(hits) >= 4
        # Match preserves the original case of the matched text
        assert hits[0]["match"] == "transformer"

    def test_case_sensitive_filters(self):
        hits = papers.find_in_markdown(
            _FIND_DOC, "TRANSFORMER", case_sensitive=True
        )
        assert hits == []

    def test_whole_words_excludes_partial(self):
        # "use" matches both standalone ("We use a transformer") and as
        # a substring of "uses" ("Training uses transformer-friendly").
        # whole_words must drop the substring hit.
        all_hits = papers.find_in_markdown(_FIND_DOC, "use")
        whole = papers.find_in_markdown(_FIND_DOC, "use", whole_words=True)
        assert len(all_hits) > len(whole) >= 1
        for h in whole:
            assert h["match"] == "use"

    def test_max_results_caps_output(self):
        hits = papers.find_in_markdown(_FIND_DOC, "transformer", max_results=2)
        assert len(hits) == 2

    def test_offsets_align_with_get_section_content(self):
        """The char_offset returned should land at the match in the
        same string get_section_content exposes."""
        hits = papers.find_in_markdown(_FIND_DOC, "multi-head attention")
        assert len(hits) == 1
        h = hits[0]
        section = papers.get_section_content(_FIND_DOC, h["section_index"])
        assert section["content"][h["char_offset"]:h["char_offset"] + len("multi-head attention")] == "multi-head attention"

    def test_no_match_returns_empty(self):
        assert papers.find_in_markdown(_FIND_DOC, "quantum entanglement") == []

    def test_empty_query_returns_empty(self):
        assert papers.find_in_markdown(_FIND_DOC, "") == []


class TestFindInPaperTool:
    @pytest.mark.asyncio
    async def test_unconverted_paper_returns_error(self):
        result = await server.find_in_paper(
            identifier="2301.99999", query="transformer"
        )
        assert "error" in result
        assert "not converted" in result["error"]

    @pytest.mark.asyncio
    async def test_finds_query_in_converted_paper(self, tmp_path, monkeypatch):
        """Place a fake markdown for an arxiv id and verify the tool
        finds occurrences of a query inside it."""
        # Use a real-looking arxiv ID so _resolve_target routes to the
        # arxiv namespace (a manual-namespace label would also work but
        # this is the more interesting code path).
        identifier = "2301.55555"
        canonical = server.arxiv._canonical_arxiv_id(identifier)
        md_path = papers._markdown_path("arxiv", canonical)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(_FIND_DOC)

        try:
            result = await server.find_in_paper(
                identifier=identifier, query="transformer"
            )
            assert "error" not in result, result
            assert result["query"] == "transformer"
            assert result["result_count"] >= 4
            assert result["paper_identifier"] == identifier
            assert all("section" in r for r in result["results"])
        finally:
            md_path.unlink(missing_ok=True)
