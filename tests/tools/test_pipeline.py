"""Tool-layer tests for tools/pipeline.py, at the MCP boundary.

The regression half covers three contracts the read tools
(get_paper_sections / get_paper_section) must honour but didn't:

  #1 markdown is read UTF-8 even under a non-UTF-8 host locale,
  #2 a markdown file unlinked by a concurrent force_refresh cascade
     surfaces a clean error instead of an uncaught FileNotFoundError,
  #4 convert_paper strips cache filesystem paths from its *error*
     responses, not just its success responses.

The rest drives each tool through ``server.<name>`` — the shape an agent
actually receives. Everything below the tool layer is faked: the providers'
``download_pdf`` and ``papers.convert_pdf`` return canned payloads, and
markdown/section state is seeded straight into the cache.
"""

import asyncio
import contextlib
from collections import OrderedDict
from pathlib import Path

import pytest

from academic_tools_mcp import manual, papers, server
from academic_tools_mcp.providers import acl, openalex
from academic_tools_mcp.store import cache, stems


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    # Redirect the cache root so each test runs against a clean filesystem.
    monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_section_locks(monkeypatch):
    # Start every test from a clean per-paper lock dict so a lock acquired
    # in one test can't leak into another (and so a held lock is the one the
    # tool re-derives by key).
    monkeypatch.setattr(papers.index, "_section_locks", OrderedDict())


def _seed_markdown(namespace, canonical, body):
    """Write markdown straight into the cache path (no subprocess)."""
    md_path = stems.markdown_path(namespace, canonical)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(body, encoding="utf-8")
    return md_path


@contextlib.contextmanager
def _c_ctype_locale():
    """Pin LC_CTYPE to C for the body so an encoding-less read() picks ASCII
    — the situation in an LC_ALL=C container/cron job. Restored on exit."""
    import locale

    saved = locale.setlocale(locale.LC_CTYPE)
    try:
        locale.setlocale(locale.LC_CTYPE, "C")
    except locale.Error:
        pytest.skip("C locale not available on this host")
    try:
        yield
    finally:
        locale.setlocale(locale.LC_CTYPE, saved)


_NON_ASCII_MD = "## Café\n\nrésumé naïve coöperate ☃ — αβγ\n"


# ---------------------------------------------------------------------------
# #1 — non-UTF-8 host locale must not break the markdown read
# ---------------------------------------------------------------------------


class TestNonUtf8Reads:
    @pytest.mark.asyncio
    async def test_get_paper_section_reads_non_utf8(self, isolated_cache):
        ident = "non-utf8-section"
        target = manual.resolve_target(ident)
        _seed_markdown(target["namespace"], target["canonical"], _NON_ASCII_MD)

        with _c_ctype_locale():
            result = await server.get_paper_section(ident, "Café")

        assert "error" not in result, result
        assert "résumé naïve" in result["content"]

    @pytest.mark.asyncio
    async def test_get_paper_sections_reads_non_utf8(self, isolated_cache):
        # No sections cache seeded → get_paper_sections must read+parse the
        # markdown, which is where the encoding-less read bit.
        ident = "non-utf8-sections"
        target = manual.resolve_target(ident)
        _seed_markdown(target["namespace"], target["canonical"], _NON_ASCII_MD)

        with _c_ctype_locale():
            result = await server.get_paper_sections(ident)

        assert "error" not in result, result
        assert result["total_sections"] == 1
        assert result["sections"][0]["title"] == "Café"


# ---------------------------------------------------------------------------
# #2 — a markdown unlinked by a concurrent cascade is a clean error, not a crash
# ---------------------------------------------------------------------------


