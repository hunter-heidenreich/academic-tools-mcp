"""Tests for get_paper_metadata's opt-in Crossref fallback on OpenAlex 404.

When OpenAlex returns a *definitive* 404 for a DOI (tagged ``not_found:
True``), ``get_paper_metadata(doi, fallback_crossref=True)`` falls back to
Crossref, whose indexing of new/niche DOIs is often ahead of OpenAlex. The
fallback must fire ONLY on a true 404 — never on a transient OpenAlex error
— and is off by default. Crossref carries no open-access info, so those
fields are null on the fallback response.
"""

import pytest

from academic_tools_mcp import server
from academic_tools_mcp.providers import crossref, openalex


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

        async def fake_crossref(doi, **kwargs):
            return dict(_CROSSREF_WORK)

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(crossref, "get_work", fake_crossref)

        result = await server.get_paper_metadata("10.1162/tacl_a_99999", fallback_crossref=True)
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

        async def fake_crossref(doi, **kwargs):
            nonlocal called
            called = True
            return dict(_CROSSREF_WORK)

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(crossref, "get_work", fake_crossref)

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

        async def fake_crossref(doi, **kwargs):
            nonlocal called
            called = True
            return dict(_CROSSREF_WORK)

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(crossref, "get_work", fake_crossref)

        result = await server.get_paper_metadata("10.1162/tacl_a_99999", fallback_crossref=True)
        assert "error" in result
        assert "Transient" in result["error"]
        assert called is False, "transient errors must NOT trigger the fallback"

    @pytest.mark.asyncio
    async def test_crossref_also_misses_returns_openalex_error(self, monkeypatch):
        """OpenAlex 404 + Crossref also errors → fall through to the
        OpenAlex error (not the Crossref one)."""

        async def fake_openalex(doi, **kwargs):
            return _openalex_404(doi)

        async def fake_crossref(doi, **kwargs):
            return {"error": f"No work found on Crossref for DOI: {doi}"}

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(crossref, "get_work", fake_crossref)

        result = await server.get_paper_metadata("10.1162/tacl_a_99999", fallback_crossref=True)
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

    def test_posted_only_date_yields_year(self):
        # Preprint-only Crossref records carry just `posted` (no issued /
        # published-*). The date walk must include it, or a non-arXiv/bioRxiv
        # preprint DOI reached via fallback_crossref returns year=None.
        work = {"title": ["A Preprint"], "posted": {"date-parts": [[2025, 11, 3]]}}
        result = server._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] == 2025
        assert result["publication_date"] == "2025-11-03"

    def test_issued_preferred_over_posted(self):
        # When both a formal `issued` date and a preprint `posted` date are
        # present, the canonical (issued) date wins.
        work = {
            "title": ["X"],
            "issued": {"date-parts": [[2024, 6]]},
            "posted": {"date-parts": [[2023, 1]]},
        }
        result = server._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] == 2024
        assert result["publication_date"] == "2024-06"


class TestFirstHelper:
    """Unit coverage for the shared _app._first list-unwrap helper."""

    def test_unwraps_list(self):
        from academic_tools_mcp._app import _first

        assert _first(["a", "b"]) == "a"

    def test_empty_list_is_none(self):
        from academic_tools_mcp._app import _first

        assert _first([]) is None

    def test_passes_through_scalar(self):
        from academic_tools_mcp._app import _first

        assert _first("plain") == "plain"
        assert _first(None) is None


class TestCrossrefDateHelper:
    """Unit coverage for the shared _app._crossref_date helper (used by both
    paper.py metadata formatting and search.py year extraction, so the two
    can't drift on whether `posted` counts)."""

    def test_reads_posted(self):
        from academic_tools_mcp._app import _crossref_date

        assert _crossref_date({"posted": {"date-parts": [[2025, 11, 3]]}}) == (
            2025,
            "2025-11-03",
        )

    def test_issued_wins_over_posted(self):
        from academic_tools_mcp._app import _crossref_date

        work = {"issued": {"date-parts": [[2024]]}, "posted": {"date-parts": [[2023]]}}
        assert _crossref_date(work) == (2024, "2024")

    def test_guards_malformed_date_parts(self):
        from academic_tools_mcp._app import _crossref_date

        assert _crossref_date({"issued": {"date-parts": None}}) == (None, None)
        assert _crossref_date({"issued": {"date-parts": []}}) == (None, None)
        assert _crossref_date({"issued": {"date-parts": [[None]]}}) == (None, None)
        assert _crossref_date({}) == (None, None)


class TestFallbackHonoursForceRefresh:
    """``fallback_crossref`` exists for brand-new DOIs — exactly where a stale
    cached Crossref record is most likely and least useful. The call site
    silently dropped ``force_refresh`` even though ``crossref.get_work``
    has always accepted and forwarded it (the graph tools pass it).
    """

    @pytest.mark.asyncio
    async def test_force_refresh_reaches_crossref(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_openalex(doi, **kwargs):
            return {"error": "No work found", "not_found": True}

        async def fake_crossref(doi, **kwargs):
            seen["force_refresh"] = kwargs.get("force_refresh")
            return {"DOI": doi, "title": ["T"], "type": "journal-article"}

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(crossref, "get_work", fake_crossref)

        await server.get_paper_metadata(
            "10.1234/brand-new", force_refresh=True, fallback_crossref=True
        )

        assert seen["force_refresh"] is True

    @pytest.mark.asyncio
    async def test_default_does_not_force_refresh(self, monkeypatch):
        seen: dict[str, object] = {}

        async def fake_openalex(doi, **kwargs):
            return {"error": "No work found", "not_found": True}

        async def fake_crossref(doi, **kwargs):
            seen["force_refresh"] = kwargs.get("force_refresh")
            return {"DOI": doi, "title": ["T"], "type": "journal-article"}

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(crossref, "get_work", fake_crossref)

        await server.get_paper_metadata("10.1234/x", fallback_crossref=True)

        assert seen["force_refresh"] is False
