"""Tests for the unified paper tools in ``tools/paper.py``.

The provider-level tests cover the underlying clients; this file covers what
the @mcp.tool layer adds on top: routing on identifier shape, the
``follow_published`` chain and its retry verdict, the per-source metadata
formatters, author pagination and the shape symmetry that lets a paginating
agent skip feature-detection, ``get_author``, and the batch branches of
``get_papers_metadata``.
"""

from typing import ClassVar

import pytest

from academic_tools_mcp import manual, server
from academic_tools_mcp.providers import acl, arxiv, biorxiv, crossref, openalex
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
            # Shape of a transient error from http — retryable, no not_found.
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
        monkeypatch.setattr(acl, "get_paper", fake_error)
        monkeypatch.setattr(openalex, "get_work", fake_error)

        cases = [
            ("2301.00001", paper._ARXIV_METADATA_HINT),
            ("10.1101/2024.01.01.123", paper._BIORXIV_METADATA_HINT),
            ("P16-1160", paper._ACL_METADATA_HINT),
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
    work. `http`'s vocabulary is three-state — transient carries
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

        from academic_tools_mcp.net import http

        request = httpx.Request("GET", "https://api.openalex.org/works/doi:10.1038/x")
        err = http.error_dict(
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
        """The coupling between `http`'s constructors and this one branch.

        Nothing else links them, so a new classification key — or a change to
        which errors carry `retryable` — would silently re-flavour the tag.
        The tag must appear for exactly the errors `http` calls retryable.
        """
        import httpx

        from academic_tools_mcp.net import http

        request = httpx.Request("GET", "https://api.openalex.org/works/doi:10.1038/x")

        def status_error(code, headers=None):
            return httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(code, request=request, headers=headers or {}),
            )

        produced = [
            http.not_found("gone"),
            http.parse_error_dict("OpenAlex"),
            http.error_dict(
                "OpenAlex", http.LocalBackpressureError("OpenAlex", pending=5, max_pending=5)
            ),
            http.error_dict("OpenAlex", status_error(429, {"Retry-After": "7"})),
            http.error_dict("OpenAlex", status_error(503)),
            http.error_dict("OpenAlex", status_error(418)),
            http.error_dict("OpenAlex", status_error(403)),
            http.error_dict("OpenAlex", httpx.TimeoutException("slow", request=request)),
            http.error_dict("OpenAlex", httpx.RequestError("dns", request=request)),
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
                "published_journal": "Nature",
                "published_date": "2024-06-01",
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
            "published_journal": "Nature",
            "published_date": "2024-06-01",
            "funding": [],
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

        # bioRxiv: still a singleton, so still resolved twice. arXiv now batches.
        result = await server.get_papers_metadata(identifiers=["10.1101/2024.01.01.123"])
        entry = result["papers"][0]
        assert entry["_input"] == "10.1101/2024.01.01.123"
        assert "Cannot resolve paper provider" in entry["error"]


# ---------------------------------------------------------------------------
# PMID resolution: one identity per paper, across every tool
# ---------------------------------------------------------------------------


def _stub_pmid(monkeypatch, mapping, *, work=None):
    """Route ``resolve_pmid`` through *mapping* and answer ``get_work`` with *work*.

    Patches the provider module object, which every importer shares — the seam
    ``.claude/rules/server.md`` names, not a wrapper in ``app``.
    """
    calls: list[str] = []

    async def fake_resolve_pmid(pmid, **kwargs):
        calls.append(pmid)
        return mapping

    async def fake_get_work(doi, **kwargs):
        return dict(work or {}, doi=f"https://doi.org/{doi}")

    monkeypatch.setattr(openalex, "resolve_pmid", fake_resolve_pmid)
    monkeypatch.setattr(openalex, "get_work", fake_get_work)
    return calls


class TestPmidRouting:
    """A PMID is traded for its DOI before dispatch, so the two spellings of one
    paper cannot acquire two cache identities or two ``_canonical_id`` values."""

    @pytest.mark.asyncio
    async def test_metadata_answers_under_the_doi(self, monkeypatch):
        _stub_pmid(monkeypatch, {"doi": "10.1234/x", "openalex_id": "https://openalex.org/W1"})

        result = await server.get_paper_metadata("pmid:20079334")

        assert result["_source"] == "openalex"
        assert result["_canonical_id"] == "10.1234/x"

    @pytest.mark.asyncio
    async def test_pmid_and_doi_agree_on_canonical_id(self, monkeypatch):
        _stub_pmid(monkeypatch, {"doi": "10.1234/x", "openalex_id": "https://openalex.org/W1"})

        via_pmid = await server.get_paper_metadata("20079334")
        via_doi = await server.get_paper_metadata("10.1234/x")

        assert via_pmid["_canonical_id"] == via_doi["_canonical_id"]

    @pytest.mark.asyncio
    async def test_metadata_surfaces_the_pmid_it_accepts(self, monkeypatch):
        _stub_pmid(
            monkeypatch,
            {"doi": "10.1234/x", "openalex_id": "https://openalex.org/W1"},
            work={"ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/20079334"}},
        )

        result = await server.get_paper_metadata("10.1234/x")

        assert result["pmid"] == "20079334"

    @pytest.mark.asyncio
    async def test_metadata_pmid_is_null_when_openalex_has_none(self, monkeypatch):
        _stub_pmid(monkeypatch, {"doi": "10.1234/x", "openalex_id": "https://openalex.org/W1"})

        result = await server.get_paper_metadata("10.1234/x")

        assert result["pmid"] is None

    @pytest.mark.asyncio
    async def test_a_doi_never_costs_a_pmid_lookup(self, monkeypatch):
        calls = _stub_pmid(monkeypatch, {"doi": "10.1234/x", "openalex_id": "W1"})

        async def fake_arxiv(arxiv_id, **kwargs):
            return {"title": "T", "links": []}

        monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)

        await server.get_paper_metadata("10.1234/x")
        await server.get_paper_metadata("2301.00001v1")

        assert calls == []

    @pytest.mark.asyncio
    async def test_unresolvable_pmid_carries_a_suggestion(self, monkeypatch):
        _stub_pmid(monkeypatch, {"error": "No work found for PMID: 99999999", "not_found": True})

        result = await server.get_paper_metadata("pmid:99999999")

        assert result.get("not_found") is True
        assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_pmid_without_a_doi_is_an_error_not_an_invented_key(self, monkeypatch):
        """These tools are DOI- or arXiv-keyed. Naming the OpenAlex id beats
        minting a cache identity the paper would then be stuck with."""
        _stub_pmid(monkeypatch, {"doi": None, "openalex_id": "https://openalex.org/W9"})

        result = await server.get_paper_metadata("pmid:20079334")

        assert result.get("not_found") is True
        assert "W9" in result["error"]
        assert "import_paper" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_batch_routes_a_pmid_and_echoes_the_input_spelling(self, monkeypatch):
        """``_input`` is the caller's string, not the DOI it resolved to —
        otherwise an agent cannot correlate the response back to its request."""
        _stub_pmid(monkeypatch, {"doi": "10.1234/x", "openalex_id": "W1"})

        async def fake_batch(dois, **kwargs):
            return {d: {"id": "W1", "doi": f"https://doi.org/{d}", "title": "T"} for d in dois}

        monkeypatch.setattr(openalex, "get_works_batch", fake_batch)

        result = await server.get_papers_metadata(["pmid:20079334"])

        assert result["papers"][0]["_input"] == "pmid:20079334"
        assert result["papers"][0]["_canonical_id"] == "10.1234/x"

    @pytest.mark.asyncio
    async def test_batch_reports_an_unresolvable_pmid_in_its_own_slot(self, monkeypatch):
        """One bad identifier must not affect the others — the batch contract."""
        _stub_pmid(monkeypatch, {"error": "No work found for PMID: 99999999", "not_found": True})

        async def fake_batch(dois, **kwargs):
            return {d: {"id": "W1", "doi": f"https://doi.org/{d}", "title": "T"} for d in dois}

        monkeypatch.setattr(openalex, "get_works_batch", fake_batch)

        result = await server.get_papers_metadata(["pmid:99999999", "10.1234/x"])

        bad, good = result["papers"]
        assert bad["_input"] == "pmid:99999999"
        assert bad["not_found"] is True
        assert good["_canonical_id"] == "10.1234/x"

    @pytest.mark.asyncio
    async def test_authors_abstract_and_bibtex_take_a_pmid_too(self, monkeypatch):
        """All four unified tools reach a provider through ``_fetch_source``,
        which is what makes the substitution uniform rather than per-tool."""
        _stub_pmid(
            monkeypatch,
            {"doi": "10.1234/x", "openalex_id": "W1"},
            work={
                "id": "https://openalex.org/W1",
                "title": "A Great Work",
                "publication_year": 2020,
                "type": "article",
                "authorships": [{"author": {"display_name": "Ada Lovelace", "id": "A1"}}],
                "abstract_inverted_index": {"Hello": [0], "world": [1]},
            },
        )

        authors = await server.get_paper_authors("pmid:20079334")
        abstract = await server.get_paper_abstract("pmid:20079334")
        bibtex = await server.get_paper_bibtex("pmid:20079334")

        assert authors["authors"][0]["name"] == "Ada Lovelace"
        assert abstract["abstract"] == "Hello world"
        assert "Lovelace" in bibtex["bibtex"]
        assert authors["_canonical_id"] == abstract["_canonical_id"] == "10.1234/x"


