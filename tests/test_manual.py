import contextlib

import pytest

from academic_tools_mcp import manual

# ---------------------------------------------------------------------------
# Identifier normalization
# ---------------------------------------------------------------------------


class TestNormalizeIdentifier:
    def test_bare_doi(self):
        assert (
            manual._normalize_identifier("10.1038/s41586-024-00001-1")
            == "10.1038/s41586-024-00001-1"
        )

    def test_doi_prefix(self):
        assert (
            manual._normalize_identifier("doi:10.1038/s41586-024-00001-1")
            == "10.1038/s41586-024-00001-1"
        )

    def test_doi_prefix_uppercase(self):
        assert (
            manual._normalize_identifier("DOI:10.1038/s41586-024-00001-1")
            == "10.1038/s41586-024-00001-1"
        )

    def test_https_doi_url(self):
        assert (
            manual._normalize_identifier("https://doi.org/10.1038/s41586-024-00001-1")
            == "10.1038/s41586-024-00001-1"
        )

    def test_dx_doi_url(self):
        assert (
            manual._normalize_identifier("https://dx.doi.org/10.1038/s41586-024-00001-1")
            == "10.1038/s41586-024-00001-1"
        )

    def test_freeform_label(self):
        assert manual._normalize_identifier("my-paper-2024") == "my-paper-2024"

    def test_strips_whitespace(self):
        assert (
            manual._normalize_identifier("  10.1038/s41586-024-00001-1  ")
            == "10.1038/s41586-024-00001-1"
        )


class TestCanonicalKey:
    def test_lowercases_doi(self):
        assert manual._canonical_key("10.1038/S41586-024-00001-1") == "10.1038/s41586-024-00001-1"

    def test_lowercases_freeform(self):
        assert manual._canonical_key("My-Paper-2024") == "my-paper-2024"

    def test_normalizes_url(self):
        assert (
            manual._canonical_key("https://doi.org/10.1038/S41586-024-00001-1")
            == "10.1038/s41586-024-00001-1"
        )


# ---------------------------------------------------------------------------
# PDF filename
# ---------------------------------------------------------------------------


class TestPdfFilename:
    def test_doi_slashes_replaced(self):
        assert (
            manual._pdf_filename("10.1038/s41586-024-00001-1") == "10.1038_s41586-024-00001-1.pdf"
        )

    def test_colons_replaced(self):
        assert manual._pdf_filename("some:label") == "some_label.pdf"

    def test_freeform(self):
        assert manual._pdf_filename("my-paper") == "my-paper.pdf"

    def test_strips_shell_metacharacters(self):
        # The filename is fed to the converter subprocess, so an exotic
        # identifier must not carry shell metacharacters into the name.
        name = manual._pdf_filename('x"$(touch pwned)`id`;rm /|y')
        assert name.endswith(".pdf")
        for bad in ('"', "$", "(", ")", "`", ";", "|", " ", "/"):
            assert bad not in name


# ---------------------------------------------------------------------------
# Provider routing
# ---------------------------------------------------------------------------


class TestResolveTarget:
    def test_arxiv_new_style(self):
        target = manual.resolve_target("2301.00001")
        assert target["namespace"] == "arxiv"
        assert target["canonical"] == "2301.00001"

    def test_arxiv_with_version(self):
        target = manual.resolve_target("2301.00001v2")
        assert target["namespace"] == "arxiv"
        assert target["canonical"] == "2301.00001"

    def test_arxiv_old_style(self):
        target = manual.resolve_target("hep-th/9901001")
        assert target["namespace"] == "arxiv"
        assert target["canonical"] == "hep-th/9901001"

    def test_arxiv_url(self):
        target = manual.resolve_target("https://arxiv.org/abs/2301.00001v2")
        assert target["namespace"] == "arxiv"
        assert target["canonical"] == "2301.00001"

    def test_biorxiv_doi(self):
        target = manual.resolve_target("10.1101/2024.01.01.573838")
        assert target["namespace"] == "biorxiv"
        assert target["canonical"] == "10.1101/2024.01.01.573838"

    def test_biorxiv_url(self):
        target = manual.resolve_target("https://doi.org/10.1101/2024.01.01.573838")
        assert target["namespace"] == "biorxiv"

    def test_acl_doi(self):
        target = manual.resolve_target("10.18653/v1/2023.acl-long.1")
        assert target["namespace"] == "acl_anthology"

    def test_acl_doi_url(self):
        target = manual.resolve_target("https://doi.org/10.18653/v1/2023.acl-long.1")
        assert target["namespace"] == "acl_anthology"

    def test_generic_doi_falls_back_to_manual(self):
        target = manual.resolve_target("10.1038/s41586-024-00001-1")
        assert target["namespace"] == "manual"

    def test_freeform_falls_back_to_manual(self):
        target = manual.resolve_target("my-paper-2024")
        assert target["namespace"] == "manual"


