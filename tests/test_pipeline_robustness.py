"""Failure modes in the PDF pipeline that used to escape the {error} contract.

Each of these raised a raw exception out of an MCP tool, orphaned a
subprocess, or dropped a document without saying so.
"""

import asyncio
import signal
from pathlib import Path

import pytest

from academic_tools_mcp import cache, cache_search, papers
from academic_tools_mcp.tools import search as search_tools


class TestConverterTemplateErrors:
    """``str.format`` raises KeyError / IndexError / ValueError on a bad
    template, none of which is an OSError — so a typo'd PDF_CONVERTER escaped
    convert_paper as a raw exception. The fast path had no ``try`` at all.
    """

    @pytest.mark.parametrize(
        "template",
        [
            "mytool --in {input} --out {outputdir}",  # unknown placeholder
            "mytool --in {0}",  # positional
            "mytool --in {input",  # unbalanced brace
        ],
    )
    def test_full_builder_raises_named_error(self, monkeypatch, template, tmp_path):
        monkeypatch.setenv("PDF_CONVERTER", template)
        with pytest.raises(papers.ConverterTemplateError) as exc:
            papers._build_converter_command(tmp_path / "x.pdf", tmp_path / "out")
        assert "PDF_CONVERTER" in str(exc.value)

    @pytest.mark.parametrize("template", ["mytool {nope}", "mytool {input"])
    def test_fast_builder_raises_named_error(self, monkeypatch, template, tmp_path):
        monkeypatch.setenv("PDF_FAST_CONVERTER", template)
        with pytest.raises(papers.ConverterTemplateError) as exc:
            papers._build_fast_converter_command(tmp_path / "x.pdf")
        assert "PDF_FAST_CONVERTER" in str(exc.value)

    def test_error_names_the_valid_placeholders(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PDF_CONVERTER", "mytool {wrong}")
        with pytest.raises(papers.ConverterTemplateError) as exc:
            papers._build_converter_command(tmp_path / "x.pdf", tmp_path / "out")
        assert "{input}" in str(exc.value) and "{output_dir}" in str(exc.value)

    def test_valid_templates_still_build(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PDF_CONVERTER", "mytool --in {input} --out {output_dir}")
        cmd = papers._build_converter_command(tmp_path / "x.pdf", tmp_path / "out")
        assert "x.pdf" in cmd and "out" in cmd

    def test_literal_braces_can_be_doubled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PDF_CONVERTER", "mytool {{literal}} {input} {output_dir}")
        cmd = papers._build_converter_command(tmp_path / "x.pdf", tmp_path / "out")
        assert "{literal}" in cmd

    @pytest.mark.asyncio
    async def test_bad_fast_template_returns_error_contract(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PDF_FAST_CONVERTER", "mytool {nope}")
        pdf = tmp_path / "p.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")

        result = await papers.convert_pdf(pdf, "manual", "p", mode="fast")

        assert "error" in result
        assert result["retryable"] is False
        assert "PDF_FAST_CONVERTER" in result["error"]

    @pytest.mark.asyncio
    async def test_bad_full_template_returns_error_contract(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PDF_CONVERTER", "mytool {nope}")
        pdf = tmp_path / "p.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")

        result = await papers.convert_pdf(pdf, "manual", "p")

        assert "error" in result
        assert result["retryable"] is False


class TestCancellationKillsTheConverter:
    """On CancelledError the ``finally`` removed the extraction dir and released
    the conversion lock, but nothing signalled the subprocess — a MinerU run
    kept pinning CPU/GPU with its output directory deleted underneath it.
    """

    @pytest.mark.asyncio
    async def test_kill_process_group_reaps_a_live_process(self):
        proc = await asyncio.create_subprocess_exec("sleep", "60", start_new_session=True)
        assert proc.returncode is None

        await papers._kill_process_group(proc)

        assert proc.returncode is not None, "process was not reaped"

    @pytest.mark.asyncio
    async def test_kill_process_group_is_safe_on_a_reaped_process(self):
        proc = await asyncio.create_subprocess_exec("true", start_new_session=True)
        await proc.wait()
        # Must not signal a pid that may have been recycled.
        await papers._kill_process_group(proc)

    @pytest.mark.asyncio
    async def test_cancelling_a_conversion_kills_the_subprocess(self, monkeypatch, tmp_path):
        pdf = tmp_path / "p.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        monkeypatch.setenv("PDF_CONVERTER", "sleep 60")
        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")

        started: list[object] = []
        real_exec = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            proc = await real_exec(*args, **kwargs)
            started.append(proc)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

        task = asyncio.create_task(papers.convert_pdf(pdf, "manual", "p"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if started:
                break
        assert started, "converter never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        proc = started[0]
        assert proc.returncode is not None, "converter was orphaned on cancellation"
        assert proc.returncode in (-signal.SIGKILL, signal.SIGKILL, 137, -9)


class TestFindInPaperReadHardening:
    """The one pipeline read path that relied on the host locale."""

    @pytest.fixture
    def converted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
        md = papers.markdown_path("manual", "paper-x")
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("# T\n\nSchrödinger and naïve café résumé.\n", encoding="utf-8")
        return md

    @pytest.mark.asyncio
    async def test_non_ascii_is_read_under_a_c_locale(self, converted, monkeypatch):
        # Under LC_ALL=C an implicit read_text() raises UnicodeDecodeError
        # straight out of the tool instead of returning the {error} contract.
        monkeypatch.setenv("LC_ALL", "C")
        monkeypatch.setenv("LANG", "C")

        result = await search_tools.find_in_paper("paper-x", "Schrödinger")

        assert "error" not in result
        assert result["result_count"] == 1

    @pytest.mark.asyncio
    async def test_unlinked_between_check_and_read_degrades_cleanly(self, converted, monkeypatch):
        # A concurrent force_refresh cascade removing the markdown must not
        # raise FileNotFoundError out of the tool.
        original = Path.read_text

        def racing_read(self, *a, **kw):
            raise FileNotFoundError(str(self))

        monkeypatch.setattr(Path, "read_text", racing_read)

        result = await search_tools.find_in_paper("paper-x", "anything")

        assert "error" in result
        assert "suggestion" in result
        monkeypatch.setattr(Path, "read_text", original)


class TestUnindexableDocumentsAreReported:
    """The tokeniser is ASCII-only, so a paper in a non-Latin script produced
    no terms, was dropped from the index, and became permanently invisible to
    search_cached_papers — with no error and no diagnostic.
    """

    @pytest.fixture
    def corpus(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
        md = tmp_path / "manual" / "markdown"
        md.mkdir(parents=True)
        (md / "english.md").write_text("# A\n\nTransformer attention.\n", encoding="utf-8")
        (md / "japanese.md").write_text("# 注意機構\n\n注意力機構の研究。\n", encoding="utf-8")
        (md / "russian.md").write_text("# Сети\n\nнейронных сетей.\n", encoding="utf-8")
        return md

    def test_non_latin_documents_are_recorded_not_dropped(self, corpus):
        reported = cache_search.unindexable()
        stems = {r["stem"] for r in reported}
        assert stems == {"japanese", "russian"}
        assert all(r["reason"] == "no_indexable_tokens" for r in reported)

    def test_indexable_documents_are_not_reported(self, corpus):
        assert "english" not in {r["stem"] for r in cache_search.unindexable()}

    def test_namespace_filter_applies(self, corpus):
        assert cache_search.unindexable("arxiv") == []
        assert len(cache_search.unindexable("manual")) == 2

    def test_search_still_returns_the_indexable_paper(self, corpus):
        hits = cache_search.search("transformer", top_k=5)
        assert [h["canonical_id"] for h in hits] == ["english"]

    @pytest.mark.asyncio
    async def test_tool_surfaces_the_gap(self, corpus):
        result = await search_tools.search_cached_papers("transformer")

        assert result["unindexable_count"] == 2
        assert {r["stem"] for r in result["unindexable"]} == {"japanese", "russian"}
        assert "find_in_paper" in result["unindexable_note"]

    @pytest.mark.asyncio
    async def test_clean_corpus_stays_lean(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
        md = tmp_path / "manual" / "markdown"
        md.mkdir(parents=True)
        (md / "english.md").write_text("# A\n\nTransformer attention.\n", encoding="utf-8")

        result = await search_tools.search_cached_papers("transformer")

        assert "unindexable_count" not in result
        assert "unindexable_note" not in result

    def test_a_document_that_becomes_indexable_is_promoted(self, corpus):
        assert len(cache_search.unindexable()) == 2
        (corpus / "japanese.md").write_text(
            "# 注意機構\n\nattention mechanism study.\n", encoding="utf-8"
        )
        reported = cache_search.unindexable()
        assert {r["stem"] for r in reported} == {"russian"}
        assert "japanese" in {h["canonical_id"] for h in cache_search.search("attention")}

    def test_a_deleted_document_is_pruned_from_the_report(self, corpus):
        (corpus / "japanese.md").unlink()
        assert {r["stem"] for r in cache_search.unindexable()} == {"russian"}
