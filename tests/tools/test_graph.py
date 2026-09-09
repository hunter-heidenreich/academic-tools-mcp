"""Tests for the reference/citation graph tools in ``tools/graph.py``.

Covers the auto-source survey and its hysteresis, pagination, the
Crossref row formatters, and the partial-failure envelope.
"""

import pytest

from academic_tools_mcp import server
from academic_tools_mcp.providers import crossref, openalex, opencitations

# ---------------------------------------------------------------------------
# get_paper_references: source="auto" picks the bigger provider
# ---------------------------------------------------------------------------


class TestReferencesAutoSource:
    """source='auto' fires Crossref and OpenCitations in parallel and pages
    from the better-covered source — saves a turn vs. calling
    get_paper_references_count first. Selection is biased toward Crossref by
    `_CROSSREF_HYSTERESIS`: OpenCitations wins only on a material margin, not
    on a row or two. The exact edge is pinned in TestHysteresisBoundary.
    """

    @pytest.mark.asyncio
    async def test_auto_picks_bigger_source(self, monkeypatch):
        async def fake_cr(doi, **kwargs):
            return {"reference": [{"DOI": "10.1/a"}, {"DOI": "10.1/b"}]}

        async def fake_oc(doi, **kwargs):
            return {
                "references": [
                    {"doi": "10.2/a"},
                    {"doi": "10.2/b"},
                    {"doi": "10.2/c"},
                    {"doi": "10.2/d"},
                ],
                "count": 4,
            }

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="auto")
        assert result["_source"] == "opencitations"
        assert result["total"] == 4

    @pytest.mark.asyncio
    async def test_auto_tie_goes_to_crossref(self, monkeypatch):
        # Tie-break to Crossref because its per-entry shape has richer
        # bibliographic metadata (author, title, year), while
        # OpenCitations is just DOI links. If counts are equal the agent
        # gets more useful per-row info from Crossref.
        async def fake_cr(doi, **kwargs):
            return {"reference": [{"DOI": "10.1/a"}, {"DOI": "10.1/b"}]}

        async def fake_oc(doi, **kwargs):
            return {
                "references": [{"doi": "10.2/a"}, {"doi": "10.2/b"}],
                "count": 2,
            }

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="auto")
        assert result["_source"] == "crossref"

    @pytest.mark.asyncio
    async def test_auto_falls_back_when_one_source_errors(self, monkeypatch):
        # Crossref errors (e.g. no record), OpenCitations succeeds —
        # auto must serve from OpenCitations, not propagate the Crossref
        # error.
        async def fake_cr(doi, **kwargs):
            return {"error": "No work found on Crossref for DOI: 10.1234/x"}

        async def fake_oc(doi, **kwargs):
            return {"references": [{"doi": "10.2/a"}], "count": 1}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="auto")
        assert result["_source"] == "opencitations"
        assert result["total"] == 1

    @pytest.mark.asyncio
    async def test_auto_returns_combined_error_when_both_sources_fail(self, monkeypatch):
        async def fake_cr(doi, **kwargs):
            return {"error": "Crossref says no"}

        async def fake_oc(doi, **kwargs):
            return {"error": "OpenCitations says no"}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="auto")
        assert "error" in result
        assert result["sources"]["crossref"]["error"] == "Crossref says no"
        assert result["sources"]["opencitations"]["error"] == "OpenCitations says no"

    @pytest.mark.asyncio
    async def test_explicit_source_skips_survey(self, monkeypatch):
        # When the agent commits to a source, only that one runs.
        # Important — paginating page=2..N must not re-survey.
        cr_called = False

        async def fake_oc(doi, **kwargs):
            return {"references": [{"doi": f"10.2/{i}"} for i in range(50)], "count": 50}

        async def fake_cr(doi, **kwargs):
            nonlocal cr_called
            cr_called = True
            return {"reference": []}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references(
            "10.1234/x", source="opencitations", page=2, page_size=10
        )
        assert result["_source"] == "opencitations"
        assert result["page"] == 2
        assert cr_called is False, "explicit source must not trigger the survey of the other source"

    @pytest.mark.asyncio
    async def test_an_explicit_source_surfaces_its_error_with_a_suggestion(self, monkeypatch):
        # No survivor to fall back on when the agent pinned the source, so the
        # provider's structured error has to reach the agent intact — and
        # `not_found` must survive, since that is what says "stop retrying".
        async def fake_oc(doi, **kwargs):
            return {"error": "No references found on OpenCitations", "not_found": True}

        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="opencitations")

        assert result["not_found"] is True
        assert "suggestion" in result
        assert "references" not in result

    @pytest.mark.asyncio
    async def test_auto_prefers_crossref_on_near_tie(self, monkeypatch):
        # OpenCitations has one more entry, but Crossref's richer per-row
        # metadata should win the near-tie — OpenCitations only takes over
        # when it has materially more references.
        async def fake_cr(doi, **kwargs):
            return {"reference": [{"DOI": f"10.1/{i}"} for i in range(10)]}

        async def fake_oc(doi, **kwargs):
            return {"references": [{"doi": f"10.2/{i}"} for i in range(11)], "count": 11}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="auto")
        assert result["_source"] == "crossref"

    @pytest.mark.asyncio
    async def test_auto_picks_opencitations_when_materially_more(self, monkeypatch):
        # 15 vs 10 clears the hysteresis margin — the breadth is worth the
        # metadata-poorer payload.
        async def fake_cr(doi, **kwargs):
            return {"reference": [{"DOI": f"10.1/{i}"} for i in range(10)]}

        async def fake_oc(doi, **kwargs):
            return {"references": [{"doi": f"10.2/{i}"} for i in range(15)], "count": 15}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="auto")
        assert result["_source"] == "opencitations"
        assert result["total"] == 15

    @pytest.mark.asyncio
    async def test_auto_empty_winner_surfaces_other_source_failure(self, monkeypatch):
        # Crossref errors transiently; OpenCitations succeeds but with zero
        # references. The empty OpenCitations page must NOT read as a
        # confident "no references" — it carries partial_failure so the
        # agent knows Crossref failed and is worth a retry.
        async def fake_cr(doi, **kwargs):
            return {"error": "Transient: Crossref 503", "retryable": True}

        async def fake_oc(doi, **kwargs):
            return {"references": [], "count": 0}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="auto")
        assert result["_source"] == "opencitations"
        assert result["total"] == 0
        assert result["partial_failure"]["source"] == "crossref"
        assert result["partial_failure"]["error"] == "Transient: Crossref 503"
        assert result["partial_failure"]["retryable"] is True

    @pytest.mark.asyncio
    async def test_auto_both_fail_preserves_retryable(self, monkeypatch):
        # The combined-error response must forward the structured retryable
        # flag, not just the message string, so the agent can tell a
        # transient failure from a definitive one.
        async def fake_cr(doi, **kwargs):
            return {"error": "Transient: Crossref 503", "retryable": True}

        async def fake_oc(doi, **kwargs):
            return {"error": "OpenCitations says no"}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="auto")
        assert "error" in result
        assert result["sources"]["crossref"]["error"] == "Transient: Crossref 503"
        assert result["sources"]["crossref"]["retryable"] is True
        assert result["sources"]["opencitations"]["error"] == "OpenCitations says no"

    @pytest.mark.asyncio
    async def test_auto_page_gt_one_requires_pinned_source(self, monkeypatch):
        # Paginating past page 1 with source='auto' is rejected with an
        # actionable error BEFORE any provider is surveyed — re-surveying
        # could pick a different source and shift the offsets mid-walk.
        called = False

        async def fake_cr(doi, **kwargs):
            nonlocal called
            called = True
            return {"reference": []}

        async def fake_oc(doi, **kwargs):
            nonlocal called
            called = True
            return {"references": [], "count": 0}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="auto", page=2)
        assert "error" in result
        assert "_source" in result["suggestion"]
        assert called is False, "page>1 auto must not survey either provider"