class TestCitedByCountReachesEveryOpenalexPath:
    """The batch closure bypasses the dispatcher, so a field can reach one path only."""

    WORK: ClassVar[dict] = {"id": "W1", "doi": "https://doi.org/10.1234/x", "cited_by_count": 84352}

    @pytest.mark.asyncio
    async def test_the_single_tool_carries_it(self, monkeypatch):
        async def fake(doi, **kwargs):
            return self.WORK

        monkeypatch.setattr(openalex, "get_work", fake)

        assert (await server.get_paper_metadata("10.1234/x"))["cited_by_count"] == 84352

    @pytest.mark.asyncio
    async def test_the_batch_carries_it(self, monkeypatch):
        async def fake_batch(dois, *, force_refresh=False):
            return {openalex.canonical_doi(d): self.WORK for d in dois}

        monkeypatch.setattr(openalex, "get_works_batch", fake_batch)

        papers = (await server.get_papers_metadata(identifiers=["10.1234/x"]))["papers"]
        assert papers[0]["cited_by_count"] == 84352

    @pytest.mark.asyncio
    async def test_the_followed_journal_version_carries_it(self, monkeypatch):
        async def fake_biorxiv(doi, **kwargs):
            return {"doi": "10.1101/x", "published_doi": "10.1234/x"}

        async def fake_openalex(doi, **kwargs):
            return self.WORK

        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)
        monkeypatch.setattr(openalex, "get_work", fake_openalex)

        result = await server.get_paper_metadata("10.1101/x", follow_published=True)
        assert result["_source"] == "openalex_via_biorxiv"
        assert result["cited_by_count"] == 84352

    @pytest.mark.asyncio
    async def test_a_work_without_the_field_reports_null(self, monkeypatch):
        async def fake(doi, **kwargs):
            return {"id": "W1"}

        monkeypatch.setattr(openalex, "get_work", fake)

        assert (await server.get_paper_metadata("10.1234/x"))["cited_by_count"] is None

    @pytest.mark.asyncio
    async def test_the_three_paths_agree_on_the_key_set(self, monkeypatch):
        """The symmetry the batch closure can silently break."""

        async def fake_work(doi, **kwargs):
            return self.WORK

        async def fake_batch(dois, *, force_refresh=False):
            return {openalex.canonical_doi(d): self.WORK for d in dois}

        async def fake_biorxiv(doi, **kwargs):
            return {"doi": "10.1101/x", "published_doi": "10.1234/x"}

        monkeypatch.setattr(openalex, "get_work", fake_work)
        monkeypatch.setattr(openalex, "get_works_batch", fake_batch)
        monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)

        single = await server.get_paper_metadata("10.1234/x")
        batched = (await server.get_papers_metadata(identifiers=["10.1234/x"]))["papers"][0]
        followed = await server.get_paper_metadata("10.1101/x", follow_published=True)

        assert set(single) == set(batched) - {"_input"}
        assert set(single) <= set(followed)


