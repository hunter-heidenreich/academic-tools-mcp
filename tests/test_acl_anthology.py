import pytest

from academic_tools_mcp import acl_anthology


# ---------------------------------------------------------------------------
# DOI detection
# ---------------------------------------------------------------------------


class TestIsAclDoi:
    def test_acl_doi_bare(self):
        assert acl_anthology.is_acl_doi("10.18653/v1/2023.acl-long.1") is True

    def test_acl_doi_url(self):
        assert acl_anthology.is_acl_doi("https://doi.org/10.18653/v1/2023.acl-long.1") is True

    def test_acl_doi_prefixed(self):
        assert acl_anthology.is_acl_doi("doi:10.18653/v1/2023.acl-long.1") is True

    def test_non_acl_doi(self):
        assert acl_anthology.is_acl_doi("10.1038/s41586-021-03819-2") is False

    def test_arxiv_doi(self):
        assert acl_anthology.is_acl_doi("10.48550/arXiv.2301.00001") is False


# ---------------------------------------------------------------------------
# DOI → Anthology ID
# ---------------------------------------------------------------------------


class TestDoiToAnthologyId:
    def test_bare_doi(self):
        assert acl_anthology.doi_to_anthology_id("10.18653/v1/2023.acl-long.1") == "2023.acl-long.1"

    def test_url_doi(self):
        assert acl_anthology.doi_to_anthology_id("https://doi.org/10.18653/v1/2023.acl-long.1") == "2023.acl-long.1"

    def test_prefixed_doi(self):
        assert acl_anthology.doi_to_anthology_id("doi:10.18653/v1/2023.acl-long.1") == "2023.acl-long.1"

    def test_emnlp(self):
        assert acl_anthology.doi_to_anthology_id("10.18653/v1/2022.emnlp-main.100") == "2022.emnlp-main.100"

    def test_naacl(self):
        assert acl_anthology.doi_to_anthology_id("10.18653/v1/2022.naacl-main.50") == "2022.naacl-main.50"

    def test_findings(self):
        assert acl_anthology.doi_to_anthology_id("10.18653/v1/2023.findings-acl.42") == "2023.findings-acl.42"

    def test_non_acl_returns_none(self):
        assert acl_anthology.doi_to_anthology_id("10.1038/s41586-021-03819-2") is None

    def test_whitespace_stripped(self):
        assert acl_anthology.doi_to_anthology_id("  10.18653/v1/2023.acl-long.1  ") == "2023.acl-long.1"


# ---------------------------------------------------------------------------
# PDF URL construction
# ---------------------------------------------------------------------------


class TestPdfUrl:
    def test_basic(self):
        assert acl_anthology.pdf_url("2023.acl-long.1") == "https://aclanthology.org/2023.acl-long.1.pdf"

    def test_emnlp(self):
        assert acl_anthology.pdf_url("2022.emnlp-main.100") == "https://aclanthology.org/2022.emnlp-main.100.pdf"


# ---------------------------------------------------------------------------
# Normalize DOI
# ---------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_bare(self):
        assert acl_anthology._normalize_doi("10.18653/v1/2023.acl-long.1") == "10.18653/v1/2023.acl-long.1"

    def test_https_url(self):
        assert acl_anthology._normalize_doi("https://doi.org/10.18653/v1/2023.acl-long.1") == "10.18653/v1/2023.acl-long.1"

    def test_http_url(self):
        assert acl_anthology._normalize_doi("http://doi.org/10.18653/v1/2023.acl-long.1") == "10.18653/v1/2023.acl-long.1"

    def test_doi_prefix(self):
        assert acl_anthology._normalize_doi("doi:10.18653/v1/2023.acl-long.1") == "10.18653/v1/2023.acl-long.1"
