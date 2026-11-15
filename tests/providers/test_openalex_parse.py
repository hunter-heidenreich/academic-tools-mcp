"""Unit tests for ``providers/openalex_parse.py``."""

from academic_tools_mcp.providers import openalex, openalex_parse


def test_openalex_reexports_public_parse_helpers() -> None:
    for name in (
        "canonical_doi",
        "best_pdf_url",
        "canonical_institution_id",
        "canonical_author_id",
        "normalize_pmid",
        "is_pmid",
        "normalize_work_id",
        "is_work_id",
        "canonical_work_id",
        "reconstruct_abstract",
    ):
        assert getattr(openalex, name) is getattr(openalex_parse, name)


def test_parse_helpers_pure_round_trips() -> None:
    assert openalex_parse.canonical_doi("https://doi.org/10.1038/Nature12373") == (
        "10.1038/nature12373"
    )
    assert openalex_parse.canonical_work_id("https://openalex.org/W4312223440") == "w4312223440"
    assert openalex_parse.is_work_id("W4312223440") is True
    assert openalex_parse.is_work_id("W2024") is False
    assert openalex_parse.is_pmid("pmid:123") is True
    assert openalex_parse.is_pmid("123") is False
    assert openalex_parse._meta_count({"meta": {"count": 42}}) == 42
    assert openalex_parse._meta_count({"meta": {"count": -1}}) is None
    assert openalex_parse.reconstruct_abstract({"hello": [0], "world": [1]}) == "hello world"
