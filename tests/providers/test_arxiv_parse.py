"""Unit tests for ``providers/arxiv_parse.py``."""

from defusedxml.ElementTree import fromstring

from academic_tools_mcp.providers import arxiv, arxiv_parse


def test_arxiv_reexports_public_parse_helpers() -> None:
    for name in (
        "ARXIV_DOI_PREFIX",
        "OLD_ARCHIVE_PATTERN",
        "OLD_NUMBER_PATTERN",
        "normalize_arxiv_id",
        "canonical_arxiv_id",
        "is_arxiv_id",
        "strip_version",
        "base_arxiv_id",
        "id_from_entry",
    ):
        assert getattr(arxiv, name) is getattr(arxiv_parse, name)


def test_arxiv_parse_id_and_xml_helpers() -> None:
    assert arxiv_parse.normalize_arxiv_id("https://arxiv.org/abs/math.GT/0309136v2") == (
        "math.GT/0309136v2"
    )
    assert arxiv_parse.canonical_arxiv_id("math.GT/0309136v2") == "math.gt/0309136v2"
    assert arxiv_parse.base_arxiv_id("math.GT/0309136v2") == "math.gt/0309136"
    assert arxiv_parse.strip_version("math.GT/0309136v2") == "math.GT/0309136"
    assert arxiv_parse._version_number("2301.00001v3") == 3
    assert arxiv_parse._version_number("2301.00001") == 0
    assert arxiv_parse.id_from_entry({"id": "http://arxiv.org/abs/2301.00001v1"}) == "2301.00001v1"
    assert arxiv_parse.id_from_entry({"id": 123}) == ""
    assert arxiv_parse._iso_utc("Mon, 12 Jun 2017 17:57:34 GMT") == "2017-06-12T17:57:34Z"
    assert arxiv_parse._iso_utc("not a date") is None

    feed = fromstring(
        '<feed xmlns="http://www.w3.org/2005/Atom"'
        ' xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"'
        ' xmlns:arxiv="http://arxiv.org/schemas/atom">'
        "<opensearch:totalResults>42</opensearch:totalResults>"
        "<entry>"
        "<id>http://arxiv.org/abs/2301.00001v1</id>"
        "<title>Test\n  Paper</title>"
        "<summary>Test\n  Abstract</summary>"
        "<published>2023-01-01T00:00:00Z</published>"
        "<updated>2023-01-02T00:00:00Z</updated>"
        "<author><name>Ada Lovelace</name>"
        "<arxiv:affiliation>MIT</arxiv:affiliation></author>"
        '<arxiv:primary_category term="cs.AI"/>'
        '<category term="cs.AI"/>'
        "</entry>"
        "</feed>"
    )
    assert arxiv_parse._total_results(feed) == 42
    entry = feed.find(f"{{{arxiv_parse._ATOM_NS}}}entry")
    assert entry is not None
    assert arxiv_parse._is_error_entry(entry) is False
    parsed = arxiv_parse._parse_entry(entry)
    assert parsed["title"] == "Test Paper"
    assert parsed["summary"] == "Test Abstract"
    assert parsed["primary_category"] == "cs.AI"
    assert parsed["authors"] == [{"name": "Ada Lovelace", "affiliations": ["MIT"]}]
