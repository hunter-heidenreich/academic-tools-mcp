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
            papers.convert._build_converter_command(tmp_path / "x.pdf", tmp_path / "out")
        assert "PDF_CONVERTER" in str(exc.value)

    @pytest.mark.parametrize("template", ["mytool {nope}", "mytool {input"])
    def test_fast_builder_raises_named_error(self, monkeypatch, template, tmp_path):
        monkeypatch.setenv("PDF_FAST_CONVERTER", template)
        with pytest.raises(papers.ConverterTemplateError) as exc:
            papers.convert._build_fast_converter_command(tmp_path / "x.pdf")
        assert "PDF_FAST_CONVERTER" in str(exc.value)

    def test_error_names_the_valid_placeholders(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PDF_CONVERTER", "mytool {wrong}")
        with pytest.raises(papers.ConverterTemplateError) as exc:
            papers.convert._build_converter_command(tmp_path / "x.pdf", tmp_path / "out")
        assert "{input}" in str(exc.value) and "{output_dir}" in str(exc.value)

    def test_valid_templates_still_build(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PDF_CONVERTER", "mytool --in {input} --out {output_dir}")
        cmd = papers.convert._build_converter_command(tmp_path / "x.pdf", tmp_path / "out")
        assert "x.pdf" in cmd and "out" in cmd

    def test_literal_braces_can_be_doubled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PDF_CONVERTER", "mytool {{literal}} {input} {output_dir}")
        cmd = papers.convert._build_converter_command(tmp_path / "x.pdf", tmp_path / "out")
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

        await papers.convert._kill_process_group(proc)

        assert proc.returncode is not None, "process was not reaped"

    @pytest.mark.asyncio
    async def test_kill_process_group_is_safe_on_a_reaped_process(self):
        proc = await asyncio.create_subprocess_exec("true", start_new_session=True)
        await proc.wait()
        # Must not signal a pid that may have been recycled.
        await papers.convert._kill_process_group(proc)

    @pytest.mark.asyncio
    async def test_cancelling_a_conversion_kills_the_subprocess(self, monkeypatch, tmp_path):
        pdf = tmp_path / "p.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        monkeypatch.setenv("PDF_CONVERTER", "sleep 60")
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")

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

    @pytest.mark.asyncio
    async def test_cancelling_a_fast_extraction_kills_the_subprocess(self, monkeypatch, tmp_path):
        """The fast path owns a second copy of the cancellation handler.

        It runs outside the global conversion lock, so nothing else would stop
        an orphaned extractor from holding the per-paper lock's work open.
        """
        pdf = tmp_path / "p.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        monkeypatch.setenv("PDF_FAST_CONVERTER", "sleep 60")
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")

        started: list[object] = []
        real_exec = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            proc = await real_exec(*args, **kwargs)
            started.append(proc)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

        task = asyncio.create_task(papers.convert_pdf(pdf, "manual", "p", mode="fast"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if started:
                break
        assert started, "fast extractor never started"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        proc = started[0]
        assert proc.returncode is not None, "fast extractor was orphaned on cancellation"
        assert proc.returncode in (-signal.SIGKILL, signal.SIGKILL, 137, -9)


class TestFindInPaperReadHardening:
    """The one pipeline read path that relied on the host locale."""

    @pytest.fixture
    def converted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)
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
    """A document FTS5 derives no terms from can never match, so it is
    recorded with a reason rather than being silently absent from the corpus.

    "No terms" means what ``unicode61`` means by it — no letter or digit in
    any script. The probe used to ask an ASCII-only tokeniser plus a MATCH for
    the five ASCII vowels, which reported a Japanese or Cyrillic paper as
    unusable when FTS5 had indexed it perfectly well. See
    ``TestNonLatinDocumentsAreIndexedNotReported``.
    """

    @pytest.fixture
    def corpus(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)
        md = tmp_path / "manual" / "markdown"
        md.mkdir(parents=True)
        (md / "english.md").write_text("# A\n\nTransformer attention.\n", encoding="utf-8")
        # Genuinely tokenless: punctuation and symbols carry no letters or
        # digits in any script, so unicode61 finds nothing to index.
        (md / "punctuation.md").write_text("# ---\n\n... !!! ??? ***\n", encoding="utf-8")
        (md / "emoji.md").write_text("# \U0001f642\n\n\U0001f600 \U0001f389\n", encoding="utf-8")
        return md

    def test_tokenless_documents_are_recorded_not_dropped(self, corpus):
        reported = cache_search.unindexable()
        stems = {r["stem"] for r in reported}
        assert stems == {"punctuation", "emoji"}
        assert all(r["reason"] == "no_indexable_tokens" for r in reported)

    def test_a_tokenless_document_leaves_no_postings(self, corpus):
        """Absent from the index, exactly as an unreadable paper is.

        It used to be inserted into both tables and *then* declared unusable,
        leaving a row that can never match — a different on-disk state for the
        same "not searchable" answer.
        """
        cache_search.search("transformer")
        con = cache_search._connect()
        try:
            rowids = {
                r["rowid"]
                for r in con.execute(
                    "SELECT rowid FROM files WHERE unindexable = 'no_indexable_tokens'"
                )
            }
            assert rowids, "the fixture seeds two tokenless papers"
            for table in ("fts", "fts_norm"):
                indexed = {r[0] for r in con.execute(f"SELECT rowid FROM {table}")}
                assert not (rowids & indexed)
        finally:
            con.close()

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
        assert {r["stem"] for r in result["unindexable"]} == {"punctuation", "emoji"}
        assert "find_in_paper" in result["unindexable_note"]

    @pytest.mark.asyncio
    async def test_the_note_states_the_actual_reason(self, corpus):
        """The note is built from the reasons present, not from one asserted
        cause. It blamed "the tokeniser is ASCII-only, so non-Latin scripts
        yield no terms" — which the any-Unicode-letter probe made false, and
        which was never true of these files: they have no letters in any
        script. An agent reading it would go looking for an encoding problem."""
        note = (await search_tools.search_cached_papers("transformer"))["unindexable_note"]

        assert "no letters or digits" in note
        assert "ASCII" not in note
        assert "non-Latin" not in note

    @pytest.mark.asyncio
    async def test_an_unreadable_paper_gets_its_own_reason(self, corpus, monkeypatch):
        """``unreadable`` is an I/O failure, not a tokenisation one, and the
        recovery differs — re-import rather than "there is nothing to index"."""
        monkeypatch.setattr(
            cache_search,
            "unindexable",
            lambda *a, **kw: [{"namespace": "manual", "stem": "x", "reason": "unreadable"}],
        )

        note = (await search_tools.search_cached_papers("transformer"))["unindexable_note"]

        assert "could not be read" in note
        assert "import_paper" in note

    @pytest.mark.asyncio
    async def test_clean_corpus_stays_lean(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)
        md = tmp_path / "manual" / "markdown"
        md.mkdir(parents=True)
        (md / "english.md").write_text("# A\n\nTransformer attention.\n", encoding="utf-8")

        result = await search_tools.search_cached_papers("transformer")

        assert "unindexable_count" not in result
        assert "unindexable_note" not in result

    def test_an_empty_document_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)
        md = tmp_path / "manual" / "markdown"
        md.mkdir(parents=True)
        (md / "blank.md").write_text("", encoding="utf-8")
        assert {r["stem"] for r in cache_search.unindexable()} == {"blank"}

    def test_a_document_that_becomes_indexable_is_promoted(self, corpus):
        assert len(cache_search.unindexable()) == 2
        (corpus / "punctuation.md").write_text(
            "# Title\n\nattention mechanism study.\n", encoding="utf-8"
        )
        reported = cache_search.unindexable()
        assert {r["stem"] for r in reported} == {"emoji"}
        assert "punctuation" in {h["canonical_id"] for h in cache_search.search("attention")}

    def test_a_deleted_document_is_pruned_from_the_report(self, corpus):
        (corpus / "punctuation.md").unlink()
        assert {r["stem"] for r in cache_search.unindexable()} == {"emoji"}


class TestNonLatinDocumentsAreIndexedNotReported:
    """A paper in a non-Latin script is indexed, searchable, and not reported.

    ``unicode61`` tokenises on Unicode character class, so Cyrillic, Greek and
    CJK text all produce terms. The old probe called them ``no_indexable_tokens``
    anyway, and once a non-Latin *query* could reach the index that report
    became actively misleading: ``unindexable_note`` told the agent to fall
    back to ``find_in_paper`` on a paper ``search_cached_papers`` would find.
    """

    @pytest.fixture
    def corpus(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)
        md = tmp_path / "manual" / "markdown"
        md.mkdir(parents=True)
        (md / "russian.md").write_text("# Сети\n\nнейронных сетей.\n", encoding="utf-8")
        (md / "greek.md").write_text("# Δίκτυα\n\nνευρωνικά δίκτυα.\n", encoding="utf-8")
        (md / "japanese.md").write_text("# 研究\n\n用語 注意力機構 が現れる。\n", encoding="utf-8")
        return md

    def test_none_are_reported_unindexable(self, corpus):
        assert cache_search.unindexable() == []

    @pytest.mark.parametrize(
        ("query", "expected"),
        [("нейронных", "russian"), ("νευρωνικά", "greek"), ("注意力機構", "japanese")],
    )
    def test_each_is_findable_in_its_own_script(self, corpus, query, expected):
        assert [h["canonical_id"] for h in cache_search.search(query)] == [expected]

    @pytest.mark.asyncio
    async def test_the_tool_reports_no_gap(self, corpus):
        result = await search_tools.search_cached_papers("нейронных")

        assert result["result_count"] == 1
        assert "unindexable_count" not in result
        assert "unindexable_note" not in result

    def test_cjk_matches_whole_runs_not_substrings(self, tmp_path, monkeypatch):
        # A documented limit of ``unicode61``, not a defect: it does not
        # segment CJK, so a run delimited by whitespace or punctuation is one
        # token. Pinned so a future tokenizer change is a deliberate choice.
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)
        md = tmp_path / "manual" / "markdown"
        md.mkdir(parents=True)
        (md / "run.md").write_text("# 研究\n\n注意力機構の研究。\n", encoding="utf-8")

        assert [h["canonical_id"] for h in cache_search.search("注意力機構の研究")] == ["run"]
        # A substring of that run does not match, and the paper is still not
        # reported unindexable — it has terms, just not this one.
        assert cache_search.search("注意力機構") == []
        assert cache_search.unindexable() == []
