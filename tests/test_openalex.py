from academic_tools_mcp.openalex import (
    _canonical_doi,
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
