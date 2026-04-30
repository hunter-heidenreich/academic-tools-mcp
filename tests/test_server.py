"""Tests for tool-layer behaviors that compose multiple modules.

The provider-level tests cover the underlying clients; this file covers
the smartness wired up at the @mcp.tool layer in server.py — chaining
across providers and the auto-source picker.
"""

import asyncio

import pytest

from academic_tools_mcp import biorxiv, crossref, openalex, opencitations, server


# ---------------------------------------------------------------------------
# get_paper_metadata: follow_published auto-chain to OpenAlex
# ---------------------------------------------------------------------------


class TestFollowPublished:
    """When follow_published=True and a bioRxiv preprint has been
    formally published, get_paper_metadata should automatically chain
    to OpenAlex for the journal version. Without follow_published the
    bioRxiv record is returned unchanged.
    """

    @pytest.mark.asyncio
    async def test_default_returns_biorxiv_record(self, monkeypatch):
        async def fake_biorxiv_get_paper(doi):
            return {
                "doi": "10.1101/2024.01.01.123",
                "title": "Preprint title",
                "date": "2024-01-01",
                "version": "1",
                "type": "new results",
                "category": "neuroscience",
                "license": "cc_by",
                "server": "biorxiv",
                "published_doi": "10.1038/s41586-024-07000-0",
                "pdf_url": "https://www.biorxiv.org/content/10.1101/2024.01.01.123v1.full.pdf",
            }

        async def _no_openalex(doi):
            raise AssertionError(
                "OpenAlex must NOT be called when follow_published is False"
            )

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv_get_paper)
        monkeypatch.setattr(openalex, "get_work", _no_openalex)

        result = await server.get_paper_metadata("10.1101/2024.01.01.123")
        assert result["_source"] == "biorxiv"
        assert result["title"] == "Preprint title"
        assert result["published_doi"] == "10.1038/s41586-024-07000-0"

    @pytest.mark.asyncio
    async def test_follow_published_returns_openalex_journal_record(
        self, monkeypatch
    ):
        async def fake_biorxiv_get_paper(doi):
            return {
                "doi": "10.1101/2024.01.01.123",
                "title": "Preprint title",
                "published_doi": "10.1038/s41586-024-07000-0",
                "pdf_url": "https://example/pdf",
            }

        async def fake_openalex_get_work(doi):
            assert doi == "10.1038/s41586-024-07000-0", (
                "follow_published must call OpenAlex with the published_doi, "
                "not the preprint DOI"
            )
            return {
                "title": "Journal version title",
                "doi": doi,
                "publication_year": 2024,
                "publication_date": "2024-03-15",
                "type": "article",
                "language": "en",
                "primary_location": {"source": {"display_name": "Nature"}},
                "open_access": {"is_oa": True, "oa_status": "hybrid", "oa_url": "https://nature.com/x"},
            }

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv_get_paper)
        monkeypatch.setattr(openalex, "get_work", fake_openalex_get_work)

        result = await server.get_paper_metadata(
            "10.1101/2024.01.01.123", follow_published=True
        )
        assert result["_source"] == "openalex_via_biorxiv"
        assert result["title"] == "Journal version title"
        assert result["doi"] == "10.1038/s41586-024-07000-0"
        assert result["venue"] == "Nature"
        assert result["is_oa"] is True
        # The chain must remain visible — agents that want to know
        # the original preprint can find it here.
        assert result["preprint_doi"] == "10.1101/2024.01.01.123"

    @pytest.mark.asyncio
    async def test_follow_published_no_published_doi_returns_preprint(
        self, monkeypatch
    ):
        # An unpublished preprint: follow_published=True must still
        # return the bioRxiv record (no journal version exists). The
        # parameter is opt-in convenience, not "I refuse to return a
        # preprint".
        async def fake_biorxiv_get_paper(doi):
            return {
                "doi": "10.1101/unpub",
                "title": "Still preprint",
                "published_doi": None,
            }

        async def _no_openalex(doi):
            raise AssertionError(
                "OpenAlex must NOT be called when there is no published_doi"
            )

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv_get_paper)
        monkeypatch.setattr(openalex, "get_work", _no_openalex)

        result = await server.get_paper_metadata(
            "10.1101/unpub", follow_published=True
        )
        assert result["_source"] == "biorxiv"
        assert result["title"] == "Still preprint"

    @pytest.mark.asyncio
    async def test_follow_published_falls_back_when_openalex_misses(
        self, monkeypatch
    ):
        # Journal version exists but isn't in OpenAlex yet (paper too
        # new to index, etc.). We must fall back to the preprint record
        # so the agent gets *something* — silently failing or erroring
        # would surprise the agent and force a retry path.
        async def fake_biorxiv_get_paper(doi):
            return {
                "doi": "10.1101/2024.fresh",
                "title": "Fresh preprint",
                "published_doi": "10.1038/not-yet-indexed",
                "pdf_url": "https://example/pdf",
            }

        async def fake_openalex_get_work(doi):
            return {"error": "No work found for DOI: 10.1038/not-yet-indexed"}

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv_get_paper)
        monkeypatch.setattr(openalex, "get_work", fake_openalex_get_work)

        result = await server.get_paper_metadata(
            "10.1101/2024.fresh", follow_published=True
        )
        assert result["_source"] == "biorxiv"
        assert result["title"] == "Fresh preprint"
        assert result["published_doi"] == "10.1038/not-yet-indexed"


