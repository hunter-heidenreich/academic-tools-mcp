import pytest

from academic_tools_mcp import manual


# ---------------------------------------------------------------------------
# Identifier normalization
# ---------------------------------------------------------------------------


class TestNormalizeIdentifier:
    def test_bare_doi(self):
        assert manual._normalize_identifier("10.1038/s41586-024-00001-1") == "10.1038/s41586-024-00001-1"

    def test_doi_prefix(self):
        assert manual._normalize_identifier("doi:10.1038/s41586-024-00001-1") == "10.1038/s41586-024-00001-1"

    def test_doi_prefix_uppercase(self):
        assert manual._normalize_identifier("DOI:10.1038/s41586-024-00001-1") == "10.1038/s41586-024-00001-1"

    def test_https_doi_url(self):
        assert manual._normalize_identifier("https://doi.org/10.1038/s41586-024-00001-1") == "10.1038/s41586-024-00001-1"

    def test_dx_doi_url(self):
        assert manual._normalize_identifier("https://dx.doi.org/10.1038/s41586-024-00001-1") == "10.1038/s41586-024-00001-1"

    def test_freeform_label(self):
        assert manual._normalize_identifier("my-paper-2024") == "my-paper-2024"

    def test_strips_whitespace(self):
        assert manual._normalize_identifier("  10.1038/s41586-024-00001-1  ") == "10.1038/s41586-024-00001-1"


class TestCanonicalKey:
    def test_lowercases_doi(self):
        assert manual._canonical_key("10.1038/S41586-024-00001-1") == "10.1038/s41586-024-00001-1"

    def test_lowercases_freeform(self):
        assert manual._canonical_key("My-Paper-2024") == "my-paper-2024"

    def test_normalizes_url(self):
        assert manual._canonical_key("https://doi.org/10.1038/S41586-024-00001-1") == "10.1038/s41586-024-00001-1"


# ---------------------------------------------------------------------------
# PDF filename
# ---------------------------------------------------------------------------


class TestPdfFilename:
    def test_doi_slashes_replaced(self):
        assert manual._pdf_filename("10.1038/s41586-024-00001-1") == "10.1038_s41586-024-00001-1.pdf"

    def test_colons_replaced(self):
        assert manual._pdf_filename("some:label") == "some_label.pdf"

    def test_freeform(self):
        assert manual._pdf_filename("my-paper") == "my-paper.pdf"


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------


class TestResolveTarget:
    def test_arxiv_new_style(self):
        target = manual._resolve_target("2301.00001")
        assert target["namespace"] == "arxiv"
        assert target["canonical"] == "2301.00001"

    def test_arxiv_with_version(self):
        target = manual._resolve_target("2301.00001v2")
        assert target["namespace"] == "arxiv"
        assert target["canonical"] == "2301.00001"

    def test_arxiv_old_style(self):
        target = manual._resolve_target("hep-th/9901001")
        assert target["namespace"] == "arxiv"
        assert target["canonical"] == "hep-th/9901001"

    def test_arxiv_url(self):
        target = manual._resolve_target("https://arxiv.org/abs/2301.00001v2")
        assert target["namespace"] == "arxiv"
        assert target["canonical"] == "2301.00001"

    def test_biorxiv_doi(self):
        target = manual._resolve_target("10.1101/2024.01.01.573838")
        assert target["namespace"] == "biorxiv"
        assert target["canonical"] == "10.1101/2024.01.01.573838"

    def test_biorxiv_url(self):
        target = manual._resolve_target("https://doi.org/10.1101/2024.01.01.573838")
        assert target["namespace"] == "biorxiv"

    def test_acl_doi(self):
        target = manual._resolve_target("10.18653/v1/2023.acl-long.1")
        assert target["namespace"] == "acl_anthology"

    def test_acl_doi_url(self):
        target = manual._resolve_target("https://doi.org/10.18653/v1/2023.acl-long.1")
        assert target["namespace"] == "acl_anthology"

    def test_generic_doi_falls_back_to_manual(self):
        target = manual._resolve_target("10.1038/s41586-024-00001-1")
        assert target["namespace"] == "manual"

    def test_freeform_falls_back_to_manual(self):
        target = manual._resolve_target("my-paper-2024")
        assert target["namespace"] == "manual"


# ---------------------------------------------------------------------------
# Local import
# ---------------------------------------------------------------------------