class TestConcurrentUnlink:
    @pytest.mark.asyncio
    async def test_get_paper_sections_survives_concurrent_unlink(self, isolated_cache):
        # get_paper_sections checks md_path.exists() *before* acquiring the
        # per-paper lock, then reads under it. A concurrent force_refresh
        # cascade that wins the lock and unlinks the file in that gap must
        # not raise FileNotFoundError.
        ident = "race-sections"
        target = manual.resolve_target(ident)
        ns, canonical = target["namespace"], target["canonical"]
        md_path = _seed_markdown(ns, canonical, "## A\n\nbody\n")

        # Hold the paper's lock so the tool blocks after its outer exists()
        # check (which sees the file) and before its read.
        lock = papers.sections_lock(ns, canonical)
        await lock.acquire()

        task = asyncio.create_task(server.get_paper_sections(ident))
        await asyncio.sleep(0)  # let the task reach the lock acquisition

        md_path.unlink()  # simulate the cascade delete
        lock.release()

        result = await task  # pre-fix: FileNotFoundError; post-fix: clean error
        assert "error" in result
        assert "not converted" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_get_paper_section_clean_error_when_markdown_vanishes(
        self, isolated_cache, monkeypatch
    ):
        # get_paper_section has no await between its exists() check and the
        # read, so the unlink race isn't exploitable in single-threaded
        # asyncio — but the read must still degrade to a clean error rather
        # than an uncaught FileNotFoundError if the file is gone at read time.
        ident = "race-section"
        target = manual.resolve_target(ident)
        md_path = _seed_markdown(target["namespace"], target["canonical"], "## A\n\nbody\n")

        real_read_text = Path.read_text

        def _vanished(self, *args, **kwargs):
            if self == md_path:
                raise FileNotFoundError(self)
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _vanished)

        result = await server.get_paper_section(ident, "A")
        assert "error" in result
        assert "not converted" in result["error"].lower()


# ---------------------------------------------------------------------------
# #4 — convert_paper must strip internal paths from error responses too
# ---------------------------------------------------------------------------


class TestConvertErrorStripsPaths:
    @pytest.mark.asyncio
    async def test_convert_paper_strips_paths_on_error(self, isolated_cache, monkeypatch):
        ident = "strip-error"
        target = manual.resolve_target(ident)
        # The pdf.exists() precheck must pass so we reach papers.convert_pdf.
        pdf = target["pdf_path"]
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4 stub")

        async def _convert_with_path(*args, **kwargs):
            return {
                "error": "boom",
                "retryable": False,
                "markdown_path": "/secret/cache/x.md",
                "path": "/secret/cache/x.pdf",
            }

        monkeypatch.setattr(papers, "convert_pdf", _convert_with_path)

        result = await server.convert_paper(ident)
        assert "error" in result
        assert "markdown_path" not in result
        assert "path" not in result


# ---------------------------------------------------------------------------
# download_pdf — the tool, not the private dispatcher
# ---------------------------------------------------------------------------


def _fake_download(**payload):
    """A provider ``download_pdf`` returning ``payload``, recording its args."""
    seen = {}

    async def _download(identifier, *, force_refresh=False):
        seen["identifier"] = identifier
        seen["force_refresh"] = force_refresh
        return dict(payload)

    return _download, seen