# ---------------------------------------------------------------------------
# ACL Anthology
# ---------------------------------------------------------------------------


def _acl_record(**overrides):
    record = {
        "anthology_id": "W04-1013",
        "title": "ROUGE: A Package for Automatic Evaluation of Summaries",
        "authors": [
            {
                "name": "Chin-Yew Lin",
                "first": "Chin-Yew",
                "last": "Lin",
                "orcid": None,
                "affiliation": "ISI",
            }
        ],
        "abstract": "An abstract.",
        "booktitle": "Text Summarization Branches Out",
        "editors": [],
        "volume_type": "proceedings",
        "journal_volume": None,
        "journal_issue": None,
        "venues": ["ws"],
        "publisher": "Association for Computational Linguistics",
        "address": "Barcelona, Spain",
        "month": "July",
        "year": "2004",
        "pages": "74–81",
        "doi": None,
        "bibkey": "lin-2004-rouge",
        "url": "https://aclanthology.org/W04-1013/",
        "pdf_url": "https://aclanthology.org/W04-1013.pdf",
    }
    record.update(overrides)
    return record


class TestAclAnthologySource:
    @pytest.fixture(autouse=True)
    def _acl(self, monkeypatch):
        seen: list[str] = []

        async def fake_get_paper(identifier, **kwargs):
            seen.append(identifier)
            return _acl_record()

        async def no_openalex(*args, **kwargs):
            raise AssertionError("an Anthology paper must not reach OpenAlex")

        monkeypatch.setattr(acl, "get_paper", fake_get_paper)
        monkeypatch.setattr(openalex, "get_work", no_openalex)
        monkeypatch.setattr(openalex, "get_works_batch", no_openalex)
        return seen

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "identifier", ["W04-1013", "https://aclanthology.org/W04-1013/", "10.18653/v1/w04-1013"]
    )
    async def test_metadata_echoes_the_anthology_id(self, identifier):
        result = await server.get_paper_metadata(identifier)

        assert result["_source"] == "acl_anthology"
        assert result["_canonical_id"] == "W04-1013"
        assert result["booktitle"] == "Text Summarization Branches Out"
        assert result["doi"] is None

    @pytest.mark.asyncio
    async def test_metadata_carries_the_journal_fields(self):
        result = await server.get_paper_metadata("W04-1013")

        assert (result["volume_type"], result["journal_volume"], result["journal_issue"]) == (
            "proceedings",
            None,
            None,
        )

    @pytest.mark.asyncio
    async def test_authors_keep_the_symmetric_shape(self):
        result = await server.get_paper_authors("W04-1013")

        assert result["_source"] == "acl_anthology"
        assert result["authors"][0]["affiliation"] == "ISI"
        assert (result["page_institutions"], result["page_institution_count"]) == ([], 0)

    @pytest.mark.asyncio
    async def test_abstract(self):
        result = await server.get_paper_abstract("W04-1013")

        assert (result["_source"], result["abstract"]) == ("acl_anthology", "An abstract.")

    @pytest.mark.asyncio
    async def test_bibtex_is_the_anthology_generator(self):
        result = await server.get_paper_bibtex("W04-1013")

        assert result["bibtex"].startswith("@inproceedings{lin2004rouge,")
        assert "booktitle={Text Summarization Branches Out}" in result["bibtex"]

    @pytest.mark.asyncio
    async def test_batch_fetches_anthology_papers_as_singletons(self):
        result = await server.get_papers_metadata(["W04-1013"])

        assert result["papers"][0]["_source"] == "acl_anthology"
        assert result["papers"][0]["_input"] == "W04-1013"

    @pytest.mark.asyncio
    async def test_fallback_crossref_never_applies(self, monkeypatch):
        async def missing(identifier, **kwargs):
            return {"error": "No ACL Anthology paper: W04-1013", "not_found": True}

        monkeypatch.setattr(acl, "get_paper", missing)

        result = await server.get_paper_metadata("W04-1013", fallback_crossref=True)

        assert result["suggestion"] == paper._ACL_METADATA_HINT
        assert "crossref_fallback_retryable" not in result

    @pytest.mark.asyncio
    async def test_a_hosted_doi_answers_from_the_anthology(self, monkeypatch, _acl):
        async def hosted(doi):
            return "2026.tacl-1.1" if doi.endswith("10.1162/tacl.a.63") else None

        monkeypatch.setattr(acl, "anthology_id_for_doi", hosted)

        result = await server.get_paper_metadata("https://doi.org/10.1162/tacl.a.63")

        assert result["_source"] == "acl_anthology"
        assert result["_canonical_id"] == "2026.tacl-1.1"
        assert _acl == ["2026.tacl-1.1"]

    @pytest.mark.asyncio
    async def test_a_claimed_doi_never_asks_the_index(self, monkeypatch):
        async def boom(doi):
            raise AssertionError("a routed DOI must not consult the index")

        monkeypatch.setattr(acl, "anthology_id_for_doi", boom)

        assert (await server.get_paper_metadata("10.18653/v1/W04-1013"))["_source"] == (
            "acl_anthology"
        )


