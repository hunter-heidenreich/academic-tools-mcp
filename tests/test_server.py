"""Tests for tool-layer behaviors that compose multiple modules.

The provider-level tests cover the underlying clients; this file covers
the smartness wired up at the @mcp.tool layer in server.py — chaining
across providers and the auto-source picker.
"""

import pytest

from academic_tools_mcp import cache, manual, papers, server
from academic_tools_mcp.providers import arxiv, biorxiv, crossref, openalex, opencitations
from academic_tools_mcp.tools import paper

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
        async def fake_biorxiv_get_paper(doi, **kwargs):
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

        async def _no_openalex(doi, **kwargs):
            raise AssertionError("OpenAlex must NOT be called when follow_published is False")

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv_get_paper)
        monkeypatch.setattr(openalex, "get_work", _no_openalex)

        result = await server.get_paper_metadata("10.1101/2024.01.01.123")
        assert result["_source"] == "biorxiv"
        assert result["title"] == "Preprint title"
        assert result["published_doi"] == "10.1038/s41586-024-07000-0"
        # No chain requested → the followed_published signal must stay absent
        # so the default response shape is unchanged.
        assert "followed_published" not in result

    @pytest.mark.asyncio
    async def test_follow_published_returns_openalex_journal_record(self, monkeypatch):
        async def fake_biorxiv_get_paper(doi, **kwargs):
            return {
                "doi": "10.1101/2024.01.01.123",
                "title": "Preprint title",
                "published_doi": "10.1038/s41586-024-07000-0",
                "pdf_url": "https://example/pdf",
            }

        async def fake_openalex_get_work(doi, **kwargs):
            assert doi == "10.1038/s41586-024-07000-0", (
                "follow_published must call OpenAlex with the published_doi, not the preprint DOI"
            )
            return {
                "title": "Journal version title",
                "doi": doi,
                "publication_year": 2024,
                "publication_date": "2024-03-15",
                "type": "article",
                "language": "en",
                "primary_location": {"source": {"display_name": "Nature"}},
                "open_access": {
                    "is_oa": True,
                    "oa_status": "hybrid",
                    "oa_url": "https://nature.com/x",
                },
            }

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv_get_paper)
        monkeypatch.setattr(openalex, "get_work", fake_openalex_get_work)

        result = await server.get_paper_metadata("10.1101/2024.01.01.123", follow_published=True)
        assert result["_source"] == "openalex_via_biorxiv"
        assert result["title"] == "Journal version title"
        assert result["doi"] == "10.1038/s41586-024-07000-0"
        assert result["venue"] == "Nature"
        assert result["is_oa"] is True
        # The chain must remain visible — agents that want to know
        # the original preprint can find it here.
        assert result["preprint_doi"] == "10.1101/2024.01.01.123"
        # The chain succeeded: signal it explicitly.
        assert result["followed_published"] is True

    @pytest.mark.asyncio
    async def test_follow_published_no_published_doi_returns_preprint(self, monkeypatch):
        # An unpublished preprint: follow_published=True must still
        # return the bioRxiv record (no journal version exists). The
        # parameter is opt-in convenience, not "I refuse to return a
        # preprint".
        async def fake_biorxiv_get_paper(doi, **kwargs):
            return {
                "doi": "10.1101/unpub",
                "title": "Still preprint",
                "published_doi": None,
            }

        async def _no_openalex(doi, **kwargs):
            raise AssertionError("OpenAlex must NOT be called when there is no published_doi")

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv_get_paper)
        monkeypatch.setattr(openalex, "get_work", _no_openalex)

        result = await server.get_paper_metadata("10.1101/unpub", follow_published=True)
        assert result["_source"] == "biorxiv"
        assert result["title"] == "Still preprint"
        # No journal version to follow → no chain attempted → the signal is
        # absent (the null published_doi already tells the story).
        assert "followed_published" not in result

    @pytest.mark.asyncio
    async def test_follow_published_falls_back_when_openalex_misses(self, monkeypatch):
        # Journal version exists but isn't in OpenAlex yet (paper too
        # new to index, etc.). OpenAlex returns a *definitive* 404
        # (not_found=True). We must fall back to the preprint record
        # so the agent gets *something* — silently failing or erroring
        # would surprise the agent and force a retry path.
        async def fake_biorxiv_get_paper(doi, **kwargs):
            return {
                "doi": "10.1101/2024.fresh",
                "title": "Fresh preprint",
                "published_doi": "10.1038/not-yet-indexed",
                "pdf_url": "https://example/pdf",
            }

        async def fake_openalex_get_work(doi, **kwargs):
            return {"error": "No work found for DOI: 10.1038/not-yet-indexed", "not_found": True}

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv_get_paper)
        monkeypatch.setattr(openalex, "get_work", fake_openalex_get_work)

        result = await server.get_paper_metadata("10.1101/2024.fresh", follow_published=True)
        assert result["_source"] == "biorxiv"
        assert result["title"] == "Fresh preprint"
        assert result["published_doi"] == "10.1038/not-yet-indexed"
        # A chain was attempted and OpenAlex missed — the agent must be able
        # to tell this is preprint-era metadata for a *published* paper, not
        # one that simply has no journal version yet.
        assert result["followed_published"] is False
        # A definitive 404 ("not indexed yet") must NOT be tagged as
        # retryable — retrying won't conjure a record that isn't there.
        assert "published_lookup_retryable" not in result

    @pytest.mark.asyncio
    async def test_follow_published_transient_openalex_error_is_flagged(self, monkeypatch):
        # The journal version may well be in OpenAlex, but the lookup hit a
        # transient failure (5xx / timeout / garbled body) — error dict has
        # no not_found. We still fall back to the preprint so the agent gets
        # *something*, but tag published_lookup_retryable so it can tell this
        # apart from a definitive "not indexed yet" miss and retry.
        async def fake_biorxiv_get_paper(doi, **kwargs):
            return {
                "doi": "10.1101/2024.transient",
                "title": "Transient preprint",
                "published_doi": "10.1038/maybe-indexed",
                "pdf_url": "https://example/pdf",
            }

        async def fake_openalex_get_work(doi, **kwargs):
            # Shape of a transient error from _http — retryable, no not_found.
            return {
                "error": "OpenAlex server error (HTTP 503). Transient — retry.",
                "retryable": True,
            }

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv_get_paper)
        monkeypatch.setattr(openalex, "get_work", fake_openalex_get_work)

        result = await server.get_paper_metadata("10.1101/2024.transient", follow_published=True)
        assert result["_source"] == "biorxiv"
        assert result["title"] == "Transient preprint"
        assert result["followed_published"] is False
        # The distinguishing signal: a retry might surface the journal record.
        assert result["published_lookup_retryable"] is True


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
# Slim search hits: author_count lets the agent decide whether to paginate
# ---------------------------------------------------------------------------