class TestGraphToolsRejectNonDois:
    """The graph tools are DOI-only and must say so without a round-trip.

    Crossref and OpenCitations have no other identifier space. Forwarding an
    arXiv ID buys a 404 and then negative-caches a key that could never have
    resolved, so the shape check happens locally, using the same predicate as
    the metadata dispatcher.
    """

    @pytest.fixture(autouse=True)
    def _no_provider_calls(self, monkeypatch):
        async def boom(*args, **kwargs):
            raise AssertionError("a non-DOI must be rejected before any provider call")

        monkeypatch.setattr(crossref, "get_work", boom)
        monkeypatch.setattr(opencitations, "get_references", boom)
        monkeypatch.setattr(opencitations, "get_citations", boom)

    @pytest.mark.parametrize(
        "identifier", ["2301.00001", "hep-th/9901001", "my-paper-2024", "", "10.123/x"]
    )
    @pytest.mark.asyncio
    async def test_every_graph_tool_rejects(self, identifier):
        for call in (
            server.get_paper_references_count(identifier),
            server.get_paper_references(identifier),
            server.get_paper_citations_count(identifier),
            server.get_paper_citations(identifier),
        ):
            result = await call
            assert "error" in result
            assert result["not_found"] is True
            assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_a_doi_url_is_accepted_not_rejected(self, monkeypatch):
        # The guard normalizes first, so every accepted DOI spelling passes it.
        async def fake_oc(doi, **kwargs):
            return {"count": 7}

        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        result = await server.get_paper_citations_count("https://doi.org/10.1234/x")
        assert result["count"] == 7


