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
from academic_tools_mcp.providers import arxiv, biorxiv, crossref, openalex
from academic_tools_mcp.tools import paper


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
            # Shape of a transient error from http.error_dict — no not_found.
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
        result = paper._format_crossref_metadata(_CROSSREF_WORK, "10.1162/tacl_a_99999")
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
        result = paper._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] == 2025
        assert result["publication_date"] == "2025-03-07"

    def test_year_only_date_parts(self):
        work = {"title": ["X"], "issued": {"date-parts": [[2019]]}}
        result = paper._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] == 2019
        assert result["publication_date"] == "2019"

    def test_falls_back_to_published_online_when_issued_missing(self):
        work = {"title": ["X"], "published-online": {"date-parts": [[2022, 11]]}}
        result = paper._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] == 2022
        assert result["publication_date"] == "2022-11"

    def test_missing_dates_are_none(self):
        work = {"title": ["X"]}
        result = paper._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] is None
        assert result["publication_date"] is None

    def test_oa_fields_always_null(self):
        result = paper._format_crossref_metadata(_CROSSREF_WORK, "10.1/x")
        assert result["is_oa"] is None
        assert result["oa_status"] is None
        assert result["oa_url"] is None
        assert result["pdf_url"] is None

    def test_empty_title_list_is_none(self):
        work = {"title": [], "container-title": []}
        result = paper._format_crossref_metadata(work, "10.1/x")
        assert result["title"] is None
        assert result["venue"] is None

    def test_posted_only_date_yields_year(self):
        # Preprint-only Crossref records carry just `posted` (no issued /
        # published-*). The date walk must include it, or a non-arXiv/bioRxiv
        # preprint DOI reached via fallback_crossref returns year=None.
        work = {"title": ["A Preprint"], "posted": {"date-parts": [[2025, 11, 3]]}}
        result = paper._format_crossref_metadata(work, "10.1/x")
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
        result = paper._format_crossref_metadata(work, "10.1/x")
        assert result["publication_year"] == 2024
        assert result["publication_date"] == "2024-06"


class TestFirstHelper:
    """Unit coverage for the shared app.unwrap_first list-unwrap helper."""

    def test_unwraps_list(self):
        from academic_tools_mcp.app import unwrap_first

        assert unwrap_first(["a", "b"]) == "a"

    def test_empty_list_is_none(self):
        from academic_tools_mcp.app import unwrap_first

        assert unwrap_first([]) is None

    def test_passes_through_scalar(self):
        from academic_tools_mcp.app import unwrap_first

        assert unwrap_first("plain") == "plain"
        assert unwrap_first(None) is None


class TestCrossrefDateHelper:
    """Unit coverage for the shared app.crossref_date helper (used by both
    paper.py metadata formatting and search.py year extraction, so the two
    can't drift on whether `posted` counts)."""

    def test_reads_posted(self):
        from academic_tools_mcp.app import crossref_date

        assert crossref_date({"posted": {"date-parts": [[2025, 11, 3]]}}) == (
            2025,
            "2025-11-03",
        )

    def test_issued_wins_over_posted(self):
        from academic_tools_mcp.app import crossref_date

        work = {"issued": {"date-parts": [[2024]]}, "posted": {"date-parts": [[2023]]}}
        assert crossref_date(work) == (2024, "2024")

    def test_guards_malformed_date_parts(self):
        from academic_tools_mcp.app import crossref_date

        assert crossref_date({"issued": {"date-parts": None}}) == (None, None)
        assert crossref_date({"issued": {"date-parts": []}}) == (None, None)
        assert crossref_date({"issued": {"date-parts": [[None]]}}) == (None, None)
        assert crossref_date({}) == (None, None)


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


# A fuller Crossref work: the fields authors / abstract / BibTeX actually read.
_CROSSREF_FULL = {
    "DOI": "10.1162/tacl_a_99999",
    "title": ["A Brand New Paper Not Yet In OpenAlex"],
    "container-title": ["Transactions of the ACL"],
    "type": "journal-article",
    "issued": {"date-parts": [[2026, 5]]},
    "volume": "14",
    "issue": "3",
    "page": "270-273",
    "publisher": "MIT Press",
    "abstract": "<jats:title>Abstract</jats:title><jats:p>We show &amp; prove it.</jats:p>",
    "author": [
        {"given": "Ada", "family": "Lovelace", "sequence": "first"},
        {
            "given": "Ludwig",
            "family": "van Beethoven",
            "sequence": "additional",
            "affiliation": [{"name": "Bonn"}],
        },
        {"name": "The Consortium", "sequence": "additional"},
    ],
}


def _stub_404_then_crossref(monkeypatch, work=None):
    """OpenAlex answers a definitive 404; Crossref answers with *work*."""

    async def fake_openalex(doi, **kwargs):
        return _openalex_404(doi)

    async def fake_crossref(doi, **kwargs):
        return dict(work if work is not None else _CROSSREF_FULL)

    monkeypatch.setattr(openalex, "get_work", fake_openalex)
    monkeypatch.setattr(crossref, "get_work", fake_crossref)