class TestSearchAuthorCount:
    """Slim search responses dropped the full author list to keep
    payloads small; agents got `first_author` only and had no way to
    know whether the paper has 3 authors or 3,000. ``author_count``
    closes that loop without re-bloating the response.
    """

    @pytest.mark.asyncio
    async def test_search_arxiv_includes_author_count(self, monkeypatch):
        async def fake_search(query, max_results=10):
            return {
                "total_results": 1,
                "entries": [
                    {
                        "id": "http://arxiv.org/abs/2301.00001v1",
                        "title": "Tiny paper",
                        "published": "2023-01-01T00:00:00Z",
                        "authors": [{"name": "Jane Doe"}, {"name": "John Roe"}],
                    },
                ],
            }

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        result = await server.search_arxiv("anything")
        assert result["results"][0]["author_count"] == 2
        assert result["results"][0]["first_author"] == "Jane Doe"

    @pytest.mark.asyncio
    async def test_search_arxiv_zero_authors(self, monkeypatch):
        # Defensive: the parser returns [] for missing authors. The slim
        # tool must still report 0, not crash, and not omit the field.
        async def fake_search(query, max_results=10):
            return {
                "total_results": 1,
                "entries": [
                    {
                        "id": "http://arxiv.org/abs/2301.99999",
                        "title": "Authorless oddity",
                        "published": "2023-01-01T00:00:00Z",
                        "authors": [],
                    }
                ],
            }

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        result = await server.search_arxiv("anything")
        assert result["results"][0]["author_count"] == 0
        assert result["results"][0]["first_author"] is None

    @pytest.mark.asyncio
    async def test_search_arxiv_response_shape_matches_crossref(self, monkeypatch):
        # search_arxiv and search_crossref_by_title must return the same
        # top-level shape so an agent can branch on the source without
        # feature-detecting field names. total_results is the upstream
        # match count (how many exist); result_count is how many hits the
        # call actually returned.
        async def fake_arxiv(query, max_results=10):
            return {"total_results": 0, "entries": []}

        async def fake_crossref(bibliographic, year=None, rows=5):
            return {"items": [], "total_results": 0}

        monkeypatch.setattr(arxiv, "search_papers", fake_arxiv)
        monkeypatch.setattr(crossref, "search_works", fake_crossref)

        arxiv_result = await server.search_arxiv("anything")
        crossref_result = await server.search_crossref_by_title("anything")
        assert set(arxiv_result.keys()) == {"total_results", "result_count", "results"}
        assert set(crossref_result.keys()) == set(arxiv_result.keys())

    @pytest.mark.asyncio
    async def test_search_arxiv_total_vs_result_count(self, monkeypatch):
        # total_results is arXiv's upstream match count; result_count is
        # how many hits this page returned. They are NOT the same number.
        async def fake_search(query, max_results=10):
            return {
                "total_results": 50000,
                "entries": [
                    {
                        "id": "http://arxiv.org/abs/2301.00001v1",
                        "title": "One of many",
                        "published": "2023-01-01T00:00:00Z",
                        "authors": [{"name": "Jane Doe"}],
                    },
                ],
            }

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        result = await server.search_arxiv("anything")
        assert result["total_results"] == 50000
        assert result["result_count"] == 1

    @pytest.mark.asyncio
    async def test_search_crossref_total_vs_result_count(self, monkeypatch):
        # Crossref exposes message.total-results upstream; the tool surfaces
        # it as total_results (NOT len of the returned page) plus a
        # result_count for the page size, matching search_arxiv.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["One of many"],
                        "author": [{"given": "Jane", "family": "Doe"}],
                        "published-online": {"date-parts": [[2023]]},
                    }
                ],
                "total_results": 312,
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["total_results"] == 312
        assert result["result_count"] == 1

    @pytest.mark.asyncio
    async def test_search_crossref_date_parts_none(self, monkeypatch):
        # Malformed Crossref record: date-parts present but null. Must not
        # crash (TypeError on None[0]) — degrade to year=None.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["Null date-parts"],
                        "published-online": {"date-parts": None},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["year"] is None

    @pytest.mark.asyncio
    async def test_search_crossref_date_parts_empty(self, monkeypatch):
        # Malformed Crossref record: date-parts is an empty list. Must not
        # crash (IndexError on [][0]) — degrade to year=None.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["Empty date-parts"],
                        "published-print": {"date-parts": []},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["year"] is None

    @pytest.mark.asyncio
    async def test_search_crossref_year_from_posted(self, monkeypatch):
        # bioRxiv/preprint records carry their date under `posted`, not
        # published-print/-online. The de-facto bioRxiv search must still
        # surface the year.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1101/2024.05.01.123",
                        "title": ["A preprint"],
                        "author": [{"given": "Jane", "family": "Doe"}],
                        "posted": {"date-parts": [[2024, 5]]},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["year"] == 2024

    @pytest.mark.asyncio
    async def test_search_crossref_org_first_author(self, monkeypatch):
        # Consortium authors carry a `name` field with no given/family.
        # first_author must surface it instead of dropping to None.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["Big collaboration paper"],
                        "author": [{"name": "The ATLAS Collaboration"}],
                        "published-online": {"date-parts": [[2023]]},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["first_author"] == "The ATLAS Collaboration"
        assert result["results"][0]["author_count"] == 1

    @pytest.mark.asyncio
    async def test_search_crossref_by_title_includes_author_count(self, monkeypatch):
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["Some title"],
                        "author": [
                            {"given": "Jane", "family": "Doe"},
                            {"given": "John", "family": "Roe"},
                            {"given": "Alice", "family": "Smith"},
                        ],
                        "published-online": {"date-parts": [[2023]]},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["author_count"] == 3
        assert result["results"][0]["first_author"] == "Jane Doe"

    @pytest.mark.asyncio
    async def test_search_crossref_missing_author_field(self, monkeypatch):
        # Some Crossref records omit the author array entirely. The
        # slim tool must report 0 instead of NoneType / KeyError.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["No-author edge case"],
                        "published-online": {"date-parts": [[2023]]},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["author_count"] == 0
        assert result["results"][0]["first_author"] is None


# ---------------------------------------------------------------------------
# find_in_paper: error contract + happy path
# ---------------------------------------------------------------------------


class TestFindInPaper:
    """find_in_paper scans one converted paper's markdown. It must error
    cleanly (with a suggestion, like every other tool here) when the paper
    isn't converted, and return positioned hits when it is.
    """

    @pytest.fixture
    def isolated_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / ".cache")
        return tmp_path / ".cache"

    @pytest.mark.asyncio
    async def test_unconverted_paper_errors_with_suggestion(self, isolated_cache):
        # No markdown on disk for this identifier — the response must carry
        # both an `error` and a `suggestion`, matching the {error, suggestion}
        # contract the rest of the search tools honour.
        result = await server.find_in_paper("2301.00001", "anything")
        assert "error" in result
        assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_finds_substring_in_converted_paper(self, isolated_cache):
        # Seed a converted-markdown file at the path find_in_paper resolves
        # to, then assert it locates the query and returns positioned hits.
        target = manual.resolve_target("2301.00001")
        md_path = papers.markdown_path(target["namespace"], target["canonical"])
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# Title\n\n## Introduction\n\nWe study variational dropout here.\n")

        result = await server.find_in_paper("2301.00001", "variational dropout")
        assert result["result_count"] == 1
        assert result["truncated"] is False
        hit = result["results"][0]
        assert hit["section"] == "Introduction"
        assert "char_offset" in hit