class TestGraphToolsAcceptPmids:
    """The graph tools *hand out* a ``pmid`` on every OpenCitations row, so they
    must take one back. Before this, a citing row carrying a ``pmid`` and no
    ``doi`` was a dead end: the docstring told the agent to chain the identifier
    into another tool, and no tool accepted it.
    """

    @pytest.fixture(autouse=True)
    def _resolve(self, monkeypatch):
        async def fake_resolve_pmid(pmid, **kwargs):
            return {"doi": "10.1234/x", "openalex_id": "https://openalex.org/W1"}

        monkeypatch.setattr(openalex, "resolve_pmid", fake_resolve_pmid)

    @pytest.mark.parametrize(
        "spelling", ["pmid:20079334", "20079334", "https://pubmed.ncbi.nlm.nih.gov/20079334/"]
    )
    @pytest.mark.asyncio
    async def test_citations_count_resolves_and_echoes_the_doi(self, monkeypatch, spelling):
        seen: list[str] = []

        async def fake_oc(doi, **kwargs):
            seen.append(doi)
            return {"count": 7}

        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        result = await server.get_paper_citations_count(spelling)

        assert result["count"] == 7
        # Canonical, not the spelling passed — the cross-tool echo contract.
        assert result["doi"] == "10.1234/x"
        assert seen == ["10.1234/x"]

    @pytest.mark.asyncio
    async def test_an_unresolvable_pmid_errors_rather_than_reaching_a_provider(self, monkeypatch):
        async def boom(*args, **kwargs):
            raise AssertionError("a PMID that did not resolve must not reach a provider")

        async def fake_resolve_pmid(pmid, **kwargs):
            return {"error": "No work found for PMID: 99999999", "not_found": True}

        monkeypatch.setattr(openalex, "resolve_pmid", fake_resolve_pmid)
        monkeypatch.setattr(opencitations, "get_citations", boom)

        result = await server.get_paper_citations_count("pmid:99999999")

        assert result["not_found"] is True
        assert "suggestion" in result


