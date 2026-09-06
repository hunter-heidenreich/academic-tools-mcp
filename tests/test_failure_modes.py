"""Failure modes that turned a working request into an exception or nonsense.

Each of these fires on a path the happy-path tests never reach: a full disk,
a PDF with no text layer, a converter that writes to stderr or emits several
candidate outputs.
"""

import asyncio
from pathlib import Path

import pytest

from academic_tools_mcp import _stats, atomic, cache, papers


class TestCacheWriteFailureIsAbsorbed:
    """``cache.put`` runs inside every provider's ``fetch`` closure *after*
    the network request already succeeded. Raising on a full or read-only
    disk threw away data we had just paid an HTTP request for, and surfaced
    as an uncaught OSError out of an MCP tool instead of the {error}
    contract.
    """

    @pytest.fixture
    def full_disk(self, monkeypatch):
        def enospc(*a, **kw):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(atomic, "write_text", enospc)

    def test_put_returns_false_instead_of_raising(self, full_disk):
        assert cache.put("arxiv", "papers", "2301.00001", {"title": "T"}) is False

    def test_put_negative_returns_false_instead_of_raising(self, full_disk):
        assert cache.put_negative("arxiv", "papers", "missing", {"error": "nope"}) is False

    def test_put_succeeds_normally(self):
        assert cache.put("arxiv", "papers", "2301.00001", {"title": "T"}) is True
        assert cache.get("arxiv", "papers", "2301.00001") == {"title": "T"}

    def test_failure_is_counted_for_the_operator(self, full_disk):
        _stats.reset()
        cache.put("arxiv", "papers", "2301.00001", {"title": "T"})
        counters = _stats.snapshot()["providers"]["arxiv"]
        assert counters["cache_write_failures"] == 1

    @pytest.mark.asyncio
    async def test_a_lookup_still_returns_its_data_on_a_full_disk(self, full_disk):
        # The whole point: the caller already has the answer. Not caching it
        # costs a repeat lookup; raising costs the answer.
        from academic_tools_mcp import _singleflight

        async def fetch():
            cache.put("arxiv", "papers", "2301.00001", {"title": "Fetched"})
            return {"title": "Fetched"}

        result = await cache.cached_lookup(
            single_flight=_singleflight.SingleFlight(),
            namespace="arxiv",
            entity="papers",
            canonical="2301.00001",
            positive_ttl=999.0,
            fetch=fetch,
        )
        assert result == {"title": "Fetched"}

    def test_other_errors_still_propagate(self, monkeypatch):
        def boom(*a, **kw):
            raise ValueError("a real bug")

        monkeypatch.setattr(atomic, "write_text", boom)
        with pytest.raises(ValueError, match="a real bug"):
            cache.put("arxiv", "papers", "x", {"a": 1})


class TestEmptyConversion:
    """A converter can exit 0 having produced an empty markdown file (a
    0-page PDF, an image-only scan). Reading a section then produced the
    literal, useless ``out of range (0--1)``.
    """

    @pytest.mark.parametrize("markdown", ["", "   ", "\n\n\n", "\t \n  \n"])
    @pytest.mark.parametrize("section", [0, "Introduction"])
    def test_says_what_actually_happened(self, markdown, section):
        result = papers.get_section_content(markdown, section)

        assert "empty" in result["error"].lower()
        assert "0--1" not in result["error"]
        assert "suggestion" in result
        assert result["retryable"] is False

    def test_suggestion_names_the_likely_cause(self):
        result = papers.get_section_content("", 0)
        assert "text layer" in result["suggestion"]
        assert "convert_paper" in result["suggestion"]

    def test_a_real_document_still_range_checks_normally(self):
        result = papers.get_section_content("## A\n\nbody\n", 9)
        assert "out of range (0-0)" in result["error"]

    def test_a_real_document_still_reports_a_missing_title(self):
        result = papers.get_section_content("## A\n\nbody\n", "Nonexistent")
        assert "No section matching" in result["error"]