class TestFallbackReachesAllFourTools:
    """The asymmetry this closes: `fallback_crossref` used to be
    get_paper_metadata's alone, so a DOI Crossref had indexed and OpenAlex had
    not gave you a title and venue but no authors, abstract or BibTeX — on a
    server whose stated purpose includes generating BibTeX.
    """

    @pytest.mark.asyncio
    async def test_authors_page_matches_the_openalex_shape(self, monkeypatch):
        """Keys stay symmetric across branches so a paginating agent never
        feature-detects; the Crossref-only nulls are explicit, not omitted."""
        _stub_404_then_crossref(monkeypatch)

        result = await server.get_paper_authors(
            "10.1162/tacl_a_99999", fallback_crossref=True, page_size=2
        )

        assert result["_source"] == "crossref"
        assert result["_canonical_id"] == "10.1162/tacl_a_99999"
        assert result["author_count"] == 3
        assert result["has_more"] is True
        assert [a["name"] for a in result["authors"]] == ["Ada Lovelace", "Ludwig van Beethoven"]
        assert result["authors"][0]["position"] == "first"
        assert result["authors"][0]["openalex_id"] is None
        assert result["authors"][0]["is_corresponding"] is None
        # Crossref's per-author affiliation is real, unlike arXiv/bioRxiv's.
        assert result["page_institutions"] == ["Bonn"]
        assert result["page_institution_count"] == 1

    @pytest.mark.asyncio
    async def test_authors_second_page_slices_in_memory(self, monkeypatch):
        _stub_404_then_crossref(monkeypatch)

        result = await server.get_paper_authors(
            "10.1162/tacl_a_99999", fallback_crossref=True, page=2, page_size=2
        )

        # An organisation author has `name`, not given/family.
        assert [a["name"] for a in result["authors"]] == ["The Consortium"]
        assert result["has_more"] is False

    @pytest.mark.asyncio
    async def test_abstract_renders_jats_to_plain_text(self, monkeypatch):
        """Crossref deposits markup, so the raw value is not something an agent
        can read — and the `<jats:title>Abstract</jats:title>` labelling the
        field is not part of the abstract."""
        _stub_404_then_crossref(monkeypatch)

        result = await server.get_paper_abstract("10.1162/tacl_a_99999", fallback_crossref=True)

        assert result["_source"] == "crossref"
        assert result["title"] == "A Brand New Paper Not Yet In OpenAlex"
        assert result["abstract"] == "We show & prove it."

    @pytest.mark.asyncio
    async def test_abstract_is_null_when_crossref_has_none(self, monkeypatch):
        """Most Crossref records carry no abstract. That is a real answer with
        the key present, not a missing key an agent has to feature-detect."""
        _stub_404_then_crossref(monkeypatch, work=_CROSSREF_WORK)

        result = await server.get_paper_abstract("10.1162/tacl_a_99999", fallback_crossref=True)

        assert result["_source"] == "crossref"
        assert result["abstract"] is None

    @pytest.mark.asyncio
    async def test_bibtex_uses_crossrefs_own_type_vocabulary(self, monkeypatch):
        """`journal-article` is a Crossref spelling `_TYPE_MAP` does not carry;
        reading it through the OpenAlex map would silently yield @misc."""
        _stub_404_then_crossref(monkeypatch)

        result = await server.get_paper_bibtex("10.1162/tacl_a_99999", fallback_crossref=True)
        entry = result["bibtex"]

        assert result["_source"] == "crossref"
        assert entry.startswith("@article{lovelace2026brand,")
        assert "author={Lovelace, Ada and van Beethoven, Ludwig and {The Consortium}}" in entry
        assert "journal={Transactions of the ACL}" in entry
        assert "volume={14}" in entry
        assert "number={3}" in entry
        # A single Crossref `page` string becomes a BibTeX range.
        assert "pages={270--273}" in entry
        assert "year={2026}" in entry
        assert "publisher={MIT Press}" in entry
        # `_escape_doi` escapes rather than drops, so the DOI stays resolvable.
        assert r"doi={10.1162/tacl\_a\_99999}" in entry

    @pytest.mark.asyncio
    async def test_every_tool_still_errors_without_the_flag(self, monkeypatch):
        """The default must not change: the fallback stays opt-in on all four."""
        _stub_404_then_crossref(monkeypatch)

        for call in (
            server.get_paper_metadata("10.1162/tacl_a_99999"),
            server.get_paper_authors("10.1162/tacl_a_99999"),
            server.get_paper_abstract("10.1162/tacl_a_99999"),
            server.get_paper_bibtex("10.1162/tacl_a_99999"),
        ):
            result = await call
            assert result.get("_source") != "crossref"
            assert "error" in result

    @pytest.mark.asyncio
    async def test_a_transient_openalex_error_never_reaches_crossref(self, monkeypatch):
        """The precondition is a *definitive* 404. A 5xx means OpenAlex may still
        have the paper, so answering from Crossref would silently downgrade it."""

        async def fake_openalex(doi, **kwargs):
            return {"error": "OpenAlex unavailable", "retryable": True}

        async def boom(*args, **kwargs):
            raise AssertionError("a transient OpenAlex error must not trigger the fallback")

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(crossref, "get_work", boom)

        for call in (
            server.get_paper_authors("10.1162/tacl_a_99999", fallback_crossref=True),
            server.get_paper_abstract("10.1162/tacl_a_99999", fallback_crossref=True),
            server.get_paper_bibtex("10.1162/tacl_a_99999", fallback_crossref=True),
        ):
            result = await call
            assert result["retryable"] is True