class TestReferencesCount:
    """`get_paper_references_count` surveys both providers in parallel so an
    agent can compare coverage before committing to one for pagination.

    Its partial-failure branch is the only shape in the graph family that no
    other test exercises end-to-end: `get_paper_references` reports a failed
    source through `partial_failure` on the winning page, while this tool
    reports it in place, inside `sources`.
    """

    @pytest.mark.asyncio
    async def test_reports_both_counts(self, monkeypatch):
        async def fake_cr(doi, **kwargs):
            return {"reference": [{"DOI": "10.1/a"}, {"DOI": "10.1/b"}]}

        async def fake_oc(doi, **kwargs):
            return {"references": [{"doi": "10.2/a"}], "count": 1}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references_count("10.1234/x")

        assert result["doi"] == "10.1234/x"
        assert result["sources"]["crossref"] == {"count": 2}
        assert result["sources"]["opencitations"] == {"count": 1}

    @pytest.mark.asyncio
    async def test_a_work_with_no_reference_list_counts_zero(self, monkeypatch):
        # Crossref omits `reference` entirely when the publisher deposited
        # none — that is a count of 0, not an error.
        async def fake_cr(doi, **kwargs):
            return {"DOI": "10.1234/x"}

        async def fake_oc(doi, **kwargs):
            return {"references": [], "count": 0}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references_count("10.1234/x")

        assert result["sources"]["crossref"] == {"count": 0}
        assert result["sources"]["opencitations"] == {"count": 0}

    @pytest.mark.asyncio
    async def test_one_source_failing_does_not_hide_the_other(self, monkeypatch):
        async def fake_cr(doi, **kwargs):
            return {"error": "Crossref timed out", "retryable": True}

        async def fake_oc(doi, **kwargs):
            return {"references": [{"doi": "10.2/a"}], "count": 1}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references_count("10.1234/x")

        assert result["sources"]["opencitations"] == {"count": 1}
        assert result["sources"]["crossref"]["retryable"] is True
        assert "count" not in result["sources"]["crossref"]

    @pytest.mark.asyncio
    async def test_structured_error_fields_are_forwarded_not_flattened(self, monkeypatch):
        # The survey exists so an agent can choose a source. A bare message
        # can't distinguish "transiently unavailable" from "definitively
        # absent", so the whole structured signal rides along.
        async def fake_cr(doi, **kwargs):
            return {
                "error": "Local backpressure",
                "retryable": True,
                "backpressure": True,
                "max_concurrency": 3,
                "retry_after_seconds": 0.1,
            }

        async def fake_oc(doi, **kwargs):
            return {"error": "No references found", "not_found": True}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references_count("10.1234/x")

        cr = result["sources"]["crossref"]
        assert cr["backpressure"] is True
        assert cr["max_concurrency"] == 3
        assert cr["retry_after_seconds"] == 0.1
        assert result["sources"]["opencitations"]["not_found"] is True

    @pytest.mark.asyncio
    async def test_both_failing_still_reports_per_source(self, monkeypatch):
        # Unlike get_paper_references, this tool has no page to return, so a
        # double failure is not a top-level error — it is two error entries.
        async def fake_cr(doi, **kwargs):
            return {"error": "Crossref down"}

        async def fake_oc(doi, **kwargs):
            return {"error": "OpenCitations down"}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references_count("10.1234/x")

        assert "error" not in result
        assert result["sources"]["crossref"]["error"] == "Crossref down"
        assert result["sources"]["opencitations"]["error"] == "OpenCitations down"

    @pytest.mark.asyncio
    async def test_force_refresh_reaches_both_providers(self, monkeypatch):
        seen: dict[str, bool] = {}

        async def fake_cr(doi, *, force_refresh=False):
            seen["crossref"] = force_refresh
            return {"reference": []}

        async def fake_oc(doi, *, force_refresh=False):
            seen["opencitations"] = force_refresh
            return {"references": [], "count": 0}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        await server.get_paper_references_count("10.1234/x", force_refresh=True)

        assert seen == {"crossref": True, "opencitations": True}


class TestGraphPagination:
    """`_page` is the one home for the slice arithmetic every graph response
    carries, and `has_more` is the field an agent's walk terminates on."""

    @staticmethod
    def _refs(n):
        return {"references": [{"doi": f"10.2/{i}"} for i in range(n)], "count": n}

    @pytest.mark.asyncio
    async def test_consecutive_pages_are_disjoint_and_cover_the_list(self, monkeypatch):
        async def fake_oc(doi, **kwargs):
            return TestGraphPagination._refs(25)

        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        first = await server.get_paper_references(
            "10.1234/x", source="opencitations", page=1, page_size=10
        )
        second = await server.get_paper_references(
            "10.1234/x", source="opencitations", page=2, page_size=10
        )
        third = await server.get_paper_references(
            "10.1234/x", source="opencitations", page=3, page_size=10
        )

        assert first["has_more"] is True
        assert second["has_more"] is True
        assert third["has_more"] is False
        assert len(third["references"]) == 5

        walked = first["references"] + second["references"] + third["references"]
        assert walked == [{"doi": f"10.2/{i}"} for i in range(25)]
        assert all(r["total"] == 25 for r in (first, second, third))

    @pytest.mark.asyncio
    async def test_a_page_past_the_end_is_empty_and_says_so(self, monkeypatch):
        async def fake_oc(doi, **kwargs):
            return TestGraphPagination._refs(3)

        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references(
            "10.1234/x", source="opencitations", page=9, page_size=20
        )

        assert result["references"] == []
        assert result["has_more"] is False
        assert result["total"] == 3, "total still names the full list, so the overshoot is visible"

    @pytest.mark.parametrize("page_size", [1, 50])
    @pytest.mark.asyncio
    async def test_both_page_size_bounds_are_honoured(self, monkeypatch, page_size):
        # Exactly at a cap must pass; the MCP boundary refuses anything past it.
        async def fake_oc(doi, **kwargs):
            return TestGraphPagination._refs(60)

        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references(
            "10.1234/x", source="opencitations", page=1, page_size=page_size
        )

        assert len(result["references"]) == page_size
        assert result["has_more"] is True

    @pytest.mark.asyncio
    async def test_citations_paginate_on_the_same_arithmetic(self, monkeypatch):
        async def fake_oc(doi, **kwargs):
            return {"citations": [{"doi": f"10.3/{i}"} for i in range(7)], "count": 7}

        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        result = await server.get_paper_citations("10.1234/x", page=2, page_size=5)

        assert result["citations"] == [{"doi": "10.3/5"}, {"doi": "10.3/6"}]
        assert result["has_more"] is False
        assert result["total"] == 7