# ---------------------------------------------------------------------------
# get_papers_metadata: arXiv ids batch into id_list calls
# ---------------------------------------------------------------------------


class TestArxivBatchThroughTheTool:
    @pytest.mark.asyncio
    async def test_twenty_cold_arxiv_ids_are_one_request_and_no_backpressure(self, monkeypatch):
        """Regression: each id was its own request. At arXiv's real pacing the fan-out
        queued past the throttle's burst cap and came back as backpressure errors."""
        from academic_tools_mcp.net import clients

        ids = [f"2301.{n:05d}" for n in range(20)]
        entries = "".join(
            f"<entry><id>http://arxiv.org/abs/{i}v1</id><title>T {i}</title></entry>" for i in ids
        )
        feed = (
            '<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
            f"<opensearch:totalResults>20</opensearch:totalResults>{entries}</feed>"
        )
        requests: list[dict] = []

        class StubResponse:
            status_code = 200
            text = feed
            headers: ClassVar[dict[str, str]] = {}

            def raise_for_status(self):
                pass

        class StubClient:
            async def get(self, url, **kwargs):
                requests.append(kwargs.get("params") or {})
                return StubResponse()

        monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())
        monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)

        result = await server.get_papers_metadata(identifiers=ids)

        assert len(requests) == 1
        papers = result["papers"]
        assert [p["_input"] for p in papers] == ids
        assert all(p["_source"] == "arxiv" and "error" not in p for p in papers)
        assert papers[3]["title"] == f"T {ids[3]}"


# ---------------------------------------------------------------------------
# get_paper_versions: arXiv license and revision history
# ---------------------------------------------------------------------------