class TestFallbackFailureIsNotSwallowed:
    """An agent that asked for the fallback and got only OpenAlex's `not_found`
    cannot tell that Crossref was tried and failed transiently — the same
    three-state discipline `published_lookup_retryable` exists for.
    """

    @pytest.mark.asyncio
    async def test_transient_crossref_failure_is_reported(self, monkeypatch):
        async def fake_openalex(doi, **kwargs):
            return _openalex_404(doi)

        async def fake_crossref(doi, **kwargs):
            return {"error": "Crossref unavailable", "retryable": True}

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(crossref, "get_work", fake_crossref)

        for call in (
            server.get_paper_metadata("10.1162/tacl_a_99999", fallback_crossref=True),
            server.get_paper_authors("10.1162/tacl_a_99999", fallback_crossref=True),
            server.get_paper_abstract("10.1162/tacl_a_99999", fallback_crossref=True),
            server.get_paper_bibtex("10.1162/tacl_a_99999", fallback_crossref=True),
        ):
            result = await call
            assert result["not_found"] is True
            assert result["crossref_fallback_retryable"] is True

    @pytest.mark.asyncio
    async def test_a_definitive_crossref_miss_carries_no_retry_hint(self, monkeypatch):
        """Neither index has it. `crossref_fallback_retryable` would send the
        agent back at a call that cannot succeed."""

        async def fake_openalex(doi, **kwargs):
            return _openalex_404(doi)

        async def fake_crossref(doi, **kwargs):
            return {"error": "not in Crossref", "not_found": True}

        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        monkeypatch.setattr(crossref, "get_work", fake_crossref)

        result = await server.get_paper_bibtex("10.1162/tacl_a_99999", fallback_crossref=True)

        assert result["not_found"] is True
        assert "crossref_fallback_retryable" not in result


class TestFallbackIsOpenalexOnly:
    """`fallback_crossref` reaches OpenAlex-routed DOIs and nothing else.

    arXiv and bioRxiv flag their own misses `not_found` too, so a gate built
    from that flag alone claims them: an arXiv id would reach `get_work` as if
    it were a DOI, and a bioRxiv miss would come back tagged `crossref`, since
    Crossref does index `10.1101` DOIs. That is not what the parameter says it
    does, and an agent branching on `_source` would be told the wrong provider.
    """

    @staticmethod
    def _recording_crossref(monkeypatch):
        calls: list[str] = []

        async def fake_get_work(doi, **kwargs):
            calls.append(doi)
            return dict(_CROSSREF_WORK)

        monkeypatch.setattr(crossref, "get_work", fake_get_work)
        return calls

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool",
        ["get_paper_metadata", "get_paper_authors", "get_paper_abstract", "get_paper_bibtex"],
    )
    async def test_an_arxiv_miss_never_reaches_crossref(self, monkeypatch, tool):
        """An arXiv id is not a DOI — consulting Crossref spends a request on nothing."""

        async def fake_arxiv(arxiv_id, **kwargs):
            return {"error": f"No paper found for arXiv ID: {arxiv_id}", "not_found": True}

        monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)
        calls = self._recording_crossref(monkeypatch)

        result = await getattr(server, tool)("2301.99999", fallback_crossref=True)

        assert calls == []
        assert result["not_found"] is True
        assert "crossref_fallback_retryable" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool",
        ["get_paper_metadata", "get_paper_authors", "get_paper_abstract", "get_paper_bibtex"],
    )
    async def test_a_biorxiv_miss_is_not_answered_by_crossref(self, monkeypatch, tool):
        """Crossref indexes 10.1101 DOIs, so this one would silently succeed."""

        async def fake_biorxiv(doi, **kwargs):
            return {"error": f"No paper found for DOI: {doi}", "not_found": True}

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)
        calls = self._recording_crossref(monkeypatch)

        result = await getattr(server, tool)("10.1101/2026.01.01.999999", fallback_crossref=True)

        assert calls == []
        assert result.get("_source") != "crossref"
        assert result["not_found"] is True
