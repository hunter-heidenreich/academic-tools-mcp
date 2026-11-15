"""Unit tests for ``providers/openalex_parse.py``."""

from academic_tools_mcp.providers import openalex, openalex_parse


def test_openalex_reexports_all_parse_helpers() -> None:
    for name in (
        "canonical_doi",
        "_usable_http_url",
        "best_pdf_url",
        "_normalize_author_id",
        "_author_path_id",
        "_looks_like_ror",
        "_normalize_institution_id",
        "_institution_path_id",
        "canonical_institution_id",
        "canonical_author_id",
        "normalize_pmid",
        "is_pmid",
        "normalize_work_id",
        "is_work_id",
        "canonical_work_id",
        "_canonical_from_response_doi",
        "_meta_count",
        "_extract_batch_pmid",
        "_extract_batch_work_id",
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