class TestGetPaperVersions:
    _RECORD: ClassVar[dict] = {
        "arxiv_id": "1706.03762",
        "submitter": "Llion Jones",
        "license": "http://arxiv.org/licenses/nonexclusive-distrib/1.0/",
        "versions": [
            {"version": "v1", "date": "2017-06-12T17:57:34Z", "size": "1102kb"},
            {"version": "v2", "date": "2017-06-19T16:49:45Z", "size": "1124kb"},
        ],
    }

    @pytest.mark.asyncio
    async def test_the_record_is_returned_with_its_count(self, monkeypatch):
        seen = []

        async def fake_versions(arxiv_id, *, force_refresh=False):
            seen.append(arxiv_id)
            return self._RECORD

        monkeypatch.setattr(arxiv, "get_versions", fake_versions)

        result = await server.get_paper_versions("arXiv:1706.03762v2")

        assert result == {
            "_source": "arxiv",
            "_canonical_id": "1706.03762",
            **self._RECORD,
            "version_count": 2,
        }
        assert seen == ["arXiv:1706.03762v2"]

    @pytest.mark.parametrize("identifier", ["10.1038/nature12373", "pmid:20079334", "P16-1160"])
    @pytest.mark.asyncio
    async def test_a_non_preprint_identifier_is_refused_without_a_request(
        self, monkeypatch, identifier
    ):
        async def no_request(*args, **kwargs):
            raise AssertionError("a non-preprint identifier must not reach a provider")

        monkeypatch.setattr(arxiv, "get_versions", no_request)
        monkeypatch.setattr(biorxiv, "get_paper", no_request)

        result = await server.get_paper_versions(identifier)

        assert "Not an arXiv ID or bioRxiv/medRxiv DOI" in result["error"]
        assert "search_arxiv" in result["suggestion"]
        # Unflagged would read as "unknown", not "can never work".
        assert result["not_found"] is True
        assert "retryable" not in result

    @pytest.mark.asyncio
    async def test_a_provider_error_keeps_its_verdict_and_gains_a_suggestion(self, monkeypatch):
        async def fake_versions(arxiv_id, *, force_refresh=False):
            return {"error": "No paper found for arXiv ID: 2301.99999", "not_found": True}

        monkeypatch.setattr(arxiv, "get_versions", fake_versions)

        result = await server.get_paper_versions("2301.99999")

        assert result["not_found"] is True
        assert result["suggestion"] == paper._ARXIV_METADATA_HINT

    @pytest.mark.asyncio
    async def test_a_biorxiv_doi_reads_the_details_record(self, monkeypatch):
        seen = []

        async def fake_get_paper(doi, *, force_refresh=False):
            seen.append((doi, force_refresh))
            return {
                "doi": "10.1101/2024.01.01.573838",
                "server": "biorxiv",
                "license": "cc_by",
                "versions": [
                    {"version": "1", "date": "2024-01-02", "license": "cc_no"},
                    {"version": "2", "date": "2024-03-04", "license": "cc_by"},
                ],
            }

        async def no_arxiv(*args, **kwargs):
            raise AssertionError("a bioRxiv DOI must not reach arXiv")

        monkeypatch.setattr(biorxiv, "get_paper", fake_get_paper)
        monkeypatch.setattr(arxiv, "get_versions", no_arxiv)

        result = await server.get_paper_versions(
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v2", force_refresh=True
        )

        assert result == {
            "_source": "biorxiv",
            "_canonical_id": "10.1101/2024.01.01.573838",
            "doi": "10.1101/2024.01.01.573838",
            "server": "biorxiv",
            "submitter": None,
            "license": "cc_by",
            "versions": [
                {"version": "1", "date": "2024-01-02", "license": "cc_no"},
                {"version": "2", "date": "2024-03-04", "license": "cc_by"},
            ],
            "version_count": 2,
        }
        assert seen[0][1] is True

    @pytest.mark.asyncio
    async def test_a_record_cached_before_versions_existed_reports_none(self, monkeypatch):
        async def fake_get_paper(doi, *, force_refresh=False):
            return {"doi": doi, "server": "biorxiv", "license": "cc_by"}

        monkeypatch.setattr(biorxiv, "get_paper", fake_get_paper)

        result = await server.get_paper_versions("10.1101/2024.01.01.573838")

        assert result["versions"] == []
        assert result["version_count"] == 0

    @pytest.mark.asyncio
    async def test_a_biorxiv_error_gains_the_biorxiv_hint(self, monkeypatch):
        async def fake_get_paper(doi, *, force_refresh=False):
            return {
                "error": "bioRxiv returned a response that could not be parsed.",
                "retryable": True,
            }

        monkeypatch.setattr(biorxiv, "get_paper", fake_get_paper)

        result = await server.get_paper_versions("10.1101/2024.01.01.573838")

        assert result["retryable"] is True
        assert result["suggestion"] == paper._BIORXIV_METADATA_HINT


# ---------------------------------------------------------------------------
# get_paper_metadata: follow_published for arXiv
# ---------------------------------------------------------------------------