# ---------------------------------------------------------------------------
# get_paper_authors: response shape is symmetric across providers
# ---------------------------------------------------------------------------


class TestFormatOpenalexAuthors:
    """Unit coverage for the extracted _format_openalex_authors slice helper."""

    def test_slices_page_and_dedupes_institutions(self):
        work = {
            "authorships": [
                {
                    "author": {"display_name": "Dan", "id": "A1"},
                    "author_position": "first",
                    "is_corresponding": True,
                    "institutions": [{"display_name": "MIT"}, {"display_name": "CSAIL"}],
                },
                {
                    "author": {"display_name": "Eve", "id": "A2"},
                    "author_position": "last",
                    "is_corresponding": False,
                    "institutions": [{"display_name": "MIT"}],
                },
                {
                    "author": {"display_name": "Frank", "id": "A3"},
                    "author_position": "middle",
                    "is_corresponding": False,
                    "institutions": [{"display_name": "Stanford"}],
                },
            ]
        }
        # author_count is the global total; the returned page is just [0, 2).
        out = paper._format_openalex_authors(work, 0, 2)
        assert out["author_count"] == 3
        assert [a["name"] for a in out["authors"]] == ["Dan", "Eve"]
        assert out["authors"][0]["openalex_id"] == "A1"
        assert out["authors"][0]["position"] == "first"
        assert out["authors"][0]["is_corresponding"] is True
        # MIT appears on both page authors but is deduped in the roll-up.
        assert out["page_institutions"] == ["MIT", "CSAIL"]
        assert out["page_institution_count"] == 2

    def test_empty_authorships(self):
        out = paper._format_openalex_authors({}, 0, 25)
        assert out["author_count"] == 0
        assert out["authors"] == []
        assert out["page_institutions"] == []
        assert out["page_institution_count"] == 0


class TestAuthorsShapeSymmetry:
    """Agents that paginate get_paper_authors expect the same keys
    regardless of which provider serves the paper. Earlier the
    page_institutions / page_institution_count fields only appeared on
    the OpenAlex branch, which forced agent code to feature-detect the
    shape mid-loop. They now appear on every branch (empty for arxiv /
    biorxiv where the upstream API doesn't carry institution rollups).
    """

    @pytest.mark.asyncio
    async def test_arxiv_branch_includes_empty_institution_fields(self, monkeypatch):
        async def fake_arxiv(arxiv_id, **kwargs):
            return {
                "id": "http://arxiv.org/abs/2301.00001v1",
                "authors": [{"name": "Jane Doe"}, {"name": "John Roe"}],
            }

        monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)

        result = await server.get_paper_authors("2301.00001")
        assert result["_source"] == "arxiv"
        assert result["page_institutions"] == []
        assert result["page_institution_count"] == 0

    @pytest.mark.asyncio
    async def test_biorxiv_branch_includes_empty_institution_fields(self, monkeypatch):
        async def fake_biorxiv(doi, **kwargs):
            return {
                "doi": "10.1101/x",
                "authors": [{"name": "Jane Doe"}],
                "author_corresponding": "Jane Doe",
                "author_corresponding_institution": "Some Lab",
            }

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)

        result = await server.get_paper_authors("10.1101/x")
        assert result["_source"] == "biorxiv"
        assert result["page_institutions"] == []
        assert result["page_institution_count"] == 0
        # Author-corresponding fields still surface independently.
        assert result["author_corresponding"] == "Jane Doe"
        assert result["author_corresponding_institution"] == "Some Lab"

    @pytest.mark.asyncio
    async def test_openalex_branch_populates_institutions(self, monkeypatch):
        # Sanity check that the OpenAlex branch still rolls up
        # institutions from the page — symmetry can't come at the cost
        # of the original behaviour.
        async def fake_openalex(doi, **kwargs):
            return {
                "authorships": [
                    {
                        "author": {"id": "A1", "display_name": "Jane Doe"},
                        "institutions": [{"display_name": "MIT"}],
                    },
                    {
                        "author": {"id": "A2", "display_name": "John Roe"},
                        "institutions": [{"display_name": "MIT"}, {"display_name": "Stanford"}],
                    },
                ]
            }

        monkeypatch.setattr(openalex, "get_work", fake_openalex)

        result = await server.get_paper_authors("10.1234/x")
        assert result["_source"] == "openalex"
        # MIT appears in both authorships; dedupe keeps it once.
        assert sorted(result["page_institutions"]) == ["MIT", "Stanford"]
        assert result["page_institution_count"] == 2


class TestCanonicalIdInResponses:
    """Every successful metadata response carries _canonical_id (the
    provider's normalized form of the input identifier). Agents reuse
    that across subsequent tool calls instead of re-normalizing whatever
    the user originally typed.
    """

    @pytest.mark.asyncio
    async def test_arxiv_metadata_keeps_version_and_lowercases(self, monkeypatch):
        async def fake_arxiv(arxiv_id, **kwargs):
            return {
                "id": "http://arxiv.org/abs/2301.00001v3",
                "title": "x",
            }

        monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)

        # Caller passes the version-suffixed form; canonical keeps it, so a
        # request for v3 can never be answered from another version's entry.
        result = await server.get_paper_metadata("2301.00001v3")
        assert result["_canonical_id"] == "2301.00001v3"

    @pytest.mark.asyncio
    async def test_openalex_metadata_lowercases_doi(self, monkeypatch):
        async def fake_openalex(doi, **kwargs):
            return {"title": "x", "doi": "https://doi.org/10.1038/X.2024.Y"}

        monkeypatch.setattr(openalex, "get_work", fake_openalex)

        # Mixed-case URL form normalises to lowercase bare DOI.
        result = await server.get_paper_metadata("https://doi.org/10.1038/X.2024.Y")
        assert result["_canonical_id"] == "10.1038/x.2024.y"

    @pytest.mark.asyncio
    async def test_canonical_id_present_across_paper_tool_family(self, monkeypatch):
        # All four unified paper tools must echo _canonical_id so an
        # agent that branches on _source always finds the same field.
        async def fake_arxiv(arxiv_id, **kwargs):
            return {
                "id": "http://arxiv.org/abs/2301.00001v1",
                "title": "x",
                "summary": "a",
                "authors": [{"name": "Jane Doe"}],
                "published": "2023-01-01T00:00:00Z",
            }

        monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)

        for tool in (
            server.get_paper_metadata,
            server.get_paper_authors,
            server.get_paper_abstract,
            server.get_paper_bibtex,
        ):
            result = await tool("2301.00001v1")
            assert result["_canonical_id"] == "2301.00001v1", (
                f"{tool.__name__} missing canonical id"
            )

    @pytest.mark.asyncio
    async def test_follow_published_canonical_is_journal_doi(self, monkeypatch):
        # When follow_published chains from a bioRxiv preprint to the
        # OpenAlex journal record, _canonical_id must reflect the
        # journal DOI (the paper the response now describes), with the
        # original preprint DOI surfaced separately as preprint_doi.
        async def fake_biorxiv(doi, **kwargs):
            return {
                "doi": "10.1101/2024.01.01.123",
                "title": "Preprint",
                "published_doi": "10.1038/S41586-024-07000-0",
            }

        async def fake_openalex(doi, **kwargs):
            return {"title": "Journal version", "doi": doi}

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)
        monkeypatch.setattr(openalex, "get_work", fake_openalex)

        result = await server.get_paper_metadata("10.1101/2024.01.01.123", follow_published=True)
        assert result["_source"] == "openalex_via_biorxiv"
        assert result["_canonical_id"] == "10.1038/s41586-024-07000-0"
        assert result["preprint_doi"] == "10.1101/2024.01.01.123"


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


