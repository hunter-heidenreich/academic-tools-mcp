"""Unit tests for ``providers/crossref_parse.py``."""

from academic_tools_mcp.providers import crossref, crossref_parse


def test_crossref_reexports_public_parse_helpers() -> None:
    for name in (
        "canonical_doi",
        "author_name",
        "institution_name",
        "abstract_text",
        "RETRACTION_UPDATE_TYPES",
        "retracts",
        "updates",
        "relations",
    ):
        assert getattr(crossref, name) is getattr(crossref_parse, name)


def test_crossref_parse_helpers() -> None:
    assert crossref_parse.author_name({"given": "Ada", "family": "Lovelace"}) == "Ada Lovelace"
    assert crossref_parse.institution_name([{"place": ["London"]}, {"name": "MIT"}]) == "MIT"
    assert (
        crossref_parse.abstract_text(
            {"abstract": "<jats:title>Abstract</jats:title><jats:p>Body &amp; text.</jats:p>"}
        )
        == "Body & text."
    )
    assert crossref_parse.retracts("partial-retraction") is True
    assert crossref_parse.retracts("correction") is False
    assert crossref_parse._message_of({"message": {"DOI": "10.1/x"}}) == {"DOI": "10.1/x"}
    assert crossref_parse._message_of({"message": []}) is None
