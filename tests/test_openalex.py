import pytest

from academic_tools_mcp import cache
from academic_tools_mcp.providers import openalex
from academic_tools_mcp.providers.openalex import (
    _canonical_author_id,
    _canonical_doi,
    _normalize_author_id,
    _normalize_doi,
    reconstruct_abstract,
)


class TestNormalizeDoi:
    def test_bare_doi(self):
        assert _normalize_doi("10.1234/test") == "10.1234/test"

    def test_prefixed_doi(self):
        assert _normalize_doi("doi:10.1234/test") == "10.1234/test"

    def test_url_doi(self):
        assert _normalize_doi("https://doi.org/10.1234/test") == "10.1234/test"


class TestCanonicalDoi:
    def test_lowercases(self):
        assert _canonical_doi("10.1234/ABC") == "10.1234/abc"

    def test_strips_prefix_and_lowercases(self):
        assert _canonical_doi("doi:10.1234/ABC") == "10.1234/abc"

    def test_strips_url_and_lowercases(self):
        assert _canonical_doi("https://doi.org/10.1234/ABC") == "10.1234/abc"


class TestNormalizeAuthorId:
    def test_openalex_id(self):
        assert _normalize_author_id("A5023888391") == "A5023888391"

    def test_openalex_url(self):
        assert _normalize_author_id("https://openalex.org/A5023888391") == "A5023888391"

    def test_orcid_url_passthrough(self):
        orcid = "https://orcid.org/0000-0001-6187-6610"
        assert _normalize_author_id(orcid) == orcid


class TestCanonicalAuthorId:
    def test_lowercases(self):
        assert _canonical_author_id("A5023888391") == "a5023888391"

    def test_strips_url_and_lowercases(self):
        assert _canonical_author_id("https://openalex.org/A5023888391") == "a5023888391"

    def test_orcid_lowercased(self):
        assert (
            _canonical_author_id("https://orcid.org/0000-0001-6187-6610")
            == "https://orcid.org/0000-0001-6187-6610"
        )


class TestReconstructAbstract:
    def test_simple(self):
        index = {"Hello": [0], "world": [1]}
        assert reconstruct_abstract(index) == "Hello world"

    def test_out_of_order(self):
        index = {"world": [1], "Hello": [0], "beautiful": [2]}
        assert reconstruct_abstract(index) == "Hello world beautiful"

    def test_repeated_words(self):
        index = {"the": [0, 2], "cat": [1], "sat": [3]}
        assert reconstruct_abstract(index) == "the cat the sat"

    def test_empty(self):
        assert reconstruct_abstract({}) == ""

    def test_none(self):
        assert reconstruct_abstract(None) == ""


class TestGetWork404Marker:
    """A definitive 404 from get_work tags its error dict with
    ``not_found: True`` so server.get_paper_metadata can distinguish a true
    miss (eligible for the Crossref fallback) from a transient error."""

    @pytest.mark.asyncio
    async def test_404_error_carries_not_found_and_negative_caches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

        class StubResponse:
            status_code = 404

            def raise_for_status(self):
                pass

        async def fake_throttled_get(url, **kwargs):
            return StubResponse()

        monkeypatch.setattr(openalex, "_throttled_get", fake_throttled_get)

        result = await openalex.get_work("10.9999/does-not-exist")
        assert result.get("not_found") is True
        assert "No work found for DOI" in result["error"]

        # The marker is persisted in the negative cache too, so a later
        # read (e.g. via a batch that warmed it) still triggers the fallback.
        neg = cache.get_negative(openalex.NAMESPACE, "works", "10.9999/does-not-exist")
        assert neg is not None
        assert neg.get("not_found") is True