class TestImportLocalPdf:
    def test_file_not_found(self):
        result = manual.import_local_pdf("/nonexistent/path.pdf", "test-id")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_directory_not_file(self, tmp_path):
        result = manual.import_local_pdf(str(tmp_path), "test-id")
        assert "error" in result
        assert "not a file" in result["error"].lower()

    def test_successful_import(self, tmp_path):
        # Create a fake PDF
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        # Use a unique identifier each run to avoid cross-test cache hits
        import uuid
        ident = f"10.1038/test-import-{uuid.uuid4().hex[:8]}"
        result = manual.import_local_pdf(str(pdf), ident)
        assert "error" not in result
        assert result["identifier"] == ident
        assert result["namespace"] == "manual"
        assert result["size_bytes"] > 0
        assert result["cached"] is False

    def test_cached_on_second_import(self, tmp_path):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        import uuid
        ident = f"10.1038/test-cached-{uuid.uuid4().hex[:8]}"
        manual.import_local_pdf(str(pdf), ident)
        result = manual.import_local_pdf(str(pdf), ident)
        assert result["cached"] is True

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        # Ensure expanduser works (just test that it doesn't crash on ~)
        result = manual.import_local_pdf("~/nonexistent-paper-12345.pdf", "test")
        assert "error" in result

    def test_arxiv_routes_to_arxiv_namespace(self, tmp_path):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        import uuid
        ident = f"2301.{uuid.uuid4().int % 100000:05d}"
        result = manual.import_local_pdf(str(pdf), ident)
        assert "error" not in result
        assert result["namespace"] == "arxiv"
        assert "arxiv" in result["path"]

    def test_biorxiv_routes_to_biorxiv_namespace(self, tmp_path):
        pdf = tmp_path / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake content")

        import uuid
        ident = f"10.1101/2024.01.01.{uuid.uuid4().hex[:6]}"
        result = manual.import_local_pdf(str(pdf), ident)
        assert "error" not in result
        assert result["namespace"] == "biorxiv"
        assert "biorxiv" in result["path"]


# ---------------------------------------------------------------------------
# URL download
# ---------------------------------------------------------------------------


class TestDownloadPdfFromUrl:
    @pytest.mark.asyncio
    async def test_html_response_rejected(self, monkeypatch):
        """Should reject responses that look like HTML (login pages, etc.)."""
        import httpx

        mock_response = httpx.Response(
            200,
            content=b"<html>Login required</html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", "http://example.com/paper.pdf"),
        )

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                return mock_response

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: MockClient())

        result = await manual.download_pdf_from_url(
            "http://example.com/paper.pdf", "test-html-reject"
        )
        assert "error" in result
        assert "HTML" in result["error"]


# ---------------------------------------------------------------------------
# Markdown import
# ---------------------------------------------------------------------------


class TestImportMarkdown:
    def test_file_not_found(self):
        result = manual.import_markdown("/nonexistent/paper.md", "test-id")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_directory_not_file(self, tmp_path):
        result = manual.import_markdown(str(tmp_path), "test-id")
        assert "error" in result
        assert "not a file" in result["error"].lower()

    def test_successful_import(self, tmp_path):
        md = tmp_path / "paper.md"
        md.write_text("## Introduction\n\nSome text.\n\n## Methods\n\nMore text.")

        import uuid
        ident = f"10.1038/test-md-{uuid.uuid4().hex[:8]}"
        result = manual.import_markdown(str(md), ident)
        assert "error" not in result
        assert result["identifier"] == ident
        assert result["namespace"] == "manual"
        assert result["cached"] is False
        assert len(result["sections"]) == 2
        assert result["sections"][0]["title"] == "Introduction"
        assert result["sections"][1]["title"] == "Methods"

    def test_cached_on_second_import(self, tmp_path):
        md = tmp_path / "paper.md"
        md.write_text("## Results\n\nFindings here.")

        import uuid
        ident = f"10.1038/test-md-cached-{uuid.uuid4().hex[:8]}"
        manual.import_markdown(str(md), ident)
        result = manual.import_markdown(str(md), ident)
        assert result["cached"] is True

    def test_sections_available_immediately(self, tmp_path):
        """After import, section cache should be populated in the right namespace."""
        md = tmp_path / "paper.md"
        md.write_text("## Intro\n\nHello.\n\n## Discussion\n\nBye.")

        import uuid
        from academic_tools_mcp import cache, papers
        ident = f"10.1038/test-md-sections-{uuid.uuid4().hex[:8]}"
        result = manual.import_markdown(str(md), ident)

        namespace = result["namespace"]
        target = manual._resolve_target(ident)
        canonical = target["canonical"]
        cached = cache.get(namespace, "sections", papers._sections_key(canonical))
        assert cached is not None
        assert len(cached["sections"]) == 2

    def test_arxiv_markdown_routes_to_arxiv_namespace(self, tmp_path):
        md = tmp_path / "paper.md"
        md.write_text("## Abstract\n\nText.\n\n## Introduction\n\nMore text.")

        import uuid
        ident = f"2301.{uuid.uuid4().int % 100000:05d}"
        result = manual.import_markdown(str(md), ident)
        assert "error" not in result
        assert result["namespace"] == "arxiv"
        assert "arxiv" in result["markdown_path"]