class TestDownloadPdfTool:
    @pytest.mark.asyncio
    async def test_success_reports_size_and_cached_but_never_the_path(
        self, isolated_cache, monkeypatch
    ):
        """The documented success shape, with the cache path filtered out.

        `path` is what `_strip_internal_paths` exists to drop; nothing else
        drove the tool, so a regression that removed the wrapper here would
        have leaked the cache layout to the agent with the suite still green.
        """
        download, _ = _fake_download(path="/cache/arxiv/pdfs/x.pdf", size_bytes=99, cached=False)
        monkeypatch.setattr(server.arxiv, "download_pdf", download)

        result = await server.download_pdf("2301.00001")

        assert result["size_bytes"] == 99
        assert result["cached"] is False
        assert "path" not in result

    @pytest.mark.asyncio
    async def test_acl_extra_fields_survive_the_strip(self, isolated_cache, monkeypatch):
        """`anthology_id` / `pdf_url` are upstream facts, not cache paths."""
        download, _ = _fake_download(
            path="/cache/acl_anthology/pdfs/x.pdf",
            size_bytes=5,
            cached=True,
            anthology_id="P16-1160",
            pdf_url="https://aclanthology.org/P16-1160.pdf",
        )
        monkeypatch.setattr(acl, "download_pdf", download)

        result = await server.download_pdf("10.18653/v1/P16-1160")

        assert result["anthology_id"] == "P16-1160"
        assert result["pdf_url"].startswith("https://")
        assert "path" not in result

    @pytest.mark.asyncio
    async def test_the_flags_reach_the_provider(self, isolated_cache, monkeypatch):
        download, seen = _fake_download(path="/x.pdf", size_bytes=1, cached=True)
        monkeypatch.setattr(server.arxiv, "download_pdf", download)

        await server.download_pdf("arXiv:2301.00001", force_refresh=True)

        assert seen == {"identifier": "arXiv:2301.00001", "force_refresh": True}

    @pytest.mark.asyncio
    async def test_a_definitive_failure_points_at_import_paper(self, isolated_cache, monkeypatch):
        """A withdrawn paper is not a dead end — there is still import_paper.

        Provider errors carry a retry verdict but no recovery advice; agents
        branch on `suggestion`, so this was the one error shape in the module
        that reached them without one.
        """
        download, _ = _fake_download(
            error="No PDF link found for arXiv ID: 2301.00001", retryable=False
        )
        monkeypatch.setattr(server.arxiv, "download_pdf", download)

        result = await server.download_pdf("2301.00001")

        assert "import_paper" in result["suggestion"]
        assert result["retryable"] is False

    @pytest.mark.asyncio
    async def test_a_transient_failure_says_retry_instead(self, isolated_cache, monkeypatch):
        download, _ = _fake_download(error="arXiv server error (HTTP 503).", retryable=True)
        monkeypatch.setattr(server.arxiv, "download_pdf", download)

        result = await server.download_pdf("2301.00001")

        assert "retry" in result["suggestion"].lower()
        assert "import_paper" not in result["suggestion"]

    @pytest.mark.asyncio
    async def test_a_providers_own_suggestion_is_not_overwritten(self, isolated_cache, monkeypatch):
        """`enrich_error` fills a gap; it never argues with the provider."""
        download, _ = _fake_download(
            error="Open-access PDF not found", retryable=False, suggestion="Ask the publisher."
        )
        monkeypatch.setattr(server.arxiv, "download_pdf", download)

        result = await server.download_pdf("2301.00001")

        assert result["suggestion"] == "Ask the publisher."

    @pytest.mark.asyncio
    async def test_a_generic_doi_is_refused_with_both_escape_hatches(self, isolated_cache):
        result = await server.download_pdf("10.1234/generic")

        assert "Cannot auto-download" in result["error"]
        assert "allow_oa_url=True" in result["suggestion"]
        assert "import_paper" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_an_error_result_never_cascades(self, isolated_cache, monkeypatch):
        """The arm of the cascade guard that protects against data loss.

        A failed refresh leaves the previous PDF in place, so its markdown is
        still the right description of it.
        """
        download, _ = _fake_download(error="boom", retryable=True, cached=False)
        monkeypatch.setattr(server.arxiv, "download_pdf", download)

        md_path = _seed_markdown("arxiv", "2301.00001", "# Keep me\n")

        result = await server.download_pdf("2301.00001", force_refresh=True)

        assert "cascaded_invalidated" not in result
        assert md_path.read_text() == "# Keep me\n"

    @pytest.mark.asyncio
    async def test_a_result_without_a_cached_flag_never_cascades(self, isolated_cache, monkeypatch):
        """`is False`, not falsiness: an absent flag is not a claim of freshness."""
        download, _ = _fake_download(path="/x.pdf", size_bytes=1)
        monkeypatch.setattr(server.arxiv, "download_pdf", download)

        md_path = _seed_markdown("arxiv", "2301.00001", "# Keep me\n")

        result = await server.download_pdf("2301.00001", force_refresh=True)

        assert "cascaded_invalidated" not in result
        assert md_path.exists()


# ---------------------------------------------------------------------------
# convert_paper — the failure advice, chosen per cause
# ---------------------------------------------------------------------------