# ---------------------------------------------------------------------------
# get_paper_references: source="auto" picks the bigger provider
# ---------------------------------------------------------------------------


class TestReferencesAutoSource:
    """source='auto' fires Crossref and OpenCitations in parallel and
    pages from whichever has more references — saves a turn vs. calling
    get_paper_references_count first.
    """

    @pytest.mark.asyncio
    async def test_auto_picks_bigger_source(self, monkeypatch):
        async def fake_cr(doi):
            return {"reference": [{"DOI": "10.1/a"}, {"DOI": "10.1/b"}]}

        async def fake_oc(doi):
            return {
                "references": [
                    {"doi": "10.2/a"},
                    {"doi": "10.2/b"},
                    {"doi": "10.2/c"},
                    {"doi": "10.2/d"},
                ],
                "count": 4,
            }

        monkeypatch.setattr(server, "_fetch_crossref_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.x/x", source="auto")
        assert result["_source"] == "opencitations"
        assert result["total"] == 4

    @pytest.mark.asyncio
    async def test_auto_tie_goes_to_crossref(self, monkeypatch):
        # Tie-break to Crossref because its per-entry shape has richer
        # bibliographic metadata (author, title, year), while
        # OpenCitations is just DOI links. If counts are equal the agent
        # gets more useful per-row info from Crossref.
        async def fake_cr(doi):
            return {"reference": [{"DOI": "10.1/a"}, {"DOI": "10.1/b"}]}

        async def fake_oc(doi):
            return {
                "references": [{"doi": "10.2/a"}, {"doi": "10.2/b"}],
                "count": 2,
            }

        monkeypatch.setattr(server, "_fetch_crossref_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.x/x", source="auto")
        assert result["_source"] == "crossref"

    @pytest.mark.asyncio
    async def test_auto_falls_back_when_one_source_errors(self, monkeypatch):
        # Crossref errors (e.g. no record), OpenCitations succeeds —
        # auto must serve from OpenCitations, not propagate the Crossref
        # error.
        async def fake_cr(doi):
            return {"error": "No work found on Crossref for DOI: 10.x/x"}

        async def fake_oc(doi):
            return {"references": [{"doi": "10.2/a"}], "count": 1}

        monkeypatch.setattr(server, "_fetch_crossref_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.x/x", source="auto")
        assert result["_source"] == "opencitations"
        assert result["total"] == 1

    @pytest.mark.asyncio
    async def test_auto_returns_combined_error_when_both_sources_fail(
        self, monkeypatch
    ):
        async def fake_cr(doi):
            return {"error": "Crossref says no"}

        async def fake_oc(doi):
            return {"error": "OpenCitations says no"}

        monkeypatch.setattr(server, "_fetch_crossref_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.x/x", source="auto")
        assert "error" in result
        assert result["sources"]["crossref"]["error"] == "Crossref says no"
        assert result["sources"]["opencitations"]["error"] == "OpenCitations says no"

    @pytest.mark.asyncio
    async def test_explicit_source_skips_survey(self, monkeypatch):
        # When the agent commits to a source, only that one runs.
        # Important — paginating page=2..N must not re-survey.
        cr_called = False

        async def fake_oc(doi):
            return {"references": [{"doi": f"10.2/{i}"} for i in range(50)], "count": 50}

        async def fake_cr(doi):
            nonlocal cr_called
            cr_called = True
            return {"reference": []}

        monkeypatch.setattr(server, "_fetch_crossref_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references(
            "10.x/x", source="opencitations", page=2, page_size=10
        )
        assert result["_source"] == "opencitations"
        assert result["page"] == 2
        assert cr_called is False, (
            "explicit source must not trigger the survey of the other source"
        )
