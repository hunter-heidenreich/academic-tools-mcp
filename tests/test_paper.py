"""Tests for the unified paper tools in ``tools/paper.py``.

The provider-level tests cover the underlying clients; this file covers
the dispatch wired up at the @mcp.tool layer — routing on identifier
shape, the follow_published chain, and the per-source formatters.
"""

import pytest

from academic_tools_mcp import manual, server
from academic_tools_mcp.providers import arxiv, biorxiv, openalex
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
