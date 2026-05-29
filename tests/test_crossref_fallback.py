"""Tests for get_paper_metadata's opt-in Crossref fallback on OpenAlex 404.

When OpenAlex returns a *definitive* 404 for a DOI (tagged ``not_found:
True``), ``get_paper_metadata(doi, fallback_crossref=True)`` falls back to
Crossref, whose indexing of new/niche DOIs is often ahead of OpenAlex. The
fallback must fire ONLY on a true 404 — never on a transient OpenAlex error
— and is off by default. Crossref carries no open-access info, so those
fields are null on the fallback response.
"""

import pytest

from academic_tools_mcp import openalex, server


# A canonical OpenAlex 404 error dict, as get_work now produces it.
def _openalex_404(doi):
    return {"error": f"No work found for DOI: {doi}", "not_found": True}


# A representative raw Crossref work object (the API's `message`).
_CROSSREF_WORK = {
    "DOI": "10.1162/tacl_a_99999",
    "title": ["A Brand New Paper Not Yet In OpenAlex"],
    "container-title": ["Transactions of the ACL"],
    "type": "journal-article",
    "language": "en",
    "issued": {"date-parts": [[2026, 5]]},
}


class TestCrossrefFallbackOn404:
    @pytest.mark.asyncio
    async def test_falls_back_to_crossref_on_404(self, monkeypatch):
        """OpenAlex 404 + fallback_crossref=True → Crossref-sourced record."""
        async def fake_openalex(doi, **kwargs):
            return _openalex_404(doi)

        async def fake_crossref(doi):
            return dict(_CROSSREF_WORK)

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(server, "_fetch_crossref_work", fake_crossref)

        result = await server.get_paper_metadata(
            "10.1162/tacl_a_99999", fallback_crossref=True
        )
        assert result["_source"] == "crossref"
        assert result["title"] == "A Brand New Paper Not Yet In OpenAlex"
        assert result["venue"] == "Transactions of the ACL"
        assert result["publication_year"] == 2026
        assert result["publication_date"] == "2026-05"
        assert result["type"] == "journal-article"
        # Crossref carries no OA info.
        assert result["is_oa"] is None
        assert result["oa_url"] is None
        assert result["pdf_url"] is None
        # Canonical DOI is the lowercased bare form.
        assert result["_canonical_id"] == "10.1162/tacl_a_99999"

    @pytest.mark.asyncio
    async def test_default_off_does_not_call_crossref(self, monkeypatch):
        """Default (fallback_crossref=False) returns the OpenAlex error and
        never touches Crossref."""
        called = False

        async def fake_openalex(doi, **kwargs):
            return _openalex_404(doi)

        async def fake_crossref(doi):
            nonlocal called
            called = True
            return dict(_CROSSREF_WORK)

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(server, "_fetch_crossref_work", fake_crossref)

        result = await server.get_paper_metadata("10.1162/tacl_a_99999")
        assert "error" in result
        assert result.get("_source") != "crossref"
        assert called is False, "Crossref must not be called when fallback is off"
        # The error is still enriched with the OpenAlex hint.
        assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_transient_error_does_not_fall_back(self, monkeypatch):
        """A transient OpenAlex error has no not_found marker, so even with
        fallback_crossref=True we surface the OpenAlex error rather than
        masking a retryable failure with a Crossref lookup."""
        called = False

        async def fake_openalex(doi, **kwargs):
            # Shape of a transient error from _http.error_dict — no not_found.
            return {"error": "OpenAlex server error (HTTP 503). Transient — retry."}

        async def fake_crossref(doi):
            nonlocal called
            called = True
            return dict(_CROSSREF_WORK)

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(server, "_fetch_crossref_work", fake_crossref)

        result = await server.get_paper_metadata(
            "10.1162/tacl_a_99999", fallback_crossref=True
        )
        assert "error" in result
        assert "Transient" in result["error"]
        assert called is False, "transient errors must NOT trigger the fallback"

    @pytest.mark.asyncio
    async def test_crossref_also_misses_returns_openalex_error(self, monkeypatch):
        """OpenAlex 404 + Crossref also errors → fall through to the
        OpenAlex error (not the Crossref one)."""
        async def fake_openalex(doi, **kwargs):
            return _openalex_404(doi)

        async def fake_crossref(doi):
            return {"error": f"No work found on Crossref for DOI: {doi}"}

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(server, "_fetch_crossref_work", fake_crossref)

        result = await server.get_paper_metadata(
            "10.1162/tacl_a_99999", fallback_crossref=True
        )
        assert "error" in result
        assert result.get("_source") != "crossref"
        assert "No work found for DOI" in result["error"]


class TestFormatCrossrefMetadata:
    """Field-mapping unit tests for the Crossref → unified-shape formatter."""

    def test_maps_list_fields_and_date(self):
        result = server._format_crossref_metadata(_CROSSREF_WORK, "10.1162/tacl_a_99999")
        assert result["_source"] == "crossref"
        assert result["_canonical_id"] == "10.1162/tacl_a_99999"
        assert result["title"] == "A Brand New Paper Not Yet In OpenAlex"
        assert result["venue"] == "Transactions of the ACL"
        assert result["doi"] == "10.1162/tacl_a_99999"
        assert result["publication_year"] == 2026
        assert result["publication_date"] == "2026-05"
        assert result["language"] == "en"

    def test_full_date_parts_yield_iso_day(self):
        work = {"title": ["X"], "issued": {"date-parts": [[2025, 3, 7]]}}
        result = server._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] == 2025
        assert result["publication_date"] == "2025-03-07"

    def test_year_only_date_parts(self):
        work = {"title": ["X"], "issued": {"date-parts": [[2019]]}}
        result = server._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] == 2019
        assert result["publication_date"] == "2019"

    def test_falls_back_to_published_online_when_issued_missing(self):
        work = {"title": ["X"], "published-online": {"date-parts": [[2022, 11]]}}
        result = server._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] == 2022
        assert result["publication_date"] == "2022-11"

    def test_missing_dates_are_none(self):
        work = {"title": ["X"]}
        result = server._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] is None
        assert result["publication_date"] is None

    def test_oa_fields_always_null(self):
        result = server._format_crossref_metadata(_CROSSREF_WORK, "10.1/x")
        assert result["is_oa"] is None
        assert result["oa_status"] is None
        assert result["oa_url"] is None
        assert result["pdf_url"] is None

    def test_empty_title_list_is_none(self):
        work = {"title": [], "container-title": []}
        result = server._format_crossref_metadata(work, "10.1/x")
        assert result["title"] is None
        assert result["venue"] is None
