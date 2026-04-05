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
        """After import, get_manual_paper_sections should find cached sections."""
        md = tmp_path / "paper.md"
        md.write_text("## Intro\n\nHello.\n\n## Discussion\n\nBye.")

        import uuid
        from academic_tools_mcp import cache, papers
        ident = f"10.1038/test-md-sections-{uuid.uuid4().hex[:8]}"
        manual.import_markdown(str(md), ident)

        canonical = manual._canonical_key(ident)
        cached = cache.get(manual.NAMESPACE, "sections", papers._sections_key(canonical))
        assert cached is not None
        assert len(cached["sections"]) == 2