def _seed_pdf(identifier, body=b"%PDF-1.4 stub"):
    target = manual.resolve_target(identifier)
    pdf = target["pdf_path"]
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(body)
    return pdf


def _fake_convert(**payload):
    seen = {}

    async def _convert(pdf_path, namespace, canonical, *, force_refresh=False, mode="full"):
        seen.update(force_refresh=force_refresh, mode=mode, canonical=canonical)
        return dict(payload)

    return _convert, seen


class TestConvertPaperTool:
    @pytest.mark.asyncio
    async def test_success_shape_without_the_markdown_path(self, isolated_cache, monkeypatch):
        _seed_pdf("conv-ok")
        convert, seen = _fake_convert(
            markdown_path="/cache/manual/markdown/conv-ok.md",
            sections=[{"index": 0, "title": "A", "h3s": [], "approx_tokens": 4}],
            sections_detected=True,
            cached=False,
            conversion_mode="full",
        )
        monkeypatch.setattr(papers, "convert_pdf", convert)

        result = await server.convert_paper("conv-ok", force_refresh=True, mode="fast")

        assert result["conversion_mode"] == "full"
        assert result["sections_detected"] is True
        assert result["cached"] is False
        assert "markdown_path" not in result
        assert seen["force_refresh"] is True
        assert seen["mode"] == "fast"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body", [None, b"", b"<html>not a pdf</html>"], ids=["absent", "empty", "not-a-pdf"]
    )
    async def test_an_unusable_pdf_is_a_miss_not_a_conversion(
        self, isolated_cache, monkeypatch, body
    ):
        """`is_usable_pdf`, not `exists()`: a 0-byte or HTML leftover is a miss.

        Handing either to the converter spends the global lock on a file that
        cannot convert, and reports the failure against the paper.
        """
        if body is not None:
            _seed_pdf("conv-bad", body)

        def boom(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("the converter must not be reached")

        monkeypatch.setattr(papers, "convert_pdf", boom)

        result = await server.convert_paper("conv-bad")

        assert "PDF not cached" in result["error"]
        assert "import_paper" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_busy_offers_the_lock_free_mode(self, isolated_cache, monkeypatch):
        _seed_pdf("conv-busy")
        convert, _ = _fake_convert(
            error="Another conversion is in progress.",
            retryable=True,
            busy=True,
            conversion_mode="full",
            in_progress={"namespace": "manual", "canonical": "other"},
        )
        monkeypatch.setattr(papers, "convert_pdf", convert)

        result = await server.convert_paper("conv-busy")

        assert "mode='fast'" in result["suggestion"]
        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_a_full_timeout_offers_fast(self, isolated_cache, monkeypatch):
        _seed_pdf("conv-slow")
        convert, _ = _fake_convert(
            error="PDF conversion timed out after 1800s. Increase PDF_CONVERT_TIMEOUT",
            retryable=False,
            timed_out=True,
            timeout_seconds=1800,
            conversion_mode="full",
        )
        monkeypatch.setattr(papers, "convert_pdf", convert)

        result = await server.convert_paper("conv-slow", mode="full")

        assert "mode='fast'" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_a_fast_timeout_offers_full_and_never_blames_the_pdf(
        self, isolated_cache, monkeypatch
    ):
        """The suggestion must not contradict the error it rides beside.

        `convert.py` says "increase PDF_FAST_CONVERT_TIMEOUT"; a catch-all that
        answers "the PDF is corrupted, do not retry" sends the agent to abandon
        a paper that `mode='full'` converts.
        """
        _seed_pdf("conv-fast-slow")
        convert, _ = _fake_convert(
            error="Fast PDF extraction timed out after 120s. Increase PDF_FAST_CONVERT_TIMEOUT",
            retryable=False,
            timed_out=True,
            timeout_seconds=120,
            conversion_mode="fast",
        )
        monkeypatch.setattr(papers, "convert_pdf", convert)

        result = await server.convert_paper("conv-fast-slow", mode="fast")

        assert "mode='full'" in result["suggestion"]
        assert "PDF_FAST_CONVERT_TIMEOUT" in result["suggestion"]
        assert "corrupt" not in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_a_missing_converter_is_not_reported_as_a_bad_pdf(
        self, isolated_cache, monkeypatch
    ):
        """An operator fix, not a paper defect — so no cause is asserted."""
        _seed_pdf("conv-nospawn")
        convert, _ = _fake_convert(
            error="Could not start fast PDF extractor subprocess: [Errno 2] pdftotext",
            retryable=False,
            conversion_mode="fast",
            pdf_size_mb=1.2,
        )
        monkeypatch.setattr(papers, "convert_pdf", convert)

        result = await server.convert_paper("conv-nospawn", mode="fast")

        assert "Read the error above" in result["suggestion"]
        assert "import_paper" in result["suggestion"]


# ---------------------------------------------------------------------------
# get_paper_sections / get_paper_section — the read tools' own contracts
# ---------------------------------------------------------------------------


def _seed_index(namespace, canonical, markdown, mode):
    """Seed markdown plus a matching, non-stale section index."""
    _seed_markdown(namespace, canonical, markdown)
    sections, detected = papers.parse_sections_and_detect(markdown)
    cache.put(
        namespace,
        "sections",
        stems.sections_key(canonical),
        {
            "sections": sections,
            "sections_detected": detected,
            "markdown_checksum": stems.checksum_text(markdown),
            "conversion_mode": mode,
        },
    )


class TestGetPaperSectionsTool:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["full", "fast", "imported", None])
    async def test_the_provenance_of_the_markdown_is_reported(self, isolated_cache, mode):
        """The sections_note tells the agent to re-convert a fast extraction.

        It cannot act on that without knowing which mode produced the text, and
        the index has recorded it all along.
        """
        ident = f"prov-{mode}"
        target = manual.resolve_target(ident)
        _seed_index(target["namespace"], target["canonical"], "## A\n\nbody\n", mode)

        result = await server.get_paper_sections(ident)

        assert result["conversion_mode"] == mode

    @pytest.mark.asyncio
    async def test_total_approx_tokens_sums_the_sections(self, isolated_cache):
        ident = "totals"
        target = manual.resolve_target(ident)
        _seed_index(
            target["namespace"], target["canonical"], "## A\n\nalpha\n\n## B\n\nbeta\n", "full"
        )

        result = await server.get_paper_sections(ident)

        assert result["total_sections"] == 2
        assert result["total_approx_tokens"] == sum(s["approx_tokens"] for s in result["sections"])

    @pytest.mark.asyncio
    async def test_force_refresh_reaches_the_index(self, isolated_cache):
        """The flag drops the index; the re-parse must agree with the markdown."""
        ident = "refresh-sections"
        target = manual.resolve_target(ident)
        ns, canonical = target["namespace"], target["canonical"]
        _seed_index(ns, canonical, "## A\n\nbody\n", "full")
        # Rewrite the markdown *and* re-stamp the stale checksum, so only an
        # actual drop can produce the new title.
        _seed_markdown(ns, canonical, "## B\n\nbody\n")
        entry = cache.get(ns, "sections", stems.sections_key(canonical))
        entry["markdown_checksum"] = stems.checksum_text("## B\n\nbody\n")
        cache.put(ns, "sections", stems.sections_key(canonical), entry)

        stale = await server.get_paper_sections(ident)
        assert stale["sections"][0]["title"] == "A"

        fresh = await server.get_paper_sections(ident, force_refresh=True)
        assert fresh["sections"][0]["title"] == "B"
        assert fresh["conversion_mode"] == "full"


