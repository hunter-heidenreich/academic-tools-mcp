"""Tests for tool-layer behaviors that compose multiple modules.

The provider-level tests cover the underlying clients; this file covers
the smartness wired up at the @mcp.tool layer in server.py — chaining
across providers and the auto-source picker.
"""

import asyncio

import pytest

from academic_tools_mcp import arxiv, biorxiv, crossref, openalex, opencitations, server


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
        async def fake_biorxiv_get_paper(doi, **kwargs):
            return {
                "doi": "10.1101/2024.01.01.123",
                "title": "Preprint title",
                "published_doi": "10.1038/s41586-024-07000-0",
                "pdf_url": "https://example/pdf",
            }

        async def fake_openalex_get_work(doi, **kwargs):
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
        async def fake_biorxiv_get_paper(doi, **kwargs):
            return {
                "doi": "10.1101/unpub",
                "title": "Still preprint",
                "published_doi": None,
            }

        async def _no_openalex(doi, **kwargs):
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
        async def fake_biorxiv_get_paper(doi, **kwargs):
            return {
                "doi": "10.1101/2024.fresh",
                "title": "Fresh preprint",
                "published_doi": "10.1038/not-yet-indexed",
                "pdf_url": "https://example/pdf",
            }

        async def fake_openalex_get_work(doi, **kwargs):
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
                "entries": [{
                    "id": "http://arxiv.org/abs/2301.99999",
                    "title": "Authorless oddity",
                    "published": "2023-01-01T00:00:00Z",
                    "authors": [],
                }],
            }
        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        result = await server.search_arxiv("anything")
        assert result["results"][0]["author_count"] == 0
        assert result["results"][0]["first_author"] is None

    @pytest.mark.asyncio
    async def test_search_arxiv_response_shape_matches_crossref(self, monkeypatch):
        # search_arxiv and search_crossref_by_title must return the same
        # top-level shape so an agent can branch on the source without
        # feature-detecting field names.
        async def fake_search(query, max_results=10):
            return {"total_results": 0, "entries": []}
        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        result = await server.search_arxiv("anything")
        assert set(result.keys()) == {"total_results", "results"}

    @pytest.mark.asyncio
    async def test_search_crossref_by_title_includes_author_count(
        self, monkeypatch
    ):
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [{
                    "DOI": "10.1234/x",
                    "title": ["Some title"],
                    "author": [
                        {"given": "Jane", "family": "Doe"},
                        {"given": "John", "family": "Roe"},
                        {"given": "Alice", "family": "Smith"},
                    ],
                    "published-online": {"date-parts": [[2023]]},
                }],
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
                "items": [{
                    "DOI": "10.1234/x",
                    "title": ["No-author edge case"],
                    "published-online": {"date-parts": [[2023]]},
                }],
            }
        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["author_count"] == 0
        assert result["results"][0]["first_author"] is None


# ---------------------------------------------------------------------------
# get_paper_authors: response shape is symmetric across providers
# ---------------------------------------------------------------------------


class TestAuthorsShapeSymmetry:
    """Agents that paginate get_paper_authors expect the same keys
    regardless of which provider serves the paper. Earlier the
    page_institutions / page_institution_count fields only appeared on
    the OpenAlex branch, which forced agent code to feature-detect the
    shape mid-loop. They now appear on every branch (empty for arxiv /
    biorxiv where the upstream API doesn't carry institution rollups).
    """

    @pytest.mark.asyncio
    async def test_arxiv_branch_includes_empty_institution_fields(
        self, monkeypatch
    ):
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
    async def test_biorxiv_branch_includes_empty_institution_fields(
        self, monkeypatch
    ):
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
    async def test_arxiv_metadata_strips_version_and_lowercases(
        self, monkeypatch
    ):
        async def fake_arxiv(arxiv_id, **kwargs):
            return {
                "id": "http://arxiv.org/abs/2301.00001v3",
                "title": "x",
            }
        monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)

        # Caller passes the version-suffixed form; canonical strips it.
        result = await server.get_paper_metadata("2301.00001v3")
        assert result["_canonical_id"] == "2301.00001"

    @pytest.mark.asyncio
    async def test_openalex_metadata_lowercases_doi(self, monkeypatch):
        async def fake_openalex(doi, **kwargs):
            return {"title": "x", "doi": "https://doi.org/10.1038/X.2024.Y"}
        monkeypatch.setattr(openalex, "get_work", fake_openalex)

        # Mixed-case URL form normalises to lowercase bare DOI.
        result = await server.get_paper_metadata("https://doi.org/10.1038/X.2024.Y")
        assert result["_canonical_id"] == "10.1038/x.2024.y"

    @pytest.mark.asyncio
    async def test_canonical_id_present_across_paper_tool_family(
        self, monkeypatch
    ):
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
            assert result["_canonical_id"] == "2301.00001", (
                f"{tool.__name__} missing canonical id"
            )

    @pytest.mark.asyncio
    async def test_follow_published_canonical_is_journal_doi(
        self, monkeypatch
    ):
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

        result = await server.get_paper_metadata(
            "10.1101/2024.01.01.123", follow_published=True
        )
        assert result["_source"] == "openalex_via_biorxiv"
        assert result["_canonical_id"] == "10.1038/s41586-024-07000-0"
        assert result["preprint_doi"] == "10.1101/2024.01.01.123"


# ---------------------------------------------------------------------------
# get_paper_citations: forward-compatible source parameter
# ---------------------------------------------------------------------------


class TestCitationsSourceParam:
    """The `source` parameter is reserved so a future second source can
    ship without a breaking change. Both 'auto' and 'opencitations'
    dispatch identically today; pinning source='opencitations' is the
    forward-stable choice for code that always wants OpenCitations."""

    @pytest.mark.asyncio
    async def test_auto_dispatches_to_opencitations(self, monkeypatch):
        async def fake_oc(doi):
            return {"citations": [{"doi": "10.x/a"}], "count": 1}
        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        result = await server.get_paper_citations("10.x/x")
        assert result["_source"] == "opencitations"
        assert result["total"] == 1

    @pytest.mark.asyncio
    async def test_explicit_opencitations_matches_auto(self, monkeypatch):
        async def fake_oc(doi):
            return {"citations": [{"doi": "10.x/a"}], "count": 1}
        monkeypatch.setattr(opencitations, "get_citations", fake_oc)

        auto = await server.get_paper_citations("10.x/x", source="auto")
        explicit = await server.get_paper_citations(
            "10.x/x", source="opencitations"
        )
        # Same source dispatch, same response shape — pinning the param
        # is a no-op today and stays no-op as long as OpenCitations is
        # the only provider.
        assert auto == explicit


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
