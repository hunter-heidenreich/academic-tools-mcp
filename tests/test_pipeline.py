"""Tool-layer regression tests for tools/pipeline.py.

These cover three contracts the read tools (get_paper_sections /
get_paper_section) must honour but didn't:

  #1 markdown is read UTF-8 even under a non-UTF-8 host locale,
  #2 a markdown file unlinked by a concurrent force_refresh cascade
     surfaces a clean error instead of an uncaught FileNotFoundError,
  #4 convert_paper strips cache filesystem paths from its *error*
     responses, not just its success responses.
"""

import asyncio
import contextlib
from collections import OrderedDict
from pathlib import Path

import pytest

from academic_tools_mcp import cache, manual, papers, server


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
    md_path = papers.markdown_path(namespace, canonical)
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