class TestGetPaperSectionTool:
    @pytest.mark.asyncio
    async def test_an_unconverted_paper_gets_the_pipeline_chain(self, isolated_cache):
        result = await server.get_paper_section("never-converted", "0")

        assert "not converted" in result["error"].lower()
        assert "convert_paper" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_paging_threads_offset_and_max_chars(self, isolated_cache):
        ident = "pager"
        target = manual.resolve_target(ident)
        body = "x" * 100
        _seed_markdown(target["namespace"], target["canonical"], f"## A\n\n{body}\n")

        first = await server.get_paper_section(ident, "A", max_chars=40)
        assert first["chars_returned"] == 40
        assert first["total_chars"] == 100
        assert first["has_more"] is True

        rest = await server.get_paper_section(
            ident, "A", offset=first["next_offset"], max_chars=100
        )
        assert first["content"] + rest["content"] == body
        assert rest["has_more"] is False
        assert rest["next_offset"] is None

    @pytest.mark.asyncio
    async def test_an_out_of_range_index_names_the_range(self, isolated_cache):
        ident = "oor"
        target = manual.resolve_target(ident)
        _seed_markdown(target["namespace"], target["canonical"], "## A\n\nbody\n")

        result = await server.get_paper_section(ident, "7")

        assert "out of range" in result["error"]

    @pytest.mark.asyncio
    async def test_an_ambiguous_title_lists_the_matches(self, isolated_cache):
        ident = "ambig"
        target = manual.resolve_target(ident)
        _seed_markdown(
            target["namespace"], target["canonical"], "## Results\n\na\n\n## Results II\n\nb\n"
        )

        result = await server.get_paper_section(ident, "Results")

        assert "Ambiguous" in result["error"]


