"""Tests for the automatic OpenAlex fallback when bioRxiv fails transiently.

bioRxiv's ``/details`` endpoint has served ``200`` with an empty body for every
query while the rest of its API answered; dispatch-by-shape then left every
``10.1101`` DOI unreachable though OpenAlex indexes them. An *upstream*-transient
bioRxiv failure therefore answers from OpenAlex in all four paper tools and the
batch — never a definitive miss, and never a local refusal (backpressure, quota).
"""

import pytest

from academic_tools_mcp import server
from academic_tools_mcp.providers import biorxiv, openalex
from academic_tools_mcp.tools import paper

_DOI = "10.1101/2020.03.09.983247"

_PARSE_ERROR = {"error": "bioRxiv returned a response that could not be parsed.", "retryable": True}

_WORK = {
    "id": "https://openalex.org/W3010960204",
    "doi": f"https://doi.org/{_DOI}",
    "title": "Inhibition of SARS-CoV-2 infection",
    "publication_year": 2020,
    "publication_date": "2020-03-12",
    "type": "preprint",
    "authorships": [
        {
            "author": {"id": "https://openalex.org/A1", "display_name": "Shuai Xia"},
            "author_position": "first",
            "institutions": [{"display_name": "Fudan University"}],
        }
    ],
    "abstract_inverted_index": {"Fusion": [0], "inhibitor": [1]},
}


def _serve(monkeypatch, biorxiv_result, openalex_result):
    """Stub both providers; returns the list of DOIs OpenAlex was asked for."""
    asked: list[str] = []

    async def fake_biorxiv(doi, **kwargs):
        return dict(biorxiv_result)

    async def fake_openalex(doi, **kwargs):
        asked.append(doi)
        return dict(openalex_result)

    monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)
    monkeypatch.setattr(openalex, "get_work", fake_openalex)
    return asked


class TestFallsBackOnUpstreamTransient:
    @pytest.mark.asyncio
    async def test_metadata(self, monkeypatch):
        asked = _serve(monkeypatch, _PARSE_ERROR, _WORK)

        result = await server.get_paper_metadata(_DOI)

        assert asked == [_DOI]
        assert result["_source"] == "openalex"
        assert result["_canonical_id"] == _DOI
        assert result["title"] == _WORK["title"]
        assert result["biorxiv_unavailable"] is True

    @pytest.mark.asyncio
    async def test_authors(self, monkeypatch):
        _serve(monkeypatch, _PARSE_ERROR, _WORK)

        result = await server.get_paper_authors(_DOI)

        assert result["_source"] == "openalex"
        assert result["authors"][0]["name"] == "Shuai Xia"
        assert result["page_institutions"] == ["Fudan University"]
        assert result["biorxiv_unavailable"] is True

    @pytest.mark.asyncio
    async def test_abstract(self, monkeypatch):
        _serve(monkeypatch, _PARSE_ERROR, _WORK)

        result = await server.get_paper_abstract(_DOI)

        assert result["_source"] == "openalex"
        assert result["abstract"] == "Fusion inhibitor"
        assert result["biorxiv_unavailable"] is True

    @pytest.mark.asyncio
    async def test_bibtex(self, monkeypatch):
        _serve(monkeypatch, _PARSE_ERROR, _WORK)

        result = await server.get_paper_bibtex(_DOI)

        assert result["_source"] == "openalex"
        assert "Inhibition of SARS-CoV-2 infection" in result["bibtex"]
        assert result["biorxiv_unavailable"] is True

    @pytest.mark.asyncio
    async def test_batch(self, monkeypatch):
        _serve(monkeypatch, _PARSE_ERROR, _WORK)

        result = await server.get_papers_metadata([_DOI])

        entry = result["papers"][0]
        assert entry["_input"] == _DOI
        assert entry["_source"] == "openalex"
        assert entry["biorxiv_unavailable"] is True

    @pytest.mark.asyncio
    async def test_follow_published_does_not_chain_from_a_fallback(self, monkeypatch):
        asked = _serve(monkeypatch, _PARSE_ERROR, _WORK)

        result = await server.get_paper_metadata(_DOI, follow_published=True)

        assert asked == [_DOI], "only the fallback lookup; no journal chain"
        assert result["_source"] == "openalex"
        assert "followed_published" not in result

    @pytest.mark.asyncio
    async def test_a_served_biorxiv_record_carries_no_flag(self, monkeypatch):
        asked = _serve(
            monkeypatch,
            {"doi": _DOI, "title": "t", "authors": [], "server": "biorxiv"},
            _WORK,
        )

        result = await server.get_paper_metadata(_DOI)

        assert asked == []
        assert result["_source"] == "biorxiv"
        assert "biorxiv_unavailable" not in result


class TestDoesNotFallBack:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(
                {"error": f"No paper found for DOI: {_DOI}", "not_found": True}, id="not_found"
            ),
            pytest.param(
                {"error": "Local backpressure", "retryable": True, "backpressure": True},
                id="backpressure",
            ),
            pytest.param(
                {"error": "quota spent", "retryable": True, "quota_exhausted": True},
                id="quota",
            ),
            pytest.param({"error": "bioRxiv HTTP 403: denied"}, id="unflagged"),
        ],
    )
    async def test_non_upstream_transient_errors_stay_biorxiv_errors(self, monkeypatch, error):
        asked = _serve(monkeypatch, error, _WORK)

        result = await server.get_paper_metadata(_DOI)

        assert asked == []
        assert result["error"] == error["error"]
        assert "suggestion" in result
        assert "openalex_fallback_retryable" not in result


class TestOpenAlexAlsoFails:
    @pytest.mark.asyncio
    async def test_a_transient_openalex_failure_is_flagged(self, monkeypatch):
        _serve(monkeypatch, _PARSE_ERROR, {"error": "OpenAlex HTTP 503", "retryable": True})

        result = await server.get_paper_bibtex(_DOI)

        assert result["error"] == _PARSE_ERROR["error"]
        assert result["retryable"] is True
        assert result["openalex_fallback_retryable"] is True

    @pytest.mark.asyncio
    async def test_a_definitive_openalex_miss_returns_the_biorxiv_error_unflagged(
        self, monkeypatch
    ):
        _serve(monkeypatch, _PARSE_ERROR, {"error": "No work found", "not_found": True})

        result = await server.get_paper_abstract(_DOI)

        assert result["error"] == _PARSE_ERROR["error"]
        assert "openalex_fallback_retryable" not in result

    @pytest.mark.asyncio
    async def test_fetch_source_keeps_the_biorxiv_source_on_a_double_failure(self, monkeypatch):
        _serve(monkeypatch, _PARSE_ERROR, {"error": "No work found", "not_found": True})

        source, canonical, obj = await paper._fetch_source(_DOI)

        assert source == "biorxiv"
        assert canonical == _DOI
        assert "suggestion" not in obj  # still un-enriched