class TestFollowPublishedArxiv:
    _ENTRY: ClassVar[dict] = {
        "id": "http://arxiv.org/abs/hep-th/9901001v3",
        "title": "String Junctions (preprint)",
        "doi": "10.1143/PTP.101.1155",
        "journal_ref": "Prog.Theor.Phys.101:1155-1164,1999",
        "links": [],
    }

    def _serve(self, monkeypatch, entry, work):
        requested = []

        async def fake_arxiv(ident, **kwargs):
            return entry

        async def fake_work(doi, **kwargs):
            requested.append(doi)
            return work

        monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)
        monkeypatch.setattr(openalex, "get_work", fake_work)
        return requested

    @pytest.mark.asyncio
    async def test_a_recorded_doi_chains_to_the_journal_record(self, monkeypatch):
        requested = self._serve(
            monkeypatch, self._ENTRY, {"title": "String Junctions", "doi": "10.1143/ptp.101.1155"}
        )

        result = await server.get_paper_metadata("hep-th/9901001", follow_published=True)

        assert requested == ["10.1143/PTP.101.1155"]
        assert result["_source"] == "openalex_via_arxiv"
        assert result["_canonical_id"] == "10.1143/ptp.101.1155"
        assert result["title"] == "String Junctions"
        assert result["preprint_arxiv_id"] == "hep-th/9901001v3"
        assert result["followed_published"] is True

    @pytest.mark.asyncio
    async def test_without_follow_published_openalex_is_never_asked(self, monkeypatch):
        requested = self._serve(monkeypatch, self._ENTRY, {"title": "unused"})

        result = await server.get_paper_metadata("hep-th/9901001")

        assert requested == []
        assert result["_source"] == "arxiv"
        assert "followed_published" not in result

    @pytest.mark.asyncio
    async def test_no_recorded_doi_returns_the_preprint_unmarked(self, monkeypatch):
        requested = self._serve(monkeypatch, {**self._ENTRY, "doi": None}, {"title": "unused"})

        result = await server.get_paper_metadata("hep-th/9901001", follow_published=True)

        assert requested == []
        assert result["_source"] == "arxiv"
        assert "followed_published" not in result

    @pytest.mark.asyncio
    async def test_the_first_of_several_recorded_dois_is_followed(self, monkeypatch):
        requested = self._serve(
            monkeypatch,
            {**self._ENTRY, "doi": "10.1143/PTP.101.1155 10.1143/PTP.101.9999"},
            {"title": "String Junctions"},
        )

        await server.get_paper_metadata("hep-th/9901001", follow_published=True)

        assert requested == ["10.1143/PTP.101.1155"]

    @pytest.mark.parametrize(
        ("work", "retryable"),
        [
            ({"error": "No work found", "not_found": True}, False),
            ({"error": "OpenAlex server error (HTTP 503).", "retryable": True}, True),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_openalex_miss_falls_back_to_the_marked_preprint(
        self, monkeypatch, work, retryable
    ):
        self._serve(monkeypatch, self._ENTRY, work)

        result = await server.get_paper_metadata("hep-th/9901001", follow_published=True)

        assert result["_source"] == "arxiv"
        assert result["title"] == "String Junctions (preprint)"
        assert result["followed_published"] is False
        assert result.get("published_lookup_retryable", False) is retryable


# ---------------------------------------------------------------------------
# get_paper_updates
# ---------------------------------------------------------------------------


_RETRACTED_WORK = {
    "DOI": "10.1016/s0140-6736(97)11096-0",
    "updated-by": [
        {
            "DOI": "10.1016/s0140-6736(04)15715-2",
            "type": "correction",
            "label": "Correction",
            "source": "retraction-watch",
            "updated": {"date-parts": [[2004, 3, 6]]},
        },
        {
            "DOI": "10.1016/s0140-6736(10)60175-4",
            "type": "retraction",
            "label": "Retraction",
            "source": "retraction-watch",
            "updated": {"date-parts": [[2010, 2, 6]]},
        },
    ],
}


def _stub_work(monkeypatch, work):
    """Answer every get_work with ``work``, recording the DOIs asked for."""
    seen: list[str] = []

    async def fake_get_work(doi, **kwargs):
        seen.append(doi)
        return work

    monkeypatch.setattr(crossref, "get_work", fake_get_work)
    return seen


class TestGetPaperUpdates:
    @pytest.mark.asyncio
    async def test_a_retracted_paper_says_so(self, monkeypatch):
        _stub_work(monkeypatch, _RETRACTED_WORK)

        result = await server.get_paper_updates("10.1016/S0140-6736(97)11096-0")

        assert result["retracted"] is True
        assert [u["type"] for u in result["updates"]] == ["correction", "retraction"]

    @pytest.mark.asyncio
    async def test_the_notice_carries_its_date_and_source(self, monkeypatch):
        _stub_work(monkeypatch, _RETRACTED_WORK)

        retraction = (await server.get_paper_updates("10.1234/x"))["updates"][1]

        assert retraction["doi"] == "10.1016/s0140-6736(10)60175-4"
        assert retraction["year"] == 2010
        assert retraction["date"] == "2010-02-06"
        assert retraction["source"] == "retraction-watch"
        assert retraction["label"] == "Retraction"
        # The raw date-parts are an implementation detail of the provider reader.
        assert "updated" not in retraction

    @pytest.mark.asyncio
    async def test_a_correction_alone_is_not_a_retraction(self, monkeypatch):
        """`retracted` keys on the update type, not on there being any notice."""
        _stub_work(monkeypatch, {"updated-by": [_RETRACTED_WORK["updated-by"][0]]})

        result = await server.get_paper_updates("10.1234/x")

        assert result["retracted"] is False
        assert len(result["updates"]) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "update_type", ["retraction", "partial_retraction", "withdrawal", "removal"]
    )
    async def test_every_withdrawing_type_sets_the_flag(self, monkeypatch, update_type):
        """Crossmark spells the class four ways and all four are in live data. Keying
        on `retraction` alone answered `retracted: false` for a withdrawn paper — a
        clean bill of health for the one thing this tool exists to catch.
        """
        _stub_work(monkeypatch, {"updated-by": [{"DOI": "10.1234/n", "type": update_type}]})

        assert (await server.get_paper_updates("10.1234/x"))["retracted"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "update_type",
        ["correction", "corrigendum", "erratum", "expression_of_concern", "new_version"],
    )
    async def test_an_amending_type_is_reported_without_the_flag(self, monkeypatch, update_type):
        """The other half of the split: an erratum is not a withdrawal, and an agent
        that read the flag as "any notice" would discard a corrected paper.
        """
        _stub_work(monkeypatch, {"updated-by": [{"DOI": "10.1234/n", "type": update_type}]})

        result = await server.get_paper_updates("10.1234/x")

        assert result["retracted"] is False
        assert [u["type"] for u in result["updates"]] == [update_type]

    @pytest.mark.asyncio
    async def test_a_clean_paper_emits_the_empty_shapes(self, monkeypatch):
        """Symmetric keys: a paginating agent never feature-detects."""
        _stub_work(monkeypatch, {"DOI": "10.1234/x"})

        result = await server.get_paper_updates("10.1234/x")

        assert result["retracted"] is False
        assert result["updates"] == []
        assert result["relations"] == {}

    @pytest.mark.asyncio
    async def test_relations_are_reported(self, monkeypatch):
        _stub_work(
            monkeypatch,
            {"relation": {"is-preprint-of": [{"id-type": "doi", "id": "10.1364/OE.572415"}]}},
        )

        result = await server.get_paper_updates("10.1234/x")

        assert result["relations"] == {"is-preprint-of": ["10.1364/oe.572415"]}

    @pytest.mark.asyncio
    async def test_the_echoed_doi_is_canonical(self, monkeypatch):
        """So every spelling of one paper correlates to a single value."""
        _stub_work(monkeypatch, {"DOI": "10.1234/x"})

        result = await server.get_paper_updates("https://doi.org/10.1234/X")

        assert result["doi"] == "10.1234/x"

    @pytest.mark.asyncio
    async def test_a_non_doi_is_refused_without_a_request(self, monkeypatch):
        seen = _stub_work(monkeypatch, {"DOI": "10.1234/x"})

        result = await server.get_paper_updates("2301.00001")

        assert result["not_found"] is True
        assert "suggestion" in result
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_pmid_trades_for_its_doi_first(self, monkeypatch):
        async def fake_resolve_pmid(pmid, **kwargs):
            return {"doi": "10.1234/traded", "openalex_id": "W1"}

        monkeypatch.setattr(openalex, "resolve_pmid", fake_resolve_pmid)
        seen = _stub_work(monkeypatch, {"DOI": "10.1234/traded"})

        result = await server.get_paper_updates("12345678")

        assert seen == ["10.1234/traded"]
        assert result["doi"] == "10.1234/traded"

    @pytest.mark.asyncio
    async def test_a_provider_error_keeps_its_verdict_and_gains_a_hint(self, monkeypatch):
        _stub_work(monkeypatch, {"error": "No work found on Crossref", "not_found": True})

        result = await server.get_paper_updates("10.1234/missing")

        assert result["not_found"] is True
        assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_force_refresh_passthrough(self, monkeypatch):
        seen: list[bool] = []

        async def fake_get_work(doi, **kwargs):
            seen.append(kwargs.get("force_refresh"))
            return {"DOI": doi}

        monkeypatch.setattr(crossref, "get_work", fake_get_work)

        await server.get_paper_updates("10.1234/x")
        await server.get_paper_updates("10.1234/x", force_refresh=True)

        assert seen == [False, True]


class TestGetInstitutionShape:
    @staticmethod
    def _institution(**over):
        return {
            "id": "https://openalex.org/I27837315",
            "display_name": "University of Michigan",
            "ror": "https://ror.org/00jmfr291",
            "country_code": "US",
            "type": "education",
            "homepage_url": "https://www.umich.edu",
            "works_count": 993484,
            "cited_by_count": 67912675,
            "summary_stats": {"h_index": 1200, "i10_index": 400000},
            "geo": {"city": "Ann Arbor", "region": "Michigan"},
            "display_name_acronyms": ["UMich"],
            "display_name_alternatives": ["University of Michigan–Ann Arbor"],
            "lineage": ["https://openalex.org/I27837315"],
            **over,
        }

    def _stub(self, monkeypatch, record):
        async def fake(institution_id, **kwargs):
            return record

        monkeypatch.setattr(openalex, "get_institution", fake)

    @pytest.mark.asyncio
    async def test_projects_the_lean_slice(self, monkeypatch):
        self._stub(monkeypatch, self._institution())

        result = await server.get_institution("I27837315")

        assert result["name"] == "University of Michigan"
        assert result["ror"] == "https://ror.org/00jmfr291"
        assert result["city"] == "Ann Arbor"
        assert result["h_index"] == 1200
        assert result["alternative_names"] == ["UMich", "University of Michigan–Ann Arbor"]
        # The raw record is megabytes of counts_by_year / topic_share; none of it leaks.
        assert "counts_by_year" not in result
        assert "topic_share" not in result

    @pytest.mark.asyncio
    async def test_lineage_excludes_the_institution_itself(self, monkeypatch):
        """Lineage includes self; what an agent wants is the university above a campus."""
        self._stub(
            monkeypatch,
            self._institution(
                id="https://openalex.org/I999",
                lineage=["https://openalex.org/I999", "https://openalex.org/I27837315"],
            ),
        )

        result = await server.get_institution("I999")

        assert result["parent_institutions"] == ["https://openalex.org/I27837315"]

    @pytest.mark.asyncio
    async def test_a_sparse_record_nulls_rather_than_raises(self, monkeypatch):
        self._stub(monkeypatch, {"id": "https://openalex.org/I1"})

        result = await server.get_institution("I1")

        assert result["name"] is None
        assert result["city"] is None
        assert result["alternative_names"] == []
        assert result["parent_institutions"] == []

    @pytest.mark.asyncio
    async def test_force_refresh_passthrough(self, monkeypatch):
        seen: list[bool] = []

        async def fake(institution_id, **kwargs):
            seen.append(kwargs.get("force_refresh"))
            return {"id": "https://openalex.org/I1"}

        monkeypatch.setattr(openalex, "get_institution", fake)

        await server.get_institution("I1")
        await server.get_institution("I1", force_refresh=True)

        assert seen == [False, True]

    @pytest.mark.asyncio
    async def test_an_error_carries_a_suggestion(self, monkeypatch):
        self._stub(monkeypatch, {"error": "No institution found for ID: I9", "not_found": True})

        result = await server.get_institution("I9")

        assert "suggestion" in result


# ---------------------------------------------------------------------------
# OpenAlex work-ID resolution: the DOI-less graph row, made chainable
# ---------------------------------------------------------------------------


def _stub_work_id(monkeypatch, mapping, *, work=None):
    """Route ``resolve_work_id`` through *mapping* and answer ``get_work`` with *work*.

    Patches the provider module object, as ``_stub_pmid`` does.
    """
    calls: list[str] = []

    async def fake_resolve_work_id(work_id, **kwargs):
        calls.append(work_id)
        return mapping

    async def fake_get_work(doi, **kwargs):
        return dict(work or {}, doi=f"https://doi.org/{doi}")

    monkeypatch.setattr(openalex, "resolve_work_id", fake_resolve_work_id)
    monkeypatch.setattr(openalex, "get_work", fake_get_work)
    return calls


class TestWorkIdRouting:
    """``search_openalex`` reports an ``openalex_id`` on every hit and OpenCitations
    on every graph row, including the ~1.4% that carry no DOI. Traded for the DOI
    before dispatch, so neither spelling acquires a second cache identity."""

    MAPPING: ClassVar[dict] = {
        "doi": "10.1234/x",
        "openalex_id": "https://openalex.org/W4312223440",
    }

    @pytest.mark.asyncio
    async def test_metadata_answers_under_the_doi(self, monkeypatch):
        _stub_work_id(monkeypatch, self.MAPPING)

        result = await server.get_paper_metadata("W4312223440")

        assert result["_source"] == "openalex"
        assert result["_canonical_id"] == "10.1234/x"

    @pytest.mark.parametrize(
        "spelling",
        ["W4312223440", "openalex:W4312223440", "https://openalex.org/W4312223440"],
    )
    @pytest.mark.asyncio
    async def test_every_spelling_agrees_with_the_doi(self, monkeypatch, spelling):
        _stub_work_id(monkeypatch, self.MAPPING)

        via_id = await server.get_paper_metadata(spelling)
        via_doi = await server.get_paper_metadata("10.1234/x")

        assert via_id["_canonical_id"] == via_doi["_canonical_id"]

    @pytest.mark.asyncio
    async def test_a_doi_never_reaches_the_work_id_trade(self, monkeypatch):
        calls = _stub_work_id(monkeypatch, self.MAPPING)

        await server.get_paper_metadata("10.1234/x")

        assert calls == []

    @pytest.mark.asyncio
    async def test_a_record_without_a_doi_is_a_definitive_miss_naming_the_id(self, monkeypatch):
        """Every tool below is DOI-keyed, so naming the id beats inventing a key
        the cache would then own."""
        _stub_work_id(monkeypatch, {"doi": None, "openalex_id": "https://openalex.org/W1"})

        result = await server.get_paper_metadata("W4312223440")

        assert result["not_found"] is True
        assert "W4312223440" in result["error"]
        assert "import_paper" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_a_provider_error_is_forwarded_with_a_suggestion(self, monkeypatch):
        _stub_work_id(monkeypatch, {"error": "OpenAlex 503", "retryable": True})

        result = await server.get_paper_metadata("W4312223440")

        assert result["retryable"] is True
        assert "openalex.org" in result["suggestion"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", ["W1", "W2024", "W1234567"])
    async def test_a_short_bare_id_stays_a_freeform_import_label(self, monkeypatch, label):
        """``is_work_id``'s two tiers: claiming a sub-live-range run hijacks a label."""
        calls = _stub_work_id(monkeypatch, self.MAPPING)

        result = await server.get_paper_metadata(label)

        assert calls == []
        assert "Cannot resolve paper provider" in result["error"]