# ---------------------------------------------------------------------------
# import_paper — extension routing and the slimmed markdown response
# ---------------------------------------------------------------------------


class TestImportPaperExtensions:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["notes.txt", "paper", "archive.pdf.gz"])
    async def test_an_unsupported_extension_is_rejected_before_any_work(
        self, isolated_cache, tmp_path, monkeypatch, name
    ):
        """Rejected on the extension alone — nothing is routed, nothing written.

        The advice belongs in `suggestion`, not jammed into `error`: agents
        branch on the former.
        """

        def boom(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("import must not be attempted")

        monkeypatch.setattr(manual, "import_local_pdf", boom)
        monkeypatch.setattr(manual, "import_markdown", boom)

        source = tmp_path / name
        source.write_text("hello\n")

        result = await server.import_paper(str(source), "ext-check")

        assert "Unsupported file extension" in result["error"]
        assert ".md/.markdown" in result["suggestion"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("suffix", [".md", ".markdown", ".MD", ".Markdown"])
    async def test_markdown_routes_on_the_lowercased_suffix(self, isolated_cache, tmp_path, suffix):
        source = tmp_path / f"paper{suffix}"
        source.write_text("## A\n\nbody\n\n## B\n\nmore\n", encoding="utf-8")

        result = await server.import_paper(str(source), f"md{suffix}")

        assert result["section_count"] == 2
        assert "sections" not in result
        assert "markdown_path" not in result

    @pytest.mark.asyncio
    async def test_an_uppercase_pdf_still_routes_to_the_pdf_branch(self, isolated_cache, tmp_path):
        source = tmp_path / "paper.PDF"
        source.write_bytes(b"%PDF-1.4 stub")

        result = await server.import_paper(str(source), "upper-pdf")

        assert result["cached"] is False
        assert result["size_bytes"] > 0
        assert "path" not in result

    @pytest.mark.asyncio
    async def test_a_cached_markdown_hit_is_slimmed_too(self, isolated_cache, tmp_path):
        """The cached branch also returns `sections`, so it needs the same slim.

        An agent that feature-detects `section_count` is an agent the response
        shape has failed.
        """
        source = tmp_path / "paper.md"
        source.write_text("## A\n\nbody\n", encoding="utf-8")

        first = await server.import_paper(str(source), "md-cached")
        second = await server.import_paper(str(source), "md-cached")

        assert first["cached"] is False
        assert second["cached"] is True
        assert second["section_count"] == first["section_count"] == 1
        assert "sections" not in second

    @pytest.mark.asyncio
    async def test_an_import_error_passes_through_unslimmed(self, isolated_cache, tmp_path):
        result = await server.import_paper(str(tmp_path / "missing.md"), "gone")

        assert "error" in result
        assert "section_count" not in result

    @pytest.mark.asyncio
    async def test_the_echoed_identifier_is_the_cache_key(self, isolated_cache, tmp_path):
        source = tmp_path / "paper.md"
        source.write_text("## A\n\nbody\n", encoding="utf-8")

        result = await server.import_paper(str(source), "arXiv:2301.00001v2")

        assert result["identifier"] == "2301.00001v2"
        assert result["namespace"] == "arxiv"


# ---------------------------------------------------------------------------
# PMID identity across the pipeline
# ---------------------------------------------------------------------------


class TestPipelineToolsAcceptPmids:
    """A PMID names the same artifact its DOI does, at every pipeline entry.

    ``import_paper`` is the one that has to hold for the others to mean
    anything: it is the writer, so a spelling it files under and the readers
    trade away is an import nothing can read back.
    """

    DOI = "10.1234/example"

    @pytest.fixture(autouse=True)
    def _resolve(self, monkeypatch):
        async def fake_resolve_pmid(pmid, **kwargs):
            return {"doi": self.DOI, "openalex_id": "https://openalex.org/W1"}

        monkeypatch.setattr(openalex, "resolve_pmid", fake_resolve_pmid)

    @staticmethod
    def _markdown(tmp_path):
        source = tmp_path / "paper.md"
        source.write_text("# Intro\n\nthe body\n", encoding="utf-8")
        return source

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "spelling",
        ["pmid:20079334", "20079334", "https://pubmed.ncbi.nlm.nih.gov/20079334/"],
    )
    async def test_import_paper_files_a_pmid_under_the_resolved_doi(
        self, isolated_cache, tmp_path, spelling
    ):
        """The echoed identifier is the cache key, and for a PMID that is the DOI."""
        result = await server.import_paper(str(self._markdown(tmp_path)), spelling)

        assert result["identifier"] == self.DOI
        assert result["namespace"] == "manual"

    @pytest.mark.asyncio
    async def test_a_pmid_import_reads_back_under_either_spelling(self, isolated_cache, tmp_path):
        """Writer and readers agree on one key, so the chain does not dead-end."""
        await server.import_paper(str(self._markdown(tmp_path)), "pmid:20079334")

        sections = await server.get_paper_sections("20079334")
        assert sections["total_sections"] == 1
        assert sections["conversion_mode"] == "imported"

        hits = await server.find_in_paper("pmid:20079334", "the body")
        assert hits["paper_identifier"] == self.DOI
        assert hits["result_count"] == 1

    @pytest.mark.asyncio
    async def test_an_import_orphaned_under_a_pmid_stem_is_repaired_on_resolve(
        self, isolated_cache
    ):
        """The lazy migration: an older build's PMID-keyed import becomes readable.

        Its ``conversion_mode`` rides along, so the repair does not expose an
        operator's own markdown to ``download_pdf``'s cascade.
        """
        _seed_markdown("manual", "pmid:20079334", "# Intro\n\nstranded\n")
        cache.put(
            "manual",
            "sections",
            stems.sections_key("pmid:20079334"),
            {
                "sections": [],
                "sections_detected": True,
                "markdown_checksum": stems.checksum_text("# Intro\n\nstranded\n"),
                "conversion_mode": "imported",
            },
        )

        sections = await server.get_paper_sections("pmid:20079334")

        assert sections["conversion_mode"] == "imported"
        assert stems.markdown_path("manual", self.DOI).exists()

    @pytest.mark.asyncio
    async def test_a_failed_pmid_lookup_writes_nothing(self, isolated_cache, tmp_path, monkeypatch):
        """An unresolvable PMID is an error, never a fallback to a manual label."""

        async def unresolvable(pmid, **kwargs):
            return {"error": f"No work found for PMID: {pmid}", "not_found": True}

        monkeypatch.setattr(openalex, "resolve_pmid", unresolvable)

        result = await server.import_paper(str(self._markdown(tmp_path)), "pmid:20079334")

        assert result["not_found"] is True
        assert not stems.markdown_path("manual", "pmid:20079334").exists()
