"""Unit tests for :mod:`academic_tools_mcp.tools.formatting`."""

from academic_tools_mcp.tools import formatting


class TestPaperFormatting:
    def test_arxiv_pdf_url_extracts_pdf_link_or_none(self) -> None:
        assert (
            formatting.arxiv_pdf_url(
                {
                    "links": [
                        {"title": "html", "href": "https://arxiv.org/html/1706.03762"},
                        {"title": "pdf", "href": "https://arxiv.org/pdf/1706.03762v1"},
                    ]
                }
            )
            == "https://arxiv.org/pdf/1706.03762v1"
        )
        assert formatting.arxiv_pdf_url({"links": [{"href": "x", "rel": "alternate"}]}) is None
        assert formatting.arxiv_pdf_url({}) is None
        assert formatting.arxiv_pdf_url({"links": None}) is None
        assert formatting.arxiv_pdf_url({"links": "not-a-list"}) is None
        assert (
            formatting.arxiv_pdf_url({"links": ["not a dict", {"title": "pdf", "href": "u"}]})
            == "u"
        )

    def test_format_arxiv_and_biorxiv_followed_published_flag(self) -> None:
        arxiv_meta = formatting.format_arxiv_metadata(
            {"id": "http://arxiv.org/abs/1706.03762v1", "title": "Attention"},
            "1706.03762",
        )
        assert "followed_published" not in arxiv_meta

        arxiv_followed = formatting.format_arxiv_metadata(
            {"id": "http://arxiv.org/abs/1706.03762v1", "title": "Attention"},
            "1706.03762",
            followed_published=False,
        )
        assert arxiv_followed["followed_published"] is False

        bio_followed = formatting.format_biorxiv_metadata(
            {"doi": "10.1101/2020.01.01.123456", "title": "Preprint"},
            "10.1101/2020.01.01.123456",
            followed_published=False,
        )
        assert bio_followed["followed_published"] is False

    def test_format_openalex_and_crossref_share_metadata_key_set(self) -> None:
        oa_meta = formatting.format_openalex_metadata(
            {"title": "Paper", "doi": "https://doi.org/10.1038/x"},
            "10.1038/x",
        )
        cr_meta = formatting.format_crossref_metadata(
            {"title": ["Paper"], "DOI": "10.1038/x"},
            "10.1038/x",
        )
        assert set(oa_meta.keys()) == set(cr_meta.keys())

    def test_format_openalex_via_biorxiv_and_arxiv(self) -> None:
        via_bio = formatting.format_openalex_via_biorxiv(
            {"title": "Journal", "doi": "https://doi.org/10.1038/j"},
            "10.1101/2020.01.01.123456",
            "10.1038/j",
        )
        assert via_bio["_source"] == "openalex_via_biorxiv"
        assert via_bio["preprint_doi"] == "10.1101/2020.01.01.123456"
        assert via_bio["followed_published"] is True

        via_arxiv = formatting.format_openalex_via_arxiv(
            {"title": "Journal", "doi": "https://doi.org/10.1038/j"},
            "1706.03762",
            "10.1038/j",
        )
        assert via_arxiv["_source"] == "openalex_via_arxiv"
        assert via_arxiv["preprint_arxiv_id"] == "1706.03762"
        assert via_arxiv["followed_published"] is True

    def test_format_author_and_institution_profiles_guard_untyped_fields(self) -> None:
        author = formatting.format_author_profile(
            {
                "display_name": "Jane Doe",
                "id": "https://openalex.org/A1",
                "summary_stats": {"h_index": 12, "i10_index": 15},
                "last_known_institutions": [{"display_name": "MIT"}, {"display_name": ""}],
                "topics": [{"display_name": f"T{i}", "count": i} for i in range(7)],
                "affiliations": [
                    {
                        "institution": {"display_name": "MIT", "country_code": "US"},
                        "years": [2024, "bad", True, 2020],
                    }
                ],
            }
        )
        assert author["current_institutions"] == ["MIT"]
        assert len(author["top_topics"]) == 5
        assert author["affiliations"][0]["years"] == [2020, 2024]

        inst = formatting.format_institution_profile(
            {
                "id": "https://openalex.org/I1",
                "display_name": "MIT",
                "display_name_acronyms": ["MIT"],
                "display_name_alternatives": [f"Alt{i}" for i in range(12)],
                "lineage": ["https://openalex.org/I1", "https://openalex.org/I0"],
            }
        )
        assert len(inst["alternative_names"]) == 10
        assert inst["parent_institutions"] == ["https://openalex.org/I0"]