class TestCrossrefReferenceFormatting:
    """The per-entry shape `get_paper_references` promises for `source="crossref"`.

    Publisher deposits vary field by field, so the formatter's job is to emit
    what is there and omit what is not — an absent field must not become a null
    the agent has to filter.
    """

    def test_every_documented_field_is_mapped(self):
        from academic_tools_mcp.tools import graph

        entry = graph._format_crossref_reference(
            {
                "key": "ref12",
                "DOI": "10.1/a",
                "author": "Curie",
                "article-title": "On Radioactivity",
                "year": "1898",
                "journal-title": "Comptes Rendus",
                "volume": "127",
                "first-page": "1215",
                "unstructured": "Curie, M. (1898).",
            }
        )

        assert entry == {
            "doi": "10.1/a",
            "author": "Curie",
            "title": "On Radioactivity",
            "year": "1898",
            "journal": "Comptes Rendus",
            "volume": "127",
            "first_page": "1215",
            "unstructured": "Curie, M. (1898).",
        }

    def test_absent_and_empty_fields_are_omitted_not_nulled(self):
        from academic_tools_mcp.tools import graph

        entry = graph._format_crossref_reference({"DOI": "10.1/a", "year": "", "volume": None})

        assert entry == {"doi": "10.1/a"}

    def test_a_bookkeeping_only_row_falls_back_to_its_key(self):
        # Crossref deposits rows carrying only `key` + `doi-asserted-by`. A bare
        # {} reads to an agent as a formatter that lost the entry; the key says
        # "deposited, no usable metadata".
        from academic_tools_mcp.tools import graph

        assert graph._format_crossref_reference(
            {"key": "ref12", "doi-asserted-by": "publisher"}
        ) == {"key": "ref12"}

    def test_the_key_is_a_last_resort_not_an_extra_field(self):
        from academic_tools_mcp.tools import graph

        assert graph._format_crossref_reference({"key": "ref12", "DOI": "10.1/a"}) == {
            "doi": "10.1/a"
        }


class TestCrossrefReferenceRowsAreTypeChecked:
    """`crossref.get_work` returns the upstream `message` verbatim and only
    guarantees it is a dict. A non-dict reference row reaches
    `_format_crossref_reference` as an `AttributeError` — and, because the body
    was positive-cached, it does so for the whole TTL.
    """

    @pytest.mark.asyncio
    async def test_a_non_dict_row_yields_a_page_not_a_traceback(self, monkeypatch):
        async def fake_cr(doi, **kwargs):
            return {"reference": ["10.1/x", None, 7, {"DOI": "10.1/a"}]}

        monkeypatch.setattr(crossref, "get_work", fake_cr)

        result = await server.get_paper_references("10.1234/x", source="crossref")

        assert result["references"] == [{"doi": "10.1/a"}]

    @pytest.mark.asyncio
    async def test_the_survey_and_the_page_agree_about_the_same_work(self, monkeypatch):
        # Both read the list through `_crossref_refs`, so the count tool can
        # never send an agent to page a source that then raises on it.
        async def fake_cr(doi, **kwargs):
            return {"reference": ["10.1/x", {"DOI": "10.1/a"}]}

        async def fake_oc(doi, **kwargs):
            return {"error": "OpenCitations down"}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        survey = await server.get_paper_references_count("10.1234/x")
        page = await server.get_paper_references("10.1234/x", source="crossref")

        assert survey["sources"]["crossref"]["count"] == page["total"] == 1

    @pytest.mark.asyncio
    async def test_a_non_list_reference_field_is_no_references(self, monkeypatch):
        async def fake_cr(doi, **kwargs):
            return {"reference": "10.1/x"}

        monkeypatch.setattr(crossref, "get_work", fake_cr)

        result = await server.get_paper_references("10.1234/x", source="crossref")

        assert result["total"] == 0
        assert result["references"] == []