# ---------------------------------------------------------------------------
# Debug tools are NOT registered in the default configuration
# ---------------------------------------------------------------------------


class TestDebugToolsGating:
    """get_server_stats exists in the codebase but must only register
    when ENABLE_DEBUG_TOOLS is truthy in the env. The default-off
    posture matters because the snapshot exposes operational data
    (counter values, in-flight queues) that agents shouldn't branch on.
    """

    def test_debug_tool_not_registered_by_default(self):
        # The env var was unset on import, so the @mcp.tool block was
        # skipped and the function should not exist at module scope.
        assert not hasattr(server, "get_server_stats"), (
            "get_server_stats must NOT be registered when "
            "ENABLE_DEBUG_TOOLS is unset — agents would see it"
        )
        assert server._DEBUG_TOOLS_ENABLED is False


# ---------------------------------------------------------------------------
# get_author threads force_refresh through to the provider
# ---------------------------------------------------------------------------


class TestGetAuthorForceRefresh:
    """get_author should expose force_refresh like its sibling metadata
    tools — author stats (h_index, cited_by_count) drift on the same
    30-day TTL as works, so an agent must be able to bust the cache.
    """

    @pytest.mark.asyncio
    async def test_force_refresh_passthrough(self, monkeypatch):
        seen: list[bool] = []

        async def fake_get_author(author_id, **kwargs):
            seen.append(kwargs.get("force_refresh"))
            return {"display_name": "Ada Lovelace", "id": "https://openalex.org/A123"}

        monkeypatch.setattr(openalex, "get_author", fake_get_author)

        await server.get_author("A123")
        await server.get_author("A123", force_refresh=True)

        assert seen == [False, True], (
            "get_author must thread force_refresh through to openalex.get_author"
        )


# ---------------------------------------------------------------------------
# Error-suggestion hints are centralized in the module constants
# ---------------------------------------------------------------------------


class TestFetchSourceDispatch:
    """_fetch_source resolves identifier → (source, canonical_id, raw_obj),
    returning the raw (un-enriched) provider object so get_paper_metadata can
    inspect provider-specific error flags before a suggestion is attached."""

    @pytest.mark.asyncio
    async def test_arxiv_success_returns_canonical_and_raw(self, monkeypatch):
        async def fake(arxiv_id, **kwargs):
            return {"id": "http://arxiv.org/abs/2301.00001v1", "title": "T"}

        monkeypatch.setattr(arxiv, "get_paper", fake)
        source, cid, obj = await paper._fetch_source("2301.00001v1")
        assert source == "arxiv"
        assert cid == "2301.00001v1"
        assert obj["title"] == "T"

    @pytest.mark.asyncio
    async def test_error_is_left_unenriched(self, monkeypatch):
        async def fake(doi, **kwargs):
            return {"error": "boom"}

        monkeypatch.setattr(openalex, "get_work", fake)
        source, _, obj = await paper._fetch_source("10.1234/x")
        assert source == "openalex"
        assert obj == {"error": "boom"}  # no suggestion attached by _fetch_source

    @pytest.mark.asyncio
    async def test_unknown_identifier_yields_source_none(self):
        source, cid, obj = await paper._fetch_source("not-an-identifier-at-all")
        assert source is None
        assert cid is None
        assert "error" in obj


class TestFormatMetadataBySource:
    """_format_metadata_by_source routes a raw object to the right per-source
    formatter so get_paper_metadata and the batch path share one mapping."""

    def test_dispatches_each_source(self):
        arxiv_obj = {"id": "http://arxiv.org/abs/2301.00001v1", "title": "A"}
        assert (
            paper._format_metadata_by_source("arxiv", arxiv_obj, "2301.00001")["_source"] == "arxiv"
        )
        bio_obj = {"doi": "10.1101/x", "title": "B"}
        assert (
            paper._format_metadata_by_source("biorxiv", bio_obj, "10.1101/x")["_source"]
            == "biorxiv"
        )
        oa_obj = {"title": "C"}
        assert (
            paper._format_metadata_by_source("openalex", oa_obj, "10.1/c")["_source"] == "openalex"
        )