class TestResolveMetadataSource:
    def test_arxiv_new_style(self):
        assert manual.resolve_metadata_source("2301.00001") == "arxiv"

    def test_arxiv_with_version(self):
        assert manual.resolve_metadata_source("2301.00001v2") == "arxiv"

    def test_arxiv_old_style(self):
        assert manual.resolve_metadata_source("hep-th/9901001") == "arxiv"

    def test_arxiv_url(self):
        assert manual.resolve_metadata_source("https://arxiv.org/abs/2301.00001") == "arxiv"

    def test_biorxiv_doi(self):
        assert manual.resolve_metadata_source("10.1101/2024.01.01.573838") == "biorxiv"

    def test_biorxiv_url(self):
        assert (
            manual.resolve_metadata_source("https://doi.org/10.1101/2024.01.01.573838") == "biorxiv"
        )

    def test_acl_doi_routes_to_openalex(self):
        # ACL Anthology has no metadata API — its DOIs route to OpenAlex
        assert manual.resolve_metadata_source("10.18653/v1/2023.acl-long.1") == "openalex"

    def test_generic_publisher_doi_routes_to_openalex(self):
        assert manual.resolve_metadata_source("10.1038/s41586-024-00001-1") == "openalex"

    def test_doi_url_routes_to_openalex(self):
        assert (
            manual.resolve_metadata_source("https://doi.org/10.1038/s41586-024-00001-1")
            == "openalex"
        )

    def test_doi_prefix_routes_to_openalex(self):
        assert manual.resolve_metadata_source("doi:10.1038/s41586-024-00001-1") == "openalex"

    def test_freeform_label_returns_none(self):
        assert manual.resolve_metadata_source("my-paper-2024") is None

    def test_empty_string_returns_none(self):
        assert manual.resolve_metadata_source("") is None


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

    def test_rejects_non_pdf_with_pdf_extension(self, tmp_path):
        # JPEG magic bytes, not %PDF-
        bogus = tmp_path / "fake.pdf"
        bogus.write_bytes(b"\xff\xd8\xff\xe0 some jpeg bytes")

        import uuid

        ident = f"10.1038/test-bogus-{uuid.uuid4().hex[:8]}"
        result = manual.import_local_pdf(str(bogus), ident)
        assert "error" in result
        assert "%PDF-" in result["error"]

    def test_rejects_empty_pdf_file(self, tmp_path):
        empty = tmp_path / "empty.pdf"
        empty.write_bytes(b"")

        import uuid

        ident = f"10.1038/test-empty-{uuid.uuid4().hex[:8]}"
        result = manual.import_local_pdf(str(empty), ident)
        assert "error" in result
        assert "%PDF-" in result["error"]


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
        target = manual.resolve_target(ident)
        canonical = target["canonical"]
        cached = cache.get(namespace, "sections", papers.sections_key(canonical))
        assert cached is not None
        assert len(cached["sections"]) == 2

    def test_cached_sections_carry_markdown_checksum(self, tmp_path):
        """import_markdown must store a markdown_checksum like convert_pdf,
        so a later convert_paper trusts the cache instead of re-parsing."""
        md = tmp_path / "paper.md"
        md.write_text("## Intro\n\nHello.\n\n## Methods\n\nBody.")

        import uuid

        from academic_tools_mcp import cache, papers

        ident = f"10.1038/test-md-checksum-{uuid.uuid4().hex[:8]}"
        result = manual.import_markdown(str(md), ident)

        namespace = result["namespace"]
        canonical = manual.resolve_target(ident)["canonical"]
        cached = cache.get(namespace, "sections", papers.sections_key(canonical))
        assert cached is not None
        # Checksum present and matches the markdown actually written to cache.
        md_path = papers.markdown_path(namespace, canonical)
        assert cached["markdown_checksum"] == papers.markdown_checksum(md_path)

    def test_arxiv_markdown_routes_to_arxiv_namespace(self, tmp_path):
        md = tmp_path / "paper.md"
        md.write_text("## Abstract\n\nText.\n\n## Introduction\n\nMore text.")

        import uuid

        ident = f"2301.{uuid.uuid4().int % 100000:05d}"
        result = manual.import_markdown(str(md), ident)
        assert "error" not in result
        assert result["namespace"] == "arxiv"
        assert "arxiv" in result["markdown_path"]

    def test_rejects_non_utf8_markdown(self, tmp_path):
        bad = tmp_path / "paper.md"
        # latin-1 byte that's not valid UTF-8 mid-stream
        bad.write_bytes(b"## Title\n\nCaf\xe9 \xff bytes\n")

        import uuid

        ident = f"10.1038/test-utf8-{uuid.uuid4().hex[:8]}"
        result = manual.import_markdown(str(bad), ident)
        assert "error" in result
        assert "UTF-8" in result["error"]