class TestGraphEchoesACanonicalDoi:
    """The echoed `doi` is what an agent correlates results by across calls.

    Echoing the raw input gives one paper as many identities as it has
    spellings, all of which share a single cache key — the opposite of the
    `_canonical_id` contract the paper family holds.
    """

    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        async def fake_oc(doi, **kwargs):
            return {"references": [], "citations": [], "count": 0}

        async def fake_cr(doi, **kwargs):
            return {"reference": []}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)
        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

    @pytest.mark.parametrize(
        "spelling",
        ["10.1234/x", "10.1234/X", "doi:10.1234/x", "DOI: 10.1234/x", "https://doi.org/10.1234/X"],
    )
    @pytest.mark.asyncio
    async def test_every_spelling_echoes_one_value(self, spelling):
        for call in (
            server.get_paper_references_count(spelling),
            server.get_paper_references(spelling),
            server.get_paper_citations_count(spelling),
            server.get_paper_citations(spelling),
        ):
            assert (await call)["doi"] == "10.1234/x"


class TestGraphErrorsCarryAVerdict:
    """Every tool-layer error names whether retrying it can help. Without one,
    a classifier reads "you called this wrong" as "unknown, maybe retry"."""

    @pytest.mark.asyncio
    async def test_auto_past_page_one_is_not_retryable(self):
        result = await server.get_paper_references("10.1234/x", source="auto", page=2)

        assert result["retryable"] is False, "re-issuing the identical call can never help"

    @pytest.mark.asyncio
    async def test_both_failing_is_retryable_when_either_source_might_answer(self, monkeypatch):
        async def fake_cr(doi, **kwargs):
            return {"error": "Crossref 503", "retryable": True}

        async def fake_oc(doi, **kwargs):
            return {"error": "No references found", "not_found": True}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x", source="auto")

        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_both_definitively_absent_is_not_retryable(self, monkeypatch):
        async def fake(doi, **kwargs):
            return {"error": "No record", "not_found": True}

        monkeypatch.setattr(crossref, "get_work", fake)
        monkeypatch.setattr(opencitations, "get_references", fake)

        result = await server.get_paper_references("10.1234/x", source="auto")

        assert result["retryable"] is False


class TestGraphForceRefreshThreading:
    """`force_refresh` has to reach every source a tool touches, or the agent
    asks for fresh coverage and silently gets the cached half."""

    @pytest.mark.asyncio
    async def test_auto_refreshes_both_sources(self, monkeypatch):
        seen = {}

        async def fake_cr(doi, *, force_refresh=False):
            seen["crossref"] = force_refresh
            return {"reference": []}

        async def fake_oc(doi, *, force_refresh=False):
            seen["opencitations"] = force_refresh
            return {"references": [], "count": 0}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        await server.get_paper_references("10.1234/x", force_refresh=True)

        assert seen == {"crossref": True, "opencitations": True}

    @pytest.mark.parametrize("source", ["crossref", "opencitations"])
    @pytest.mark.asyncio
    async def test_an_explicit_source_refreshes_only_itself(self, monkeypatch, source):
        seen = {}

        async def fake_cr(doi, *, force_refresh=False):
            seen["crossref"] = force_refresh
            return {"reference": []}

        async def fake_oc(doi, *, force_refresh=False):
            seen["opencitations"] = force_refresh
            return {"references": [], "count": 0}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        await server.get_paper_references("10.1234/x", source=source, force_refresh=True)

        assert seen == {source: True}

    @pytest.mark.asyncio
    async def test_both_citation_tools_refresh_opencitations(self, monkeypatch):
        seen = []

        async def fake_oc(doi, *, force_refresh=False):
            seen.append(force_refresh)
            return {"citations": [], "count": 0}

        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        await server.get_paper_citations_count("10.1234/x", force_refresh=True)
        await server.get_paper_citations("10.1234/x", force_refresh=True)

        assert seen == [True, True]


