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
        assert (
            acl_anthology.doi_to_anthology_id("https://doi.org/10.18653/v1/2023.acl-long.1")
            == "2023.acl-long.1"
        )

    def test_prefixed_doi(self):
        assert (
            acl_anthology.doi_to_anthology_id("doi:10.18653/v1/2023.acl-long.1")
            == "2023.acl-long.1"
        )

    def test_emnlp(self):
        assert (
            acl_anthology.doi_to_anthology_id("10.18653/v1/2022.emnlp-main.100")
            == "2022.emnlp-main.100"
        )

    def test_naacl(self):
        assert (
            acl_anthology.doi_to_anthology_id("10.18653/v1/2022.naacl-main.50")
            == "2022.naacl-main.50"
        )

    def test_findings(self):
        assert (
            acl_anthology.doi_to_anthology_id("10.18653/v1/2023.findings-acl.42")
            == "2023.findings-acl.42"
        )

    def test_non_acl_returns_none(self):
        assert acl_anthology.doi_to_anthology_id("10.1038/s41586-021-03819-2") is None

    def test_whitespace_stripped(self):
        assert (
            acl_anthology.doi_to_anthology_id("  10.18653/v1/2023.acl-long.1  ")
            == "2023.acl-long.1"
        )

    def test_old_format_lowercased_uppercased(self):
        # Crossref hands old-format DOIs back lowercased; the CDN path is
        # case-sensitive, so the extracted ID must be uppercased.
        assert acl_anthology.doi_to_anthology_id("10.18653/v1/p16-1160") == "P16-1160"

    def test_old_format_already_uppercase_idempotent(self):
        assert acl_anthology.doi_to_anthology_id("10.18653/v1/P16-1160") == "P16-1160"

    def test_old_format_workshop_venue(self):
        assert acl_anthology.doi_to_anthology_id("10.18653/v1/w04-1013") == "W04-1013"

    def test_new_format_stays_lowercase(self):
        # New-format IDs carry lowercase venue letters that must be preserved.
        assert acl_anthology.doi_to_anthology_id("10.18653/v1/2023.acl-long.1") == "2023.acl-long.1"


# ---------------------------------------------------------------------------
# PDF URL construction
# ---------------------------------------------------------------------------


class TestPdfUrl:
    def test_basic(self):
        assert (
            acl_anthology.pdf_url("2023.acl-long.1")
            == "https://aclanthology.org/2023.acl-long.1.pdf"
        )

    def test_emnlp(self):
        assert (
            acl_anthology.pdf_url("2022.emnlp-main.100")
            == "https://aclanthology.org/2022.emnlp-main.100.pdf"
        )

    def test_old_format_lowercased_doi_round_trip(self):
        # A Crossref-lowercased old-format DOI must produce the case-sensitive
        # URL the CDN expects (P16-1160.pdf, not p16-1160.pdf).
        aid = acl_anthology.doi_to_anthology_id("10.18653/v1/p16-1160")
        assert acl_anthology.pdf_url(aid) == "https://aclanthology.org/P16-1160.pdf"


# ---------------------------------------------------------------------------
# Anthology ID normalization
# ---------------------------------------------------------------------------


class TestNormalizeAnthologyId:
    def test_old_format_lowercased(self):
        assert acl_anthology._normalize_anthology_id("p16-1160") == "P16-1160"

    def test_old_format_already_uppercase(self):
        assert acl_anthology._normalize_anthology_id("P16-1160") == "P16-1160"

    def test_new_format_unchanged(self):
        assert acl_anthology._normalize_anthology_id("2023.acl-long.1") == "2023.acl-long.1"

    def test_new_format_mixed_case_left_as_is(self):
        # Not an old-format match, so it is returned verbatim (no spurious upper()).
        assert acl_anthology._normalize_anthology_id("2023.ACL-long.1") == "2023.ACL-long.1"


# ---------------------------------------------------------------------------
# Normalize DOI
# ---------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_bare(self):
        assert (
            acl_anthology._normalize_doi("10.18653/v1/2023.acl-long.1")
            == "10.18653/v1/2023.acl-long.1"
        )

    def test_https_url(self):
        assert (
            acl_anthology._normalize_doi("https://doi.org/10.18653/v1/2023.acl-long.1")
            == "10.18653/v1/2023.acl-long.1"
        )

    def test_http_url(self):
        assert (
            acl_anthology._normalize_doi("http://doi.org/10.18653/v1/2023.acl-long.1")
            == "10.18653/v1/2023.acl-long.1"
        )

    def test_doi_prefix(self):
        assert (
            acl_anthology._normalize_doi("doi:10.18653/v1/2023.acl-long.1")
            == "10.18653/v1/2023.acl-long.1"
        )