# ---------------------------------------------------------------------------
# Re-import / force_refresh, atomicity, encoding
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _c_ctype_locale():
    """Pin LC_CTYPE to C for the body so an encoding-less open() picks ASCII
    — the situation in an LC_ALL=C container/cron job. Restored on exit."""
    import locale

    saved = locale.setlocale(locale.LC_CTYPE)
    locale.setlocale(locale.LC_CTYPE, "C")
    try:
        yield
    finally:
        locale.setlocale(locale.LC_CTYPE, saved)


class TestImportForceRefresh:
    def test_force_refresh_replaces_pdf_and_cascades(self, tmp_path, monkeypatch):
        from academic_tools_mcp import cache, papers

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")

        v1 = tmp_path / "v1.pdf"
        v1.write_bytes(b"%PDF-1.4 version ONE")
        v2 = tmp_path / "v2.pdf"
        v2.write_bytes(b"%PDF-1.4 version TWO is strictly longer")
        ident = "10.1234/force-refresh-pdf"

        first = manual.import_local_pdf(str(v1), ident)
        assert first["cached"] is False
        # A brand-new import has nothing to cascade.
        assert "cascaded_invalidated" not in first

        namespace = first["namespace"]
        canonical = manual.resolve_target(ident)["canonical"]

        # Simulate a prior conversion: markdown + sections cache present.
        md_path = papers.markdown_path(namespace, canonical)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("## Stale\n\nOld converted text.", encoding="utf-8")
        cache.put(
            namespace,
            "sections",
            papers.sections_key(canonical),
            {"sections": [{"title": "Stale", "index": 0}], "markdown_checksum": "x"},
        )

        # Without force_refresh, re-import is a cache hit and old bytes survive.
        hit = manual.import_local_pdf(str(v2), ident)
        assert hit["cached"] is True
        assert manual.pdf_path(ident).read_bytes() == b"%PDF-1.4 version ONE"

        # With force_refresh, new bytes land and derived state is dropped.
        refreshed = manual.import_local_pdf(str(v2), ident, force_refresh=True)
        assert refreshed["cached"] is False
        assert refreshed["cascaded_invalidated"] == ["markdown", "sections"]
        assert manual.pdf_path(ident).read_bytes() == b"%PDF-1.4 version TWO is strictly longer"
        assert not md_path.exists()
        assert cache.get(namespace, "sections", papers.sections_key(canonical)) is None

    def test_force_refresh_replaces_markdown(self, tmp_path, monkeypatch):
        from academic_tools_mcp import cache, papers

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")

        md1 = tmp_path / "v1.md"
        md1.write_text("## Alpha\n\nFirst.", encoding="utf-8")
        md2 = tmp_path / "v2.md"
        md2.write_text("## Beta\n\nSecond.\n\n## Gamma\n\nThird.", encoding="utf-8")
        ident = "10.1234/force-refresh-md"

        first = manual.import_markdown(str(md1), ident)
        assert first["cached"] is False
        assert [s["title"] for s in first["sections"]] == ["Alpha"]

        # Cache hit keeps v1.
        hit = manual.import_markdown(str(md2), ident)
        assert hit["cached"] is True
        assert [s["title"] for s in hit["sections"]] == ["Alpha"]

        refreshed = manual.import_markdown(str(md2), ident, force_refresh=True)
        assert refreshed["cached"] is False
        assert [s["title"] for s in refreshed["sections"]] == ["Beta", "Gamma"]

        namespace = refreshed["namespace"]
        canonical = manual.resolve_target(ident)["canonical"]
        md_path = papers.markdown_path(namespace, canonical)
        assert md_path.read_text(encoding="utf-8") == "## Beta\n\nSecond.\n\n## Gamma\n\nThird."
        cached = cache.get(namespace, "sections", papers.sections_key(canonical))
        assert [s["title"] for s in cached["sections"]] == ["Beta", "Gamma"]
        # Stored checksum matches the new on-disk file.
        assert cached["markdown_checksum"] == papers.markdown_checksum(md_path)