class TestMetadataHintsCentralized:
    """All four unified paper tools surface the same per-source error
    suggestion. The text lives once in the _*_METADATA_HINT constants;
    every tool must route through them rather than re-inlining the
    literal, so a future edit to a constant propagates everywhere.
    """

    @pytest.mark.asyncio
    async def test_sibling_tools_emit_the_canonical_hint(self, monkeypatch):
        async def fake_error(identifier, **kwargs):
            return {"error": "boom"}

        monkeypatch.setattr(arxiv, "get_paper", fake_error)
        monkeypatch.setattr(biorxiv, "get_paper", fake_error)
        monkeypatch.setattr(openalex, "get_work", fake_error)

        cases = [
            ("2301.00001", paper._ARXIV_METADATA_HINT),
            ("10.1101/2024.01.01.123", paper._BIORXIV_METADATA_HINT),
            ("10.1038/s41586-024-07000-0", paper._OPENALEX_METADATA_HINT),
        ]
        sibling_tools = (
            server.get_paper_authors,
            server.get_paper_abstract,
            server.get_paper_bibtex,
        )
        for tool in sibling_tools:
            for identifier, expected_hint in cases:
                result = await tool(identifier)
                assert result["suggestion"] == expected_hint, (
                    f"{tool.__name__}({identifier!r}) must use the shared hint constant"
                )


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
        to `_http` is dropped from every multi-source response — silently, since
        an absent key looks exactly like an inapplicable one. Nothing else
        couples the two, so this is the coupling.
        """
        import httpx

        from academic_tools_mcp import _http
        from academic_tools_mcp.tools import graph

        request = httpx.Request("GET", "https://example.test/works/10.1/x")

        def status_error(code, headers=None):
            return httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(code, request=request, headers=headers or {}),
            )

        produced = [
            _http.not_found("gone"),
            _http.parse_error_dict("Crossref"),
            _http.error_dict(
                "Crossref", _http.LocalBackpressureError("Crossref", pending=5, max_pending=5)
            ),
            _http.error_dict(
                "Crossref",
                _http.LocalBackpressureError(
                    "Crossref", pending=5, max_pending=5, min_gap_seconds=3.0
                ),
            ),
            _http.error_dict("Crossref", status_error(429, {"Retry-After": "7"})),
            _http.error_dict("Crossref", status_error(503)),
            _http.error_dict("Crossref", status_error(418)),
            _http.error_dict("Crossref", httpx.TimeoutException("slow", request=request)),
            _http.error_dict("Crossref", httpx.RequestError("dns", request=request)),
        ]

        emitted = {key for err in produced for key in err}
        unforwarded = emitted - set(graph._FORWARDED_ERROR_KEYS)

        assert not unforwarded, (
            f"_http can emit {sorted(unforwarded)}, which _source_error would drop — "
            "add them to _FORWARDED_ERROR_KEYS or decide deliberately not to forward them"
        )
        # And each forwarded key survives the projection intact.
        for err in produced:
            assert graph._source_error(err) == err


# ---------------------------------------------------------------------------
# get_paper_metadata: the follow_published chain's retry verdict
# ---------------------------------------------------------------------------


class TestPublishedLookupRetryVerdict:
    """`published_lookup_retryable` is a claim that retrying the chain might
    work. `_http`'s vocabulary is three-state — transient carries
    `retryable: True`, a definitive miss carries `not_found: True`, and any
    other 4xx carries neither, deliberately ("unknown != definitive"). Reading
    the tag off the *absence* of `not_found` collapses three states into two
    and sends the agent back at a call that can never succeed.
    """

    @staticmethod
    def _chain(monkeypatch, work_error):
        async def fake_biorxiv(doi, **kwargs):
            return {
                "doi": "10.1101/2024.01.01.123",
                "title": "Preprint",
                "published_doi": "10.1038/s41586-024-07000-0",
            }

        async def fake_openalex(doi, **kwargs):
            return work_error

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)
        monkeypatch.setattr(openalex, "get_work", fake_openalex)
        return server.get_paper_metadata("10.1101/2024.01.01.123", follow_published=True)

    @pytest.mark.asyncio
    async def test_an_unclassified_4xx_is_not_reported_as_retryable(self, monkeypatch):
        import httpx

        from academic_tools_mcp import _http

        request = httpx.Request("GET", "https://api.openalex.org/works/doi:10.1038/x")
        err = _http.error_dict(
            "OpenAlex",
            httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(403, request=request, content=b"forbidden"),
            ),
        )
        assert "retryable" not in err and "not_found" not in err, "the premise of this test"

        result = await self._chain(monkeypatch, err)
        assert result["_source"] == "biorxiv"
        assert result["followed_published"] is False
        assert "published_lookup_retryable" not in result

    @pytest.mark.asyncio
    async def test_a_transient_failure_is_reported_as_retryable(self, monkeypatch):
        result = await self._chain(monkeypatch, {"error": "OpenAlex 503", "retryable": True})
        assert result["published_lookup_retryable"] is True

    @pytest.mark.asyncio
    async def test_a_definitive_miss_is_not_reported_as_retryable(self, monkeypatch):
        result = await self._chain(monkeypatch, {"error": "No work found", "not_found": True})
        assert result["followed_published"] is False
        assert "published_lookup_retryable" not in result

    @pytest.mark.asyncio
    async def test_the_verdict_is_total_over_every_error_http_can_build(self, monkeypatch):
        """The coupling between `_http`'s constructors and this one branch.

        Nothing else links them, so a new classification key — or a change to
        which errors carry `retryable` — would silently re-flavour the tag.
        The tag must appear for exactly the errors `_http` calls retryable.
        """
        import httpx

        from academic_tools_mcp import _http

        request = httpx.Request("GET", "https://api.openalex.org/works/doi:10.1038/x")

        def status_error(code, headers=None):
            return httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(code, request=request, headers=headers or {}),
            )

        produced = [
            _http.not_found("gone"),
            _http.parse_error_dict("OpenAlex"),
            _http.error_dict(
                "OpenAlex", _http.LocalBackpressureError("OpenAlex", pending=5, max_pending=5)
            ),
            _http.error_dict("OpenAlex", status_error(429, {"Retry-After": "7"})),
            _http.error_dict("OpenAlex", status_error(503)),
            _http.error_dict("OpenAlex", status_error(418)),
            _http.error_dict("OpenAlex", status_error(403)),
            _http.error_dict("OpenAlex", httpx.TimeoutException("slow", request=request)),
            _http.error_dict("OpenAlex", httpx.RequestError("dns", request=request)),
        ]

        for err in produced:
            result = await self._chain(monkeypatch, dict(err))
            assert result["followed_published"] is False
            tagged = result.get("published_lookup_retryable") is True
            assert tagged == (err.get("retryable") is True), err


# ---------------------------------------------------------------------------
# get_paper_authors: pagination end to end
# ---------------------------------------------------------------------------


def _flat_authors(n):
    return [{"name": f"A{i}"} for i in range(n)]


class TestAuthorsPagination:
    """`page_bounds` is shared with the graph tools, but the envelope around it
    is this tool's own — and no example had ever passed `page` or `page_size`.
    """

    @pytest.mark.asyncio
    async def test_pages_are_disjoint_and_cover_the_list(self, monkeypatch):
        async def fake_arxiv(arxiv_id, **kwargs):
            return {"id": "http://arxiv.org/abs/2301.00001v1", "authors": _flat_authors(5)}

        monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)

        first = await server.get_paper_authors("2301.00001", page=1, page_size=2)
        second = await server.get_paper_authors("2301.00001", page=2, page_size=2)
        third = await server.get_paper_authors("2301.00001", page=3, page_size=2)

        assert [a["name"] for a in first["authors"]] == ["A0", "A1"]
        assert [a["name"] for a in second["authors"]] == ["A2", "A3"]
        assert [a["name"] for a in third["authors"]] == ["A4"]
        assert first["has_more"] is True
        assert second["has_more"] is True
        assert third["has_more"] is False
        assert first["author_count"] == second["author_count"] == 5

    @pytest.mark.asyncio
    async def test_a_page_past_the_end_is_empty(self, monkeypatch):
        async def fake_arxiv(arxiv_id, **kwargs):
            return {"id": "http://arxiv.org/abs/2301.00001v1", "authors": _flat_authors(3)}

        monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)

        result = await server.get_paper_authors("2301.00001", page=9, page_size=25)
        assert result["authors"] == []
        assert result["has_more"] is False
        assert result["author_count"] == 3
        assert result["page"] == 9 and result["page_size"] == 25

    @pytest.mark.asyncio
    async def test_both_page_size_bounds(self, monkeypatch):
        async def fake_arxiv(arxiv_id, **kwargs):
            return {"id": "http://arxiv.org/abs/2301.00001v1", "authors": _flat_authors(30)}

        monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)

        one = await server.get_paper_authors("2301.00001", page=1, page_size=1)
        cap = await server.get_paper_authors("2301.00001", page=1, page_size=25)
        assert len(one["authors"]) == 1
        assert len(cap["authors"]) == 25
        assert cap["has_more"] is True

    @pytest.mark.asyncio
    async def test_biorxiv_corresponding_fields_appear_on_every_page(self, monkeypatch):
        async def fake_biorxiv(doi, **kwargs):
            return {
                "doi": "10.1101/x",
                "authors": _flat_authors(3),
                "author_corresponding": "Jane Doe",
                "author_corresponding_institution": "Some Lab",
            }

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)

        second = await server.get_paper_authors("10.1101/x", page=2, page_size=2)
        assert [a["name"] for a in second["authors"]] == ["A2"]
        assert second["author_corresponding"] == "Jane Doe"
        assert second["author_corresponding_institution"] == "Some Lab"

    @pytest.mark.asyncio
    async def test_the_institution_rollup_is_per_page_not_global(self, monkeypatch):
        async def fake_openalex(doi, **kwargs):
            return {
                "id": "W1",
                "authorships": [
                    {
                        "author": {"id": "A1", "display_name": "Jane"},
                        "institutions": [{"display_name": "MIT"}],
                    },
                    {
                        "author": {"id": "A2", "display_name": "John"},
                        "institutions": [{"display_name": "MIT"}, {"display_name": "Stanford"}],
                    },
                ],
            }

        monkeypatch.setattr(openalex, "get_work", fake_openalex)

        first = await server.get_paper_authors("10.1234/x", page=1, page_size=1)
        second = await server.get_paper_authors("10.1234/x", page=2, page_size=1)
        # MIT is on both authors, so it must be reported on both pages — the
        # roll-up describes the page, not the paper.
        assert first["page_institutions"] == ["MIT"]
        assert second["page_institutions"] == ["MIT", "Stanford"]
        assert second["page_institution_count"] == 2


# ---------------------------------------------------------------------------
# get_paper_abstract / get_paper_bibtex: the source dispatch itself
# ---------------------------------------------------------------------------


class TestAbstractDispatch:
    """Each source keeps the abstract under a different key; nothing had ever
    asserted the returned text, only that the tool echoed `_canonical_id`."""

    @pytest.mark.asyncio
    async def test_arxiv_reads_summary(self, monkeypatch):
        async def fake(arxiv_id, **kwargs):
            return {
                "id": "http://arxiv.org/abs/2301.00001v1",
                "title": "T",
                "summary": "We show that.",
            }

        monkeypatch.setattr(arxiv, "get_paper", fake)
        result = await server.get_paper_abstract("2301.00001")
        assert result == {
            "_source": "arxiv",
            "_canonical_id": "2301.00001",
            "title": "T",
            "abstract": "We show that.",
        }

    @pytest.mark.asyncio
    async def test_biorxiv_reads_abstract(self, monkeypatch):
        async def fake(doi, **kwargs):
            return {"doi": "10.1101/x", "title": "T", "abstract": "Preprint abstract."}

        monkeypatch.setattr(biorxiv, "get_paper", fake)
        result = await server.get_paper_abstract("10.1101/x")
        assert result["_source"] == "biorxiv"
        assert result["abstract"] == "Preprint abstract."

    @pytest.mark.asyncio
    async def test_openalex_reconstructs_the_inverted_index(self, monkeypatch):
        async def fake(doi, **kwargs):
            return {
                "id": "W1",
                "title": "T",
                "abstract_inverted_index": {"Hello": [0], "inverted": [2], "world": [1]},
            }

        monkeypatch.setattr(openalex, "get_work", fake)
        result = await server.get_paper_abstract("10.1234/x")
        assert result["_source"] == "openalex"
        assert result["abstract"] == "Hello world inverted"

    @pytest.mark.asyncio
    async def test_a_missing_openalex_index_is_null_not_empty_string(self, monkeypatch):
        """`reconstruct_abstract` answers "" for a missing or malformed index.
        Returning that verbatim would read as "the abstract is blank"; the tool
        normalizes it to null, which is "we don't have one"."""

        async def fake(doi, **kwargs):
            return {"id": "W1", "title": "T", "abstract_inverted_index": None}

        monkeypatch.setattr(openalex, "get_work", fake)
        result = await server.get_paper_abstract("10.1234/x")
        assert result["abstract"] is None


class TestBibtexDispatch:
    """One generator per provider shape; the tool picks which. `test_bibtex.py`
    drives all three directly and never through the dispatch."""

    @pytest.mark.asyncio
    async def test_arxiv_without_journal_ref_is_misc_with_eprint(self, monkeypatch):
        async def fake(arxiv_id, **kwargs):
            return {
                "id": "http://arxiv.org/abs/2301.00001v1",
                "title": "A Paper",
                "authors": [{"name": "Jane Doe"}],
                "published": "2023-01-01T00:00:00Z",
                "primary_category": "cs.LG",
            }

        monkeypatch.setattr(arxiv, "get_paper", fake)
        result = await server.get_paper_bibtex("2301.00001")
        assert result["_source"] == "arxiv"
        assert result["bibtex"].startswith("@misc{")
        # Field names render lowercased; BibTeX field names are case-insensitive.
        assert "eprint={2301.00001}" in result["bibtex"]
        assert "archiveprefix={arXiv}" in result["bibtex"]
        assert "primaryclass={cs.LG}" in result["bibtex"]

    @pytest.mark.asyncio
    async def test_arxiv_with_journal_ref_is_article(self, monkeypatch):
        async def fake(arxiv_id, **kwargs):
            return {
                "id": "http://arxiv.org/abs/2301.00001v1",
                "title": "A Paper",
                "authors": [{"name": "Jane Doe"}],
                "published": "2023-01-01T00:00:00Z",
                "journal_ref": "Nature 615 (2023) 1-9",
            }

        monkeypatch.setattr(arxiv, "get_paper", fake)
        result = await server.get_paper_bibtex("2301.00001")
        assert result["bibtex"].startswith("@article{")

    @pytest.mark.asyncio
    async def test_biorxiv_preprint_is_misc_and_published_is_article(self, monkeypatch):
        base = {
            "doi": "10.1101/x",
            "title": "A Preprint",
            "authors": [{"name": "Jane Doe"}],
            "date": "2024-01-01",
            "server": "bioRxiv",
        }

        async def preprint(doi, **kwargs):
            return base

        monkeypatch.setattr(biorxiv, "get_paper", preprint)
        assert (await server.get_paper_bibtex("10.1101/x"))["bibtex"].startswith("@misc{")

        async def published(doi, **kwargs):
            return {**base, "published_doi": "10.1038/s41586-024-07000-0"}

        monkeypatch.setattr(biorxiv, "get_paper", published)
        out = await server.get_paper_bibtex("10.1101/x")
        assert out["_source"] == "biorxiv"
        assert out["bibtex"].startswith("@article{")

    @pytest.mark.asyncio
    async def test_openalex_type_selects_the_entry(self, monkeypatch):
        async def fake(doi, **kwargs):
            return {
                "id": "W1",
                "title": "A Conference Paper",
                "type": "conference-paper",
                "publication_year": 2023,
                "authorships": [{"author": {"display_name": "Jane Doe"}}],
            }

        monkeypatch.setattr(openalex, "get_work", fake)
        result = await server.get_paper_bibtex("10.1234/x")
        assert result["_source"] == "openalex"
        assert result["bibtex"].startswith("@inproceedings{")


# ---------------------------------------------------------------------------
# get_author: the projected profile
# ---------------------------------------------------------------------------


class TestGetAuthorShape:
    @pytest.mark.asyncio
    async def test_projects_stats_topics_and_affiliations(self, monkeypatch):
        async def fake(author_id, **kwargs):
            return {
                "id": "https://openalex.org/A1",
                "display_name": "Jane Doe",
                "orcid": "https://orcid.org/0000-0001-0000-0000",
                "works_count": 42,
                "cited_by_count": 900,
                "summary_stats": {"h_index": 17, "i10_index": 25, "2yr_mean_citedness": 3.1},
                "last_known_institutions": [
                    {"display_name": "MIT"},
                    {"display_name": None},  # filtered, not rendered as null
                ],
                "topics": [{"display_name": f"T{i}", "count": i} for i in range(7)],
                "affiliations": [
                    {
                        "institution": {"display_name": "MIT", "country_code": "US"},
                        "years": [2021, 2019, 2020],
                    }
                ],
            }

        monkeypatch.setattr(openalex, "get_author", fake)
        result = await server.get_author("A1")

        assert result["name"] == "Jane Doe"
        assert result["h_index"] == 17 and result["i10_index"] == 25
        assert result["works_count"] == 42 and result["cited_by_count"] == 900
        assert result["current_institutions"] == ["MIT"]
        assert len(result["top_topics"]) == 5, "capped at 5"
        assert result["top_topics"][0] == {"name": "T0", "count": 0}
        assert result["affiliations"] == [
            {"institution": "MIT", "country_code": "US", "years": [2019, 2020, 2021]}
        ]

    @pytest.mark.asyncio
    async def test_an_all_null_profile_projects_to_empties(self, monkeypatch):
        """OpenAlex nulls keys rather than dropping them, at every level."""

        async def fake(author_id, **kwargs):
            return {
                "id": "A1",
                "display_name": None,
                "summary_stats": None,
                "last_known_institutions": None,
                "topics": None,
                "affiliations": None,
            }

        monkeypatch.setattr(openalex, "get_author", fake)
        result = await server.get_author("A1")
        assert result["current_institutions"] == []
        assert result["top_topics"] == []
        assert result["affiliations"] == []
        assert result["h_index"] is None

    @pytest.mark.asyncio
    async def test_error_carries_the_chaining_suggestion(self, monkeypatch):
        async def fake(author_id, **kwargs):
            return {"error": "No author found for ID: nope", "not_found": True}

        monkeypatch.setattr(openalex, "get_author", fake)
        result = await server.get_author("nope")
        assert "get_paper_authors" in result["suggestion"]
        assert result["not_found"] is True


# ---------------------------------------------------------------------------
# force_refresh reaches every provider the paper family touches
# ---------------------------------------------------------------------------


class TestPaperFamilyForceRefreshThreading:
    """Every provider fake in this suite absorbs `force_refresh` as `**kwargs`,
    so nothing had asserted the family threads it at all. A tool that drops it
    serves the stale record the agent explicitly asked to bypass.
    """

    @pytest.mark.asyncio
    async def test_the_four_unified_tools_thread_it(self, monkeypatch):
        for identifier, provider, attr in (
            ("2301.00001", arxiv, "get_paper"),
            ("10.1101/x", biorxiv, "get_paper"),
            ("10.1234/x", openalex, "get_work"),
        ):
            for tool in (
                server.get_paper_metadata,
                server.get_paper_authors,
                server.get_paper_abstract,
                server.get_paper_bibtex,
            ):
                seen: list[bool] = []

                async def fake(ident, *, force_refresh=False, _seen=seen, **kwargs):
                    _seen.append(force_refresh)
                    return {"id": "http://arxiv.org/abs/2301.00001v1", "doi": "10.1101/x"}

                monkeypatch.setattr(provider, attr, fake)
                await tool(identifier)
                await tool(identifier, force_refresh=True)
                assert seen == [False, True], f"{tool.__name__} / {identifier}"

    @pytest.mark.asyncio
    async def test_the_batch_threads_it(self, monkeypatch):
        seen: list[bool] = []

        async def fake_batch(dois, *, force_refresh=False):
            seen.append(force_refresh)
            return {openalex.canonical_doi(d): {"id": "W1"} for d in dois}

        monkeypatch.setattr(openalex, "get_works_batch", fake_batch)
        await server.get_papers_metadata(identifiers=["10.1234/x"])
        await server.get_papers_metadata(identifiers=["10.1234/x"], force_refresh=True)
        assert seen == [False, True]

    @pytest.mark.asyncio
    async def test_the_follow_published_chain_threads_it(self, monkeypatch):
        """The inner lookup is the one most likely to be stale: the chain is
        followed precisely when a preprint has just acquired a journal DOI."""
        seen: list[bool] = []

        async def fake_biorxiv(doi, *, force_refresh=False):
            return {"doi": "10.1101/x", "published_doi": "10.1038/y"}

        async def fake_openalex(doi, *, force_refresh=False):
            seen.append(force_refresh)
            return {"id": "W1", "title": "Journal"}

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)
        monkeypatch.setattr(openalex, "get_work", fake_openalex)

        await server.get_paper_metadata("10.1101/x", follow_published=True)
        await server.get_paper_metadata("10.1101/x", follow_published=True, force_refresh=True)
        assert seen == [False, True]


# ---------------------------------------------------------------------------
# get_papers_metadata: the branches no example reached
# ---------------------------------------------------------------------------


class TestBatchUncoveredBranches:
    @pytest.mark.asyncio
    async def test_a_biorxiv_doi_routes_through_the_singleton_fan_out(self, monkeypatch):
        async def fake_biorxiv(doi, **kwargs):
            return {"doi": "10.1101/2024.01.01.123", "title": "Preprint", "server": "bioRxiv"}

        async def fake_batch(dois, **kwargs):
            raise AssertionError("a bioRxiv DOI must not reach the OpenAlex batch")

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)
        monkeypatch.setattr(openalex, "get_works_batch", fake_batch)

        result = await server.get_papers_metadata(identifiers=["10.1101/2024.01.01.123"])
        entry = result["papers"][0]
        assert entry["_source"] == "biorxiv"
        assert entry["_input"] == "10.1101/2024.01.01.123"
        assert entry["title"] == "Preprint"

    @pytest.mark.asyncio
    async def test_a_batch_entry_that_errored_is_enriched_not_dropped(self, monkeypatch):
        async def fake_batch(dois, **kwargs):
            return {
                "10.1234/ok": {"id": "W1", "title": "Fine"},
                "10.1234/bad": {"error": "No work found for DOI: 10.1234/bad", "not_found": True},
            }

        monkeypatch.setattr(openalex, "get_works_batch", fake_batch)

        result = await server.get_papers_metadata(identifiers=["10.1234/ok", "10.1234/bad"])
        ok, bad = result["papers"]
        assert ok["_source"] == "openalex"
        assert bad["_input"] == "10.1234/bad"
        assert bad["not_found"] is True
        assert bad["suggestion"] == paper._OPENALEX_METADATA_HINT

    @pytest.mark.asyncio
    async def test_the_enrichment_does_not_mutate_the_shared_batch_entry(self, monkeypatch):
        """Batch entries are not deep-copied the way `cache.cached_lookup`'s
        are, and two spellings of one DOI share a single entry across two
        slots. Enriching in place would write a suggestion into it."""
        shared = {"error": "No work found for DOI: 10.1234/x", "not_found": True}

        async def fake_batch(dois, **kwargs):
            return {"10.1234/x": shared}

        monkeypatch.setattr(openalex, "get_works_batch", fake_batch)

        await server.get_papers_metadata(identifiers=["10.1234/X", "doi:10.1234/x"])
        assert "suggestion" not in shared


# ---------------------------------------------------------------------------
# The per-source metadata formatters, field by field
# ---------------------------------------------------------------------------


class TestArxivPdfUrl:
    def test_picks_the_link_titled_pdf(self):
        links = [
            {"href": "http://arxiv.org/abs/2301.00001v1", "rel": "alternate"},
            {"href": "http://arxiv.org/pdf/2301.00001v1", "rel": "related", "title": "pdf"},
        ]
        assert paper._arxiv_pdf_url({"links": links}) == "http://arxiv.org/pdf/2301.00001v1"

    def test_no_pdf_link_is_none(self):
        assert paper._arxiv_pdf_url({"links": [{"href": "x", "rel": "alternate"}]}) is None

    def test_missing_links_is_none(self):
        assert paper._arxiv_pdf_url({}) is None


class TestFlatMetadataFormatters:
    """`_format_metadata_by_source` was asserted only on `_source`, so a
    mis-mapped field passed. The Crossref formatter has this coverage already.
    """

    def test_arxiv_field_mapping(self):
        out = paper._format_arxiv_metadata(
            {
                "id": "http://arxiv.org/abs/2301.00001v2",
                "title": "A Paper",
                "published": "2023-01-01T00:00:00Z",
                "updated": "2023-02-01T00:00:00Z",
                "primary_category": "cs.LG",
                "categories": ["cs.LG", "stat.ML"],
                "links": [{"title": "pdf", "href": "http://arxiv.org/pdf/2301.00001v2"}],
                "doi": "10.1038/x",
                "journal_ref": "Nature 615 (2023) 1-9",
                "comment": "9 pages",
            },
            "2301.00001v2",
        )
        assert out == {
            "_source": "arxiv",
            "_canonical_id": "2301.00001v2",
            "arxiv_id": "2301.00001v2",
            "title": "A Paper",
            "published": "2023-01-01T00:00:00Z",
            "updated": "2023-02-01T00:00:00Z",
            "primary_category": "cs.LG",
            "categories": ["cs.LG", "stat.ML"],
            "pdf_url": "http://arxiv.org/pdf/2301.00001v2",
            "doi": "10.1038/x",
            "journal_ref": "Nature 615 (2023) 1-9",
            "comment": "9 pages",
        }

    def test_biorxiv_field_mapping_omits_followed_published_by_default(self):
        out = paper._format_biorxiv_metadata(
            {
                "doi": "10.1101/2024.01.01.123",
                "title": "A Preprint",
                "date": "2024-01-01",
                "version": "2",
                "type": "new results",
                "category": "neuroscience",
                "license": "cc_by",
                "server": "bioRxiv",
                "published_doi": "10.1038/y",
                "pdf_url": "https://biorxiv.org/x.full.pdf",
            },
            "10.1101/2024.01.01.123",
        )
        assert out == {
            "_source": "biorxiv",
            "_canonical_id": "10.1101/2024.01.01.123",
            "doi": "10.1101/2024.01.01.123",
            "title": "A Preprint",
            "date": "2024-01-01",
            "version": "2",
            "type": "new results",
            "category": "neuroscience",
            "license": "cc_by",
            "server": "bioRxiv",
            "published_doi": "10.1038/y",
            "pdf_url": "https://biorxiv.org/x.full.pdf",
        }

    def test_openalex_metadata_surfaces_the_best_pdf_url(self):
        out = paper._format_openalex_metadata(
            {
                "id": "W1",
                "title": "A Work",
                "doi": "https://doi.org/10.1234/x",
                "publication_year": 2023,
                "publication_date": "2023-05-01",
                "type": "article",
                "language": "en",
                "primary_location": {"source": {"display_name": "Nature"}},
                "open_access": {"is_oa": True, "oa_status": "gold", "oa_url": "https://oa/x"},
                "best_oa_location": {"pdf_url": "https://oa/x.pdf"},
            },
            "10.1234/x",
        )
        assert out["venue"] == "Nature"
        assert out["is_oa"] is True and out["oa_status"] == "gold"
        assert out["oa_url"] == "https://oa/x"
        # Documented on the openalex branch of get_paper_metadata's response.
        assert out["pdf_url"] == "https://oa/x.pdf"


class TestUnknownIdentifierAcrossTheFamily:
    """An identifier no provider claims is answered by the tool, not by a
    provider — so the message names the two shapes that work and the two
    search tools that find one, rather than a provider-specific hint."""

    @pytest.mark.asyncio
    async def test_every_unified_tool_refuses_a_freeform_label(self):
        for tool in (
            server.get_paper_metadata,
            server.get_paper_authors,
            server.get_paper_abstract,
            server.get_paper_bibtex,
        ):
            result = await tool("my reading notes")
            assert "Cannot resolve paper provider" in result["error"], tool.__name__
            assert "search_arxiv" in result["error"]
            assert "_source" not in result, f"{tool.__name__} must not claim a source"

    @pytest.mark.asyncio
    async def test_the_batch_answers_it_per_input(self):
        result = await server.get_papers_metadata(identifiers=["my reading notes"])
        entry = result["papers"][0]
        assert entry["_input"] == "my reading notes"
        assert "Cannot resolve paper provider" in entry["error"]


class TestBatchResolutionDisagreement:
    """`get_papers_metadata` resolves each identifier twice — once to route it
    into the singleton fan-out, and again inside `_fetch_source`. The guard in
    `_singleton_one` is what stops a disagreement between the two from
    indexing `_METADATA_HINT_BY_SOURCE` with `None` and raising a KeyError out
    of the whole batch instead of answering the other papers.
    """

    @pytest.mark.asyncio
    async def test_a_second_opinion_of_none_is_answered_not_raised(self, monkeypatch):
        real = manual.resolve_metadata_source
        calls: list[str] = []

        def flaky(identifier):
            calls.append(identifier)
            # First call routes it to the singleton fan-out; the second, made
            # inside _fetch_source, no longer claims it.
            return None if calls.count(identifier) > 1 else real(identifier)

        monkeypatch.setattr(manual, "resolve_metadata_source", flaky)

        result = await server.get_papers_metadata(identifiers=["2301.00001"])
        entry = result["papers"][0]
        assert entry["_input"] == "2301.00001"
        assert "Cannot resolve paper provider" in entry["error"]
