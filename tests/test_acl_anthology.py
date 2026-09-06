import pytest

from academic_tools_mcp.providers import acl_anthology

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

    def test_uppercase_prefix(self):
        # DOIs are case-insensitive; an uppercased 'V1' prefix is still ACL.
        assert acl_anthology.is_acl_doi("10.18653/V1/2023.acl-long.1") is True

    def test_uppercase_prefix_url(self):
        assert acl_anthology.is_acl_doi("https://doi.org/10.18653/V1/P16-1160") is True


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

    def test_uppercase_prefix_new_format(self):
        # DOIs are case-insensitive: an uppercased 'V1' prefix still resolves.
        assert acl_anthology.doi_to_anthology_id("10.18653/V1/2023.acl-long.1") == "2023.acl-long.1"

    def test_uppercase_prefix_old_format(self):
        # Case-insensitive prefix match AND old-format suffix uppercasing.
        assert acl_anthology.doi_to_anthology_id("10.18653/V1/p16-1160") == "P16-1160"


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


# ---------------------------------------------------------------------------
# PDF cache path
# ---------------------------------------------------------------------------


class TestPdfPath:
    def test_acl_doi(self):
        path = acl_anthology.pdf_path("10.18653/v1/2023.acl-long.1")
        assert path.parent.name == "pdfs"
        assert path.parent.parent.name == "acl_anthology"
        assert path.name == "2023.acl-long.1.pdf"

    def test_old_format_uppercased(self):
        # Round-trips the case-sensitive CDN filename, not the lowercased DOI.
        assert acl_anthology.pdf_path("10.18653/v1/p16-1160").name == "P16-1160.pdf"

    def test_non_acl_doi_raises(self):
        # Must not return a sentinel path (e.g. /dev/null) whose .exists() is
        # True — that would let a non-PDF slip past the convert guard.
        with pytest.raises(ValueError):
            acl_anthology.pdf_path("10.1038/s41586-021-03819-2")


# ---------------------------------------------------------------------------
# PDF filename sanitization
# ---------------------------------------------------------------------------


class TestPdfFilename:
    def test_new_format_unchanged(self):
        # Regression: real ACL IDs must map to the same filename as before
        # (no cache migration) — all chars are in the safe set.
        assert acl_anthology._pdf_filename("2023.acl-long.1") == "2023.acl-long.1.pdf"

    def test_old_format_unchanged(self):
        assert acl_anthology._pdf_filename("P16-1160") == "P16-1160.pdf"

    def test_metacharacters_neutralized(self):
        # Defense-in-depth: shell/path metacharacters never reach the filename.
        # They are percent-encoded (injective) rather than collapsed to "_".
        assert acl_anthology._pdf_filename("foo;bar") == "foo%3Bbar.pdf"


# ---------------------------------------------------------------------------
# download_pdf
# ---------------------------------------------------------------------------


class TestDownloadPdfProvenance:
    """ACL decorates its response with ``anthology_id`` and ``pdf_url``.

    The cached and fresh branches were two hand-copied blocks, so nothing
    stopped them drifting apart — and both called ``dest.stat()`` outside any
    try, so a concurrent unlink between the usability check and the stat raised
    OSError straight out of ``download_pdf``, breaking the module's uniform
    ``{error}`` contract. Both now come from one ``extra_fields`` dict.
    """

    _DOI = "10.18653/v1/2023.acl-long.1"

    @pytest.mark.asyncio
    async def test_fresh_and_cached_payloads_agree(self, tmp_path, monkeypatch):
        from academic_tools_mcp import _pdf_download, cache

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)

        async def fake_stream(client, url, dest, **kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4 acl")
            return {"path": str(dest), "size_bytes": dest.stat().st_size, "cached": False}

        monkeypatch.setattr(_pdf_download, "stream_to_file", fake_stream)

        fresh = await acl_anthology.download_pdf(self._DOI)
        cached = await acl_anthology.download_pdf(self._DOI)

        assert fresh["cached"] is False
        assert cached["cached"] is True
        # Provenance is identical across both branches, by construction.
        for key in ("anthology_id", "pdf_url"):
            assert fresh[key] == cached[key]
        assert fresh["anthology_id"] == "2023.acl-long.1"
        assert fresh["pdf_url"] == "https://aclanthology.org/2023.acl-long.1.pdf"

    @pytest.mark.asyncio
    async def test_a_404_is_negative_cached(self, tmp_path, monkeypatch):
        """A missing camera-ready re-hit the CDN on every call: only
        oa_download negative-cached its download failures, the three native
        providers cached nothing."""
        from academic_tools_mcp import _pdf_download, cache

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)
        calls = 0

        async def fake_stream(client, url, dest, **kwargs):
            nonlocal calls
            calls += 1
            return {"error": "PDF not found", "retryable": False}

        monkeypatch.setattr(_pdf_download, "stream_to_file", fake_stream)

        assert "error" in await acl_anthology.download_pdf(self._DOI)
        assert "error" in await acl_anthology.download_pdf(self._DOI)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_an_error_carries_no_provenance(self, tmp_path, monkeypatch):
        from academic_tools_mcp import _pdf_download, cache

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)

        async def fake_stream(client, url, dest, **kwargs):
            return {"error": "PDF not found", "retryable": False}

        monkeypatch.setattr(_pdf_download, "stream_to_file", fake_stream)

        result = await acl_anthology.download_pdf(self._DOI)

        assert "anthology_id" not in result

    @pytest.mark.asyncio
    async def test_a_non_acl_doi_is_rejected_before_any_fetch(self):
        result = await acl_anthology.download_pdf("10.1038/nature12373")
        assert "error" in result
        assert "Not an ACL Anthology DOI" in result["error"]