class TestImportAtomicityAndEncoding:
    def test_zero_byte_cached_pdf_is_not_served(self, tmp_path, monkeypatch):
        from academic_tools_mcp import cache

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")

        pdf = tmp_path / "real.pdf"
        pdf.write_bytes(b"%PDF-1.4 real content")
        ident = "10.1234/zero-byte"

        dest = manual.pdf_path(ident)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"")  # truncated/torn leftover from an interrupted import

        result = manual.import_local_pdf(str(pdf), ident)
        assert result["cached"] is False
        assert dest.read_bytes() == b"%PDF-1.4 real content"

    def test_import_local_pdf_atomic_copy_no_torn_file(self, tmp_path, monkeypatch):
        import shutil

        from academic_tools_mcp import cache

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")

        pdf = tmp_path / "p.pdf"
        pdf.write_bytes(b"%PDF-1.4 content")
        ident = "10.1234/atomic-copy"
        dest = manual.pdf_path(ident)

        def boom(*_a, **_kw):
            raise OSError("copy interrupted")

        monkeypatch.setattr(shutil, "copyfileobj", boom)

        result = manual.import_local_pdf(str(pdf), ident)
        assert "error" in result
        assert not dest.exists(), "a failed copy left a torn canonical PDF"
        assert list(dest.parent.glob("*.tmp")) == []

    def test_markdown_import_survives_non_utf8_locale(self, tmp_path, monkeypatch):
        from academic_tools_mcp import cache, papers

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")

        md = tmp_path / "p.md"
        content = "## Café\n\nMüller, François-René — 数据 \U0001f600\n"
        md.write_text(content, encoding="utf-8")
        ident = "10.1234/non-ascii"

        with _c_ctype_locale():
            first = manual.import_markdown(str(md), ident)
            assert "error" not in first
            assert first["cached"] is False
            # Cached-branch re-read must also decode UTF-8 under the C locale.
            second = manual.import_markdown(str(md), ident)
            assert "error" not in second
            assert second["cached"] is True

        namespace = first["namespace"]
        canonical = manual.resolve_target(ident)["canonical"]
        md_path = papers.markdown_path(namespace, canonical)
        assert md_path.read_text(encoding="utf-8") == content


class TestImportPaperTool:
    @pytest.mark.asyncio
    async def test_force_refresh_replaces_and_cascades(self, tmp_path, monkeypatch):
        from academic_tools_mcp import cache, server

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")

        v1 = tmp_path / "v1.pdf"
        v1.write_bytes(b"%PDF-1.4 ONE")
        v2 = tmp_path / "v2.pdf"
        v2.write_bytes(b"%PDF-1.4 TWO is longer")
        ident = "10.1234/tool-force-refresh"

        first = await server.import_paper(str(v1), ident)
        assert first["cached"] is False
        # Tool boundary strips on-disk paths.
        assert "path" not in first

        refreshed = await server.import_paper(str(v2), ident, force_refresh=True)
        assert refreshed["cached"] is False
        assert refreshed["cascaded_invalidated"] == ["markdown", "sections"]
        assert manual.pdf_path(ident).read_bytes() == b"%PDF-1.4 TWO is longer"

    @pytest.mark.asyncio
    async def test_markdown_force_refresh_slims_to_section_count(self, tmp_path, monkeypatch):
        from academic_tools_mcp import cache, server

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")

        md1 = tmp_path / "v1.md"
        md1.write_text("## A\n\nx.", encoding="utf-8")
        md2 = tmp_path / "v2.md"
        md2.write_text("## A\n\nx.\n\n## B\n\ny.", encoding="utf-8")
        ident = "10.1234/tool-md-refresh"

        await server.import_paper(str(md1), ident)
        result = await server.import_paper(str(md2), ident, force_refresh=True)
        assert result["cached"] is False
        assert result["section_count"] == 2
        assert "sections" not in result
        assert "markdown_path" not in result