class TestGraphSingleSourceErrors:
    """A pinned source has no survivor to fall back on, so its structured error
    is the whole response — and it must arrive with something to do next."""

    @pytest.mark.asyncio
    async def test_crossref_error_names_the_title_search(self, monkeypatch):
        async def fake_cr(doi, **kwargs):
            return {"error": "No work found on Crossref", "not_found": True}

        monkeypatch.setattr(crossref, "get_work", fake_cr)

        result = await server.get_paper_references("10.1234/x", source="crossref")

        assert result["not_found"] is True
        assert "search_crossref_by_title" in result["suggestion"]
        assert "references" not in result

    @pytest.mark.asyncio
    async def test_the_count_tool_surfaces_its_error_with_a_suggestion(self, monkeypatch):
        async def fake_oc(doi, **kwargs):
            return {"error": "OpenCitations 503", "retryable": True}

        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        result = await server.get_paper_citations_count("10.1234/x")

        assert result["retryable"] is True
        assert "suggestion" in result
        assert "count" not in result

    @pytest.mark.asyncio
    async def test_a_providers_own_suggestion_is_not_overwritten(self, monkeypatch):
        async def fake_oc(doi, **kwargs):
            return {"error": "nope", "suggestion": "Do this instead."}

        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        result = await server.get_paper_citations_count("10.1234/x")

        assert result["suggestion"] == "Do this instead."


class TestPartialFailureBothDirections:
    """`partial_failure` names whichever source died, so a short page is never
    read as a confident "no references". Both directions, or the ternary that
    picks the name is only half covered."""

    @pytest.mark.asyncio
    async def test_opencitations_failing_is_named(self, monkeypatch):
        async def fake_cr(doi, **kwargs):
            return {"reference": [{"DOI": "10.1/a"}]}

        async def fake_oc(doi, **kwargs):
            return {"error": "OpenCitations 503", "retryable": True}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x")

        assert result["_source"] == "crossref"
        assert result["partial_failure"] == {
            "source": "opencitations",
            "error": "OpenCitations 503",
            "retryable": True,
        }

    @pytest.mark.asyncio
    async def test_no_partial_failure_when_both_answered(self, monkeypatch):
        async def fake_cr(doi, **kwargs):
            return {"reference": [{"DOI": "10.1/a"}]}

        async def fake_oc(doi, **kwargs):
            return {"references": [], "count": 0}

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

        result = await server.get_paper_references("10.1234/x")

        assert "partial_failure" not in result


class TestHysteresisBoundary:
    """`_CROSSREF_HYSTERESIS` is the policy: OpenCitations takes over only on a
    material margin. The exact edge is what a "simplify this to oc > cr" edit
    would move."""

    @staticmethod
    def _stub(monkeypatch, cr_count, oc_count):
        async def fake_cr(doi, **kwargs):
            return {"reference": [{"DOI": f"10.1/{i}"} for i in range(cr_count)]}

        async def fake_oc(doi, **kwargs):
            return {
                "references": [{"doi": f"10.2/{i}"} for i in range(oc_count)],
                "count": oc_count,
            }

        monkeypatch.setattr(crossref, "get_work", fake_cr)
        monkeypatch.setattr(opencitations, "get_references", fake_oc)

    @pytest.mark.parametrize(
        ("cr_count", "oc_count", "expected"),
        [
            (10, 12, "crossref"),  # exactly at the margin — not material
            (10, 13, "opencitations"),  # one past it
            (0, 0, "crossref"),  # nothing either way: richer shape wins
            (0, 1, "opencitations"),  # any coverage beats none
        ],
    )
    @pytest.mark.asyncio
    async def test_the_margin_decides(self, monkeypatch, cr_count, oc_count, expected):
        self._stub(monkeypatch, cr_count, oc_count)

        result = await server.get_paper_references("10.1234/x")

        assert result["_source"] == expected


# ---------------------------------------------------------------------------
# get_paper_citations: forward-compatible source parameter
# ---------------------------------------------------------------------------