class TestConverterSubprocessPlumbing:
    @pytest.fixture
    def pdf(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)
        # PDF_CONVERTER_VENV / PDF_CONVERT_TIMEOUT are cleared by conftest's
        # `_scrub_config_env`, not here.
        p = tmp_path / "paper.pdf"
        p.write_bytes(b"%PDF-1.4\n")
        return p

    @pytest.mark.asyncio
    async def test_a_chatty_converter_cannot_bury_its_error(self, pdf, monkeypatch):
        # The command used to be wrapped as `{cmd} 2>&1`, merging stderr into
        # stdout and leaving the stderr pipe permanently empty. The error
        # message is `stdout + stderr` truncated to its last 500 chars, so a
        # converter that keeps logging after it fails pushed its own error out
        # of that window. Separated, stderr is appended last and survives.
        monkeypatch.setenv(
            "PDF_CONVERTER",
            ">&2 echo 'REAL_ERROR_HERE'; python3 -c \"print('progress line ' * 200)\"; exit 3",
        )

        result = await papers.convert_pdf(pdf, "manual", "paper")

        assert "error" in result
        assert "REAL_ERROR_HERE" in result["error"], (
            "the converter's actual error was buried by its own stdout chatter"
        )

    @pytest.mark.asyncio
    async def test_stdout_diagnostics_still_reach_the_error(self, pdf, monkeypatch):
        monkeypatch.setenv("PDF_CONVERTER", "echo 'STDOUT_DIAGNOSTIC'; exit 4")

        result = await papers.convert_pdf(pdf, "manual", "paper")

        assert "STDOUT_DIAGNOSTIC" in result["error"]
        assert "exit 4" in result["error"]

    @pytest.mark.asyncio
    async def test_stderr_is_captured_separately(self, pdf, monkeypatch):
        # The command used to be wrapped as `{cmd} 2>&1`. That was worse than
        # "stderr is always empty": `;` and `&&` bind tighter than the
        # redirect, so it merged streams only for a *single-command*
        # converter. With PDF_CONVERTER_VENV set — `source ... && {cmd}` —
        # the redirect attached to whatever the last command happened to be,
        # so whether stderr was captured depended on the shape of the
        # operator's converter string.
        captured = {}
        real = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            proc = await real(*args, **kwargs)
            real_comm = proc.communicate

            async def communicate():
                out, err = await real_comm()
                captured["stdout"], captured["stderr"] = out, err
                return out, err

            proc.communicate = communicate
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
        # One simple command: `;` and `&&` bind tighter than a redirect, so a
        # compound command would leave stderr intact even with `2>&1` and the
        # test could not tell the two apart.
        monkeypatch.setenv(
            "PDF_CONVERTER",
            "python3 -c \"import sys; sys.stderr.write('ONLY_ON_STDERR'); sys.exit(5)\"",
        )

        await papers.convert_pdf(pdf, "manual", "paper")

        assert b"ONLY_ON_STDERR" in captured["stderr"]
        assert b"ONLY_ON_STDERR" not in (captured["stdout"] or b"")

    @pytest.mark.asyncio
    async def test_output_choice_does_not_depend_on_filesystem_order(
        self, pdf, monkeypatch, tmp_path
    ):
        # MinerU emits several .md files per run. `list(glob(...))` returns
        # them in filesystem order, so which one *became* the paper was
        # arbitrary. Feed a deliberately hostile order and assert the
        # shallowest-then-alphabetical file still wins.
        monkeypatch.setenv(
            "PDF_CONVERTER",
            "mkdir -p {output_dir}/nested && "
            "printf '# Nested\\n\\nnested body\\n' > {output_dir}/nested/aaa.md && "
            "printf '# Top\\n\\ntop body\\n' > {output_dir}/zzz.md",
        )

        real_glob = Path.glob

        def hostile_glob(self, pattern):
            # Ascending path order puts "<dir>/nested/aaa.md" before
            # "<dir>/zzz.md", so an unsorted `list(glob(...))[0]` picks the
            # nested one. The fix sorts by depth first, so the top-level file
            # must still win.
            return iter(sorted(real_glob(self, pattern)))

        monkeypatch.setattr(Path, "glob", hostile_glob)

        result = await papers.convert_pdf(pdf, "manual", "paper")

        assert "error" not in result, result
        first_line = Path(result["markdown_path"]).read_text(encoding="utf-8").split("\n")[0]
        assert first_line == "# Top", f"picked {first_line!r} under a hostile glob order"

    @pytest.mark.asyncio
    async def test_exact_stem_match_is_preferred(self, pdf, monkeypatch):
        monkeypatch.setenv(
            "PDF_CONVERTER",
            "printf '# Other\\n\\nother\\n' > {output_dir}/aaa.md && "
            "printf '# Exact\\n\\nexact\\n' > {output_dir}/paper.md",
        )

        result = await papers.convert_pdf(pdf, "manual", "paper")

        assert "error" not in result, result
        assert Path(result["markdown_path"]).read_text(encoding="utf-8").startswith("# Exact")