class TestCitationsDispatch:
    """OpenCitations is the only source of incoming citations, so this tool
    takes no `source` parameter — a one-value knob is noise. One would be
    reintroduced if a second citation source ever shipped."""

    @pytest.mark.asyncio
    async def test_auto_dispatches_to_opencitations(self, monkeypatch):
        async def fake_oc(doi, **kwargs):
            return {"citations": [{"doi": "10.x/a"}], "count": 1}

        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        result = await server.get_paper_citations("10.1234/x")
        assert result["_source"] == "opencitations"
        assert result["total"] == 1

    @pytest.mark.asyncio
    async def test_no_source_parameter(self):
        # The reserved-but-inert `source` param was removed; it's gone from
        # the signature (a future second source would reintroduce it).
        import inspect

        assert "source" not in inspect.signature(server.get_paper_citations).parameters

    @pytest.mark.asyncio
    async def test_a_provider_error_reaches_the_agent_with_a_suggestion(self, monkeypatch):
        # OpenCitations is the only source of incoming citations, so there is
        # no survivor: its error is the whole response, and a retryable one
        # must stay flagged as such rather than reading as an empty page.
        async def fake_oc(doi, **kwargs):
            return {"error": "OpenCitations is unreachable", "retryable": True}

        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        result = await server.get_paper_citations("10.1234/x")

        assert result["retryable"] is True
        assert "suggestion" in result
        assert "citations" not in result

    @pytest.mark.asyncio
    async def test_count_missing_count_key_is_zero(self, monkeypatch):
        # If a success response ever lacks the 'count' key, the count tool
        # must degrade to 0 rather than KeyError into a raw 500.
        async def fake_oc(doi, **kwargs):
            return {"citations": []}

        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        result = await server.get_paper_citations_count("10.1234/x")
        assert result["count"] == 0


class TestSourceErrorForwarding:
    """``_source_error`` embeds a provider error inside a multi-source
    response. It used to keep only error/retryable/suggestion, dropping
    exactly the fields that make the distinction actionable.
    """

    def test_forwards_retry_after_and_backpressure(self):
        from academic_tools_mcp.tools import graph

        out = graph._source_error(
            {
                "error": "Local backpressure: ...",
                "retryable": True,
                "backpressure": True,
                "max_concurrency": 5,
                "retry_after_seconds": 0.5,
            }
        )
        assert out["retry_after_seconds"] == 0.5
        assert out["backpressure"] is True
        assert out["max_concurrency"] == 5

    def test_forwards_not_found_so_definitive_is_distinguishable(self):
        from academic_tools_mcp.tools import graph

        out = graph._source_error({"error": "No work found", "not_found": True})
        assert out["not_found"] is True

    def test_drops_unrelated_payload(self):
        from academic_tools_mcp.tools import graph

        out = graph._source_error({"error": "x", "results": [1, 2, 3], "doi": "10.1/x"})
        assert out == {"error": "x"}

    def test_forwards_a_suggestion_a_tool_already_attached(self):
        from academic_tools_mcp.tools import graph

        out = graph._source_error({"error": "x", "suggestion": "Try the other source."})
        assert out["suggestion"] == "Try the other source."

    def test_the_allowlist_covers_every_key_http_can_emit(self):
        """`_source_error` projects an allowlist, so a classification key added
        to `http` is dropped from every multi-source response — silently, since
        an absent key looks exactly like an inapplicable one. Nothing else
        couples the two, so this is the coupling.
        """
        import httpx

        from academic_tools_mcp.net import http
        from academic_tools_mcp.tools import graph

        request = httpx.Request("GET", "https://example.test/works/10.1/x")

        def status_error(code, headers=None):
            return httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(code, request=request, headers=headers or {}),
            )

        produced = [
            http.not_found("gone"),
            http.parse_error_dict("Crossref"),
            http.error_dict(
                "Crossref", http.LocalBackpressureError("Crossref", pending=5, max_pending=5)
            ),
            http.error_dict(
                "Crossref",
                http.LocalBackpressureError(
                    "Crossref", pending=5, max_pending=5, min_gap_seconds=3.0
                ),
            ),
            http.error_dict("Crossref", status_error(429, {"Retry-After": "7"})),
            http.error_dict("Crossref", status_error(503)),
            http.error_dict("Crossref", status_error(418)),
            http.error_dict("Crossref", httpx.TimeoutException("slow", request=request)),
            http.error_dict("Crossref", httpx.RequestError("dns", request=request)),
        ]

        emitted = {key for err in produced for key in err}
        unforwarded = emitted - set(graph._FORWARDED_ERROR_KEYS)

        assert not unforwarded, (
            f"http can emit {sorted(unforwarded)}, which _source_error would drop — "
            "add them to _FORWARDED_ERROR_KEYS or decide deliberately not to forward them"
        )
        # And each forwarded key survives the projection intact.
        for err in produced:
            assert graph._source_error(err) == err
