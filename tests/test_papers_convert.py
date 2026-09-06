"""Running a converter: command building, the subprocess, and both modes.

Covers ``papers.convert``. Every subprocess here is faked — the suite asserts
what the driver does with an outcome, never that a real converter works.
"""

import asyncio
import shlex
from pathlib import Path

import pytest

from academic_tools_mcp import cache, papers
from academic_tools_mcp.papers import convert_pdf
from academic_tools_mcp.papers.convert import (
    _DEFAULT_FAST_CONVERT_TIMEOUT,
    _DEFAULT_PDF_CONVERT_TIMEOUT,
    _build_converter_command,
    _build_fast_converter_command,
    _resolve_convert_timeout,
    _resolve_fast_convert_timeout,
)

from ._checksums import markdown_checksum
from ._conversion_fakes import env, fake_proc, spawning

# ---------------------------------------------------------------------------
# convert_pdf cache paths (subprocess path is not exercised here)
# ---------------------------------------------------------------------------


class TestConvertPdfCachePaths:
    """When the markdown is already cached, convert_pdf must never invoke
    the slow subprocess — even if the sections cache is missing or stale.
    """

    @pytest.fixture
    def fail_if_subprocess(self, monkeypatch):
        # Any attempt to spawn a subprocess in this test is a bug
        async def _fail(*args, **kwargs):
            raise AssertionError("convert_pdf should not invoke the subprocess on this path")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)

    def _seed_markdown(self, namespace, canonical, body):
        md_path = papers.markdown_path(namespace, canonical)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(body)
        return md_path

    @pytest.mark.asyncio
    async def test_uses_cached_sections_when_checksum_matches(
        self, isolated_cache, fail_if_subprocess
    ):
        ns, canonical = "test", "doc-1"
        md_path = self._seed_markdown(ns, canonical, "## A\n\nx\n\n## B\n\ny\n")
        sections = papers.parse_sections(md_path.read_text())
        cache.put(
            ns,
            "sections",
            papers.sections_key(canonical),
            {
                "sections": sections,
                "markdown_checksum": markdown_checksum(md_path),
            },
        )

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert result["cached"] is True
        assert result["sections"] == sections

    @pytest.mark.asyncio
    async def test_reparses_when_sections_cache_missing(self, isolated_cache, fail_if_subprocess):
        # The bug fix: markdown exists, sections cache missing -> re-parse,
        # do NOT re-run the subprocess (which would also overwrite markdown).
        ns, canonical = "test", "doc-2"
        self._seed_markdown(ns, canonical, "## Intro\n\nhi\n\n## Methods\n\nstuff\n")

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert result["cached"] is True
        titles = [s["title"] for s in result["sections"]]
        assert titles == ["Intro", "Methods"]

        # And the sections cache is now populated for next time
        refreshed = cache.get(ns, "sections", papers.sections_key(canonical))
        assert refreshed is not None
        assert refreshed["sections"] == result["sections"]

    @pytest.mark.asyncio
    async def test_reparses_when_checksum_stale(self, isolated_cache, fail_if_subprocess):
        # Markdown was edited externally so the cached checksum no longer matches.
        ns, canonical = "test", "doc-3"
        self._seed_markdown(ns, canonical, "## Old\n\nold body\n")
        cache.put(
            ns,
            "sections",
            papers.sections_key(canonical),
            {
                "sections": [{"index": 0, "title": "Old", "h3s": [], "approx_tokens": 1}],
                "markdown_checksum": "deadbeef",  # deliberately wrong
            },
        )

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert result["cached"] is True
        # Re-parsed from current markdown, not the stale cache
        assert [s["title"] for s in result["sections"]] == ["Old"]

        refreshed = cache.get(ns, "sections", papers.sections_key(canonical))
        assert refreshed["markdown_checksum"] != "deadbeef"

    @pytest.mark.asyncio
    async def test_reparses_when_checksum_missing(self, isolated_cache, fail_if_subprocess):
        # A sections cache entry written before the checksum field existed
        # (or by any path that didn't persist one) must be treated as stale,
        # not valid — otherwise external edits to the markdown go undetected.
        ns, canonical = "test", "doc-5"
        md_path = self._seed_markdown(ns, canonical, "## Fresh\n\nbody\n")
        cache.put(
            ns,
            "sections",
            papers.sections_key(canonical),
            {
                "sections": [{"index": 0, "title": "Stale", "h3s": [], "approx_tokens": 1}],
                "markdown_checksum": None,
            },
        )

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert [s["title"] for s in result["sections"]] == ["Fresh"]

        refreshed = cache.get(ns, "sections", papers.sections_key(canonical))
        assert refreshed["markdown_checksum"] == markdown_checksum(md_path)

    @pytest.mark.asyncio
    async def test_errors_when_neither_markdown_nor_pdf_exists(self, isolated_cache):
        ns, canonical = "test", "doc-4"
        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert "error" in result
        assert "PDF not found" in result["error"]

    @pytest.mark.asyncio
    async def test_force_refresh_drops_markdown_and_sections(self, isolated_cache):
        # force_refresh must blow away the cached markdown AND the
        # sections cache, so the next call falls through to "PDF not
        # found" (no PDF here) — proving both halves were cleared.
        ns, canonical = "test", "doc-force-refresh"
        md_path = self._seed_markdown(ns, canonical, "## A\n\nbody\n")
        cache.put(
            ns,
            "sections",
            papers.sections_key(canonical),
            {
                "sections": [{"index": 0, "title": "A", "h3s": [], "approx_tokens": 1}],
                "markdown_checksum": markdown_checksum(md_path),
            },
        )

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical, force_refresh=True)
        assert "error" in result
        assert not md_path.exists(), "force_refresh should unlink the markdown"
        assert cache.get(ns, "sections", papers.sections_key(canonical)) is None, (
            "force_refresh should invalidate the sections cache"
        )

    @pytest.mark.asyncio
    async def test_concurrent_callers_reparse_only_once(
        self, isolated_cache, fail_if_subprocess, monkeypatch
    ):
        # Two concurrent callers on the same paper with no sections cache
        # must serialise via the per-paper lock: only the first re-parses,
        # the second sees the freshly written cache entry. Without the lock
        # both would re-parse and race to write.
        ns, canonical = "test", "concurrent-1"
        self._seed_markdown(ns, canonical, "## A\n\nx\n\n## B\n\ny\n")

        # Reset the lock dict so this test starts from a clean slate
        # regardless of test ordering.
        from collections import OrderedDict

        monkeypatch.setattr(papers.index, "_section_locks", OrderedDict())

        parse_calls = 0
        real_parse = papers.index.parse_sections_and_detect

        def counting_parse(markdown):
            nonlocal parse_calls
            parse_calls += 1
            return real_parse(markdown)

        monkeypatch.setattr(papers.index, "parse_sections_and_detect", counting_parse)

        results = await asyncio.gather(
            convert_pdf(Path("/nonexistent.pdf"), ns, canonical),
            convert_pdf(Path("/nonexistent.pdf"), ns, canonical),
            convert_pdf(Path("/nonexistent.pdf"), ns, canonical),
        )

        assert all(r.get("cached") is True for r in results)
        titles = [s["title"] for s in results[0]["sections"]]
        assert titles == ["A", "B"]
        assert parse_calls == 1, (
            f"expected exactly one re-parse under the per-paper lock, got {parse_calls}"
        )

    @pytest.mark.asyncio
    async def test_markdown_unlinked_between_exists_and_lock_is_a_miss(
        self, isolated_cache, monkeypatch
    ):
        # TOCTOU regression: convert_pdf checks md_path.exists() *before*
        # acquiring the per-paper lock, then reads inside it. A concurrent
        # unlink (force_refresh, or the download_pdf cascade which doesn't take
        # the lock) in that gap must NOT raise FileNotFoundError — it must be
        # treated as a cache miss and fall through cleanly.
        ns, canonical = "test", "toctou-1"
        md_path = self._seed_markdown(ns, canonical, "## A\n\nbody\n")

        from collections import OrderedDict

        monkeypatch.setattr(papers.index, "_section_locks", OrderedDict())

        # Hold the paper's lock so the convert_pdf task blocks after its outer
        # exists() check (which sees the file) and before its read.
        lock = papers.sections_lock(ns, canonical)
        await lock.acquire()

        task = asyncio.create_task(convert_pdf(Path("/nonexistent.pdf"), ns, canonical))
        # Let the task run up to the lock acquisition.
        await asyncio.sleep(0)

        # Simulate the unsynchronised cascade delete, then release the lock.
        md_path.unlink()
        lock.release()

        # Pre-fix this raised FileNotFoundError; post-fix it's a clean miss.
        result = await task
        assert "error" in result
        assert "PDF not found" in result["error"]


class TestConvertPdfSubprocessFailures:
    """The subprocess path must turn every failure into an {error, ...} dict;
    nothing should bubble as a raw exception.
    """

    @pytest.mark.asyncio
    async def test_spawn_failure_returns_error_dict(self, isolated_cache, real_pdf, monkeypatch):
        async def _spawn_fail(*args, **kwargs):
            raise FileNotFoundError("bash: not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_fail)
        result = await convert_pdf(real_pdf, "test", "spawn-fail-1")
        assert "error" in result
        assert "Could not start" in result["error"]
        assert result["retryable"] is False
        assert "pdf_size_mb" in result

    @pytest.mark.asyncio
    async def test_timeout_kills_process_group_and_returns_error(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        # FakeProc whose communicate() never finishes — exactly the
        # failure mode the timeout exists to bound.
        killed_pgids: list[int] = []

        class HangingProc:
            pid = 424242
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            async def wait(self):
                # Pretend the SIGKILL took effect immediately.
                self.returncode = -9
                return -9

        async def _fake_spawn(*args, **kwargs):
            assert kwargs.get("start_new_session") is True, (
                "convert_pdf must spawn with start_new_session=True so the "
                "whole process tree can be signalled on timeout"
            )
            return HangingProc()

        def _fake_getpgid(pid):
            return pid

        def _fake_killpg(pgid, sig):
            killed_pgids.append(pgid)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        monkeypatch.setattr("academic_tools_mcp.papers.convert.os.getpgid", _fake_getpgid)
        monkeypatch.setattr("academic_tools_mcp.papers.convert.os.killpg", _fake_killpg)
        # Force a tiny timeout via env so the test runs fast.
        monkeypatch.setattr(
            "academic_tools_mcp.papers.convert.config.get",
            lambda key: "0.05" if key == "PDF_CONVERT_TIMEOUT" else None,
        )

        result = await convert_pdf(real_pdf, "test", "timeout-1")

        assert "error" in result
        assert "timed out" in result["error"].lower()
        assert result["retryable"] is False
        assert result["timed_out"] is True
        assert result["timeout_seconds"] == pytest.approx(0.05)
        assert "pdf_size_mb" in result
        assert killed_pgids == [HangingProc.pid], (
            "timeout path must SIGKILL the converter's process group"
        )

    @pytest.mark.asyncio
    async def test_second_caller_gets_busy_while_one_in_flight(self, isolated_cache, monkeypatch):
        # Server runs at most one PDF conversion at a time. The second
        # caller, while another conversion is mid-flight, must get a
        # structured `busy` error — NOT queue, NOT spawn its own
        # subprocess. The first caller's run is unaffected.
        pdf_a = isolated_cache / "a.pdf"
        pdf_a.write_bytes(b"%PDF-1.4 stub a")
        pdf_b = isolated_cache / "b.pdf"
        pdf_b.write_bytes(b"%PDF-1.4 stub b")

        # Reset the global lock + state so we don't inherit anything
        # from another test that ran in this loop.
        monkeypatch.setattr(papers.convert, "_global_convert_lock", asyncio.Lock())
        monkeypatch.setattr(papers.convert, "_current_conversion", None)

        spawn_count = 0
        spawn_started = asyncio.Event()
        release_subprocess = asyncio.Event()

        class HangingProc:
            pid = 313131
            returncode = None

            async def communicate(self):
                # Wait until the test releases us, then return success-ish.
                # We won't actually parse anything because returncode!=0
                # is set below to short-circuit the post-processing.
                await release_subprocess.wait()
                self.returncode = 1
                return b"converter aborted by test", b""

            async def wait(self):
                self.returncode = -9
                return -9

        async def fake_spawn(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            spawn_started.set()
            return HangingProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

        # First caller — start it as a background task so we can run
        # the second caller while the first one is still in the lock.
        task_a = asyncio.create_task(convert_pdf(pdf_a, "test", "paper-a"))

        # Wait until the first task has actually entered the subprocess
        # block (i.e. has acquired the global lock). spawn_started fires
        # from inside `async with _global_convert_lock`.
        await spawn_started.wait()

        # Second caller — different paper. Must get busy without spawning.
        result_b = await convert_pdf(pdf_b, "test", "paper-b")
        assert result_b.get("busy") is True
        assert result_b.get("retryable") is True
        assert "already in progress" in result_b["error"]
        assert result_b["in_progress"]["canonical"] == "paper-a"
        assert result_b["in_progress"]["namespace"] == "test"
        assert result_b["in_progress"]["elapsed_seconds"] >= 0
        assert "pdf_size_mb" in result_b

        # Third caller, same paper as the in-flight one — still busy.
        # We deliberately do not collapse same-paper requests; the second
        # caller could observe a half-written cache, so making them retry
        # after the first one finishes is the safe answer.
        result_a2 = await convert_pdf(pdf_a, "test", "paper-a")
        assert result_a2.get("busy") is True

        # Only the first caller ever spawned a subprocess.
        assert spawn_count == 1, (
            f"expected exactly one subprocess spawn under the global "
            f"convert lock, got {spawn_count}"
        )

        # Let the first caller finish so the test doesn't leak the task.
        release_subprocess.set()
        result_a = await task_a
        assert "error" in result_a  # converter exited 1 by design

        # Lock is released — a fresh caller can now proceed (would spawn
        # again if we let it). Just confirm the gate is open.
        assert papers.convert._global_convert_lock.locked() is False
        assert papers.convert._current_conversion is None

    @pytest.mark.asyncio
    async def test_binary_output_does_not_crash(self, isolated_cache, real_pdf, monkeypatch):
        # A converter that crashes can dump binary garbage on stdout.
        # The non-zero exit handler used to call .decode() with strict UTF-8
        # and raise UnicodeDecodeError on those bytes.
        binary_garbage = b"\xff\xfe\xfd boom \xc3\x28 invalid utf-8 \x00\x01"

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            spawning(fake_proc(returncode=1, stdout=binary_garbage)),
        )
        result = await convert_pdf(real_pdf, "test", "binary-out-1")
        assert "error" in result
        assert "exit 1" in result["error"]
        assert result["retryable"] is False


class TestConvertPdfTempDirCleanup:
    """The extraction dir must be removed on every exit path — success *and*
    all four failure paths — so failed conversions don't leak it. The dir is
    a private ``tempfile.mkdtemp`` created by ``papers.convert._make_extraction_dir``;
    these tests monkeypatch that helper to a known path so the fake subprocess
    can populate it and the assertions can check it was cleaned up.
    """

    @pytest.fixture
    def extract_dir(self, tmp_path, monkeypatch):
        # Replace the mkdtemp-based helper with a deterministic dir, populated
        # with a non-.md leftover so the assertion proves the finally removes
        # real content rather than just an absent path.
        d = tmp_path / "extract"

        def _make(canonical):
            d.mkdir(parents=True, exist_ok=True)
            (d / "images").mkdir(exist_ok=True)
            (d / "images" / "fig.png").write_bytes(b"\x89PNG fake")
            return d

        monkeypatch.setattr(papers.convert, "_make_extraction_dir", _make)
        return d

    @pytest.mark.asyncio
    async def test_cleanup_on_spawn_failure(
        self, isolated_cache, real_pdf, extract_dir, monkeypatch
    ):
        async def _spawn_fail(*args, **kwargs):
            raise FileNotFoundError("bash: not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_fail)
        result = await convert_pdf(real_pdf, "test", "tmp-spawn-fail")
        assert "error" in result
        assert not extract_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_timeout(self, isolated_cache, real_pdf, extract_dir, monkeypatch):
        class HangingProc:
            pid = 525252
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            async def wait(self):
                self.returncode = -9
                return -9

        async def _fake_spawn(*args, **kwargs):
            return HangingProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        monkeypatch.setattr("academic_tools_mcp.papers.convert.os.getpgid", lambda pid: pid)
        monkeypatch.setattr("academic_tools_mcp.papers.convert.os.killpg", lambda pgid, sig: None)
        monkeypatch.setattr(
            "academic_tools_mcp.papers.convert.config.get",
            lambda key: "0.05" if key == "PDF_CONVERT_TIMEOUT" else None,
        )
        result = await convert_pdf(real_pdf, "test", "tmp-timeout")
        assert result.get("timed_out") is True
        assert not extract_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_nonzero_exit(
        self, isolated_cache, real_pdf, extract_dir, monkeypatch
    ):
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            spawning(fake_proc(returncode=1, stdout=b"converter blew up")),
        )
        result = await convert_pdf(real_pdf, "test", "tmp-nonzero")
        assert "exit 1" in result["error"]
        assert not extract_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_no_markdown(self, isolated_cache, real_pdf, extract_dir, monkeypatch):
        # Converter "succeeds" (exit 0) but emits no .md file (only the
        # leftover the fixture seeded), so the glob finds nothing.
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawning(fake_proc()))
        result = await convert_pdf(real_pdf, "test", "tmp-no-md")
        assert "no markdown output" in result["error"]
        assert not extract_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_success(self, isolated_cache, real_pdf, extract_dir, monkeypatch):
        # A converter that "produces" markdown: write a .md into the extraction
        # dir the moment convert_pdf spawns it, mimicking a real run.
        async def _fake_spawn(*args, **kwargs):
            (extract_dir / f"{real_pdf.stem}.md").write_text("## Intro\n\nHello world.")

            return fake_proc()()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "tmp-success")
        assert result.get("cached") is False
        assert result["sections"]
        assert not extract_dir.exists()


# ---------------------------------------------------------------------------
# _build_converter_command
# ---------------------------------------------------------------------------


class TestBuildConverterCommand:
    """Tests for the configurable PDF converter command builder."""

    def test_default_is_mineru(self):
        with env():
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        # Paths with no shell-special chars shlex.quote to themselves (no quotes).
        assert cmd == "mineru -p /a/b.pdf -o /tmp/out"

    def test_named_marker_backend(self):
        with env(PDF_CONVERTER="marker"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert cmd == "marker_single /a/b.pdf --output_dir /tmp/out"

    def test_custom_command_template(self):
        # Custom templates use BARE placeholders (values arrive shell-quoted).
        custom = "my-tool convert --src {input} --dst {output_dir}"
        with env(PDF_CONVERTER=custom):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert cmd == "my-tool convert --src /a/b.pdf --dst /tmp/out"

    def test_venv_activation(self):
        with env(PDF_CONVERTER="mineru", PDF_CONVERTER_VENV="~/.venvs/mineru"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert "source" in cmd
        assert ".venvs/mineru/bin/activate" in cmd
        assert cmd.endswith("mineru -p /a/b.pdf -o /tmp/out")

    def test_no_venv_by_default(self):
        with env(PDF_CONVERTER="marker"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert "source" not in cmd
        assert "activate" not in cmd


# ---------------------------------------------------------------------------
# Shell-quoting safety (command injection hardening)
# ---------------------------------------------------------------------------


class TestConverterCommandQuoting:
    """A canonical-derived path containing shell metacharacters must be
    shell-quoted before it reaches ``bash -c``, so it can't be interpreted
    as a command. Manual double-quoting around ``{input}`` did not protect
    against ``$()`` / backticks / embedded quotes.
    """

    # A path that, unquoted (or merely double-quoted), would run a command
    # and break out of the surrounding quotes.
    HOSTILE = Path('/cache/pdfs/x"$(touch pwned)`id`.pdf')

    def test_full_command_quotes_hostile_input(self):
        with env():
            cmd = _build_converter_command(self.HOSTILE, Path("/tmp/out"))
        # The path survives as exactly one shell token — so bash treats it as a
        # single literal argument and never command-substitutes the $()/`id`.
        assert str(self.HOSTILE) in shlex.split(cmd)
        # The quoted form is what's embedded (the $() is inert inside it).
        assert shlex.quote(str(self.HOSTILE)) in cmd

    def test_full_command_quotes_output_dir(self):
        with env():
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/o$(x)"))
        assert "/tmp/o$(x)" in shlex.split(cmd)
        assert shlex.quote("/tmp/o$(x)") in cmd

    def test_venv_path_is_quoted(self):
        with env(PDF_CONVERTER="mineru", PDF_CONVERTER_VENV="/opt/v env"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        # The space-containing venv path is quoted so `source` gets one arg.
        assert shlex.quote("/opt/v env/bin/activate") in cmd

    def test_fast_command_quotes_hostile_input(self):
        with env():
            cmd = _build_fast_converter_command(self.HOSTILE)
        assert str(self.HOSTILE) in shlex.split(cmd)
        assert shlex.quote(str(self.HOSTILE)) in cmd


# ---------------------------------------------------------------------------
# _resolve_convert_timeout
# ---------------------------------------------------------------------------


class TestResolveConvertTimeout:
    """PDF_CONVERT_TIMEOUT parsing — bad input must never raise."""

    def _env(self, value):
        return env(PDF_CONVERT_TIMEOUT=value)

    def test_unset_uses_default(self):
        with self._env(None):
            assert _resolve_convert_timeout() == _DEFAULT_PDF_CONVERT_TIMEOUT

    def test_explicit_seconds(self):
        with self._env("600"):
            assert _resolve_convert_timeout() == 600.0

    def test_float_seconds(self):
        with self._env("90.5"):
            assert _resolve_convert_timeout() == 90.5

    def test_zero_disables(self):
        with self._env("0"):
            assert _resolve_convert_timeout() is None

    def test_negative_disables(self):
        with self._env("-1"):
            assert _resolve_convert_timeout() is None

    def test_word_disables(self):
        for word in ("none", "off", "disabled", "NONE", "Off"):
            with self._env(word):
                assert _resolve_convert_timeout() is None, word

    def test_garbage_falls_back_to_default(self):
        with self._env("not-a-number"):
            assert _resolve_convert_timeout() == _DEFAULT_PDF_CONVERT_TIMEOUT

    def test_non_finite_falls_back_to_default(self):
        """``float("nan")`` is neither ``> 0`` nor ``<= 0``.

        It slipped past both branches and reached ``asyncio.wait_for`` as a
        deadline no elapsed time can ever satisfy.
        """
        for word in ("nan", "inf", "-inf"):
            with self._env(word):
                assert _resolve_convert_timeout() == _DEFAULT_PDF_CONVERT_TIMEOUT, word


# ---------------------------------------------------------------------------
# _build_fast_converter_command
# ---------------------------------------------------------------------------


class TestBuildFastConverterCommand:
    """The fast extractor command builder mirrors the full one but emits to
    stdout and has no output-dir. {python} expands to the server interpreter.
    """

    def test_default_is_pdftotext(self):
        with env():
            cmd = _build_fast_converter_command(Path("/a/b.pdf"))
        assert cmd == "pdftotext -layout /a/b.pdf -"

    def test_named_pymupdf_uses_server_interpreter(self):
        import shlex
        import sys

        with env(PDF_FAST_CONVERTER="pymupdf"):
            cmd = _build_fast_converter_command(Path("/a/b.pdf"))
        assert cmd == (
            f"{shlex.quote(sys.executable)} -m academic_tools_mcp._fast_extract /a/b.pdf"
        )

    def test_custom_command_template(self):
        # Bare placeholder per the shell-quoted-substitution contract.
        custom = "my-extractor --src {input}"
        with env(PDF_FAST_CONVERTER=custom):
            cmd = _build_fast_converter_command(Path("/a/b.pdf"))
        assert cmd == "my-extractor --src /a/b.pdf"


# ---------------------------------------------------------------------------
# _resolve_fast_convert_timeout
# ---------------------------------------------------------------------------


class TestResolveFastConvertTimeout:
    """PDF_FAST_CONVERT_TIMEOUT parsing — same rules as the full timeout."""

    def _env(self, value):
        return env(PDF_FAST_CONVERT_TIMEOUT=value)

    def test_unset_uses_default(self):
        with self._env(None):
            assert _resolve_fast_convert_timeout() == _DEFAULT_FAST_CONVERT_TIMEOUT

    def test_explicit_seconds(self):
        with self._env("30"):
            assert _resolve_fast_convert_timeout() == 30.0

    def test_word_disables(self):
        with self._env("none"):
            assert _resolve_fast_convert_timeout() is None

    def test_garbage_falls_back_to_default(self):
        with self._env("nope"):
            assert _resolve_fast_convert_timeout() == _DEFAULT_FAST_CONVERT_TIMEOUT


class TestResolveFastConvertTimeoutBoundaries:
    """The fast resolver had four of the eight cases its full-mode twin has.
    Both delegate to ``config.number``, so the gap was provenance, not risk —
    but the env var an operator actually sets was unpinned at the boundary.
    """

    def _resolve(self, monkeypatch, value):
        monkeypatch.setenv("PDF_FAST_CONVERT_TIMEOUT", value)
        return papers.convert._resolve_fast_convert_timeout()

    def test_zero_disables(self, monkeypatch):
        assert self._resolve(monkeypatch, "0") is None

    def test_negative_disables(self, monkeypatch):
        assert self._resolve(monkeypatch, "-1") is None

    def test_float_seconds(self, monkeypatch):
        assert self._resolve(monkeypatch, "2.5") == 2.5

    def test_non_finite_falls_back_to_default(self, monkeypatch):
        assert self._resolve(monkeypatch, "nan") == _DEFAULT_FAST_CONVERT_TIMEOUT
        assert self._resolve(monkeypatch, "inf") == _DEFAULT_FAST_CONVERT_TIMEOUT


# ---------------------------------------------------------------------------
# convert_pdf(mode="fast")
# ---------------------------------------------------------------------------


class TestConvertPdfFastMode:
    """Fast mode: lightweight stdout-capturing extraction that runs outside
    the global conversion lock and tags its output conversion_mode='fast'.
    """

    @staticmethod
    def _stdout_proc(text: bytes, returncode: int = 0, stderr: bytes = b""):
        return fake_proc(returncode=returncode, stdout=text, stderr=stderr)

    @pytest.mark.asyncio
    async def test_fast_extraction_caches_markdown_and_sections(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        proc_cls = self._stdout_proc(b"## Intro\n\nHello from pdftotext.")

        async def _fake_spawn(*args, **kwargs):
            return proc_cls()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "fast-1", mode="fast")

        assert result["cached"] is False
        assert result["conversion_mode"] == "fast"
        assert result["sections"]

        # Markdown landed in the cache and the section index carries the
        # checksum plus the conversion_mode tag.
        md_path = papers.markdown_path("test", "fast-1")
        assert md_path.exists()
        cached = cache.get("test", "sections", papers.sections_key("fast-1"))
        assert cached["conversion_mode"] == "fast"
        assert cached["markdown_checksum"] == markdown_checksum(md_path)

    @pytest.mark.asyncio
    async def test_fast_mode_runs_outside_global_lock(self, isolated_cache, real_pdf, monkeypatch):
        # Hold the global convert lock — a full conversion would get `busy`.
        # Fast mode must succeed anyway because it never takes the lock.
        monkeypatch.setattr(papers.convert, "_global_convert_lock", asyncio.Lock())
        monkeypatch.setattr(papers.convert, "_current_conversion", None)
        await papers.convert._global_convert_lock.acquire()
        try:
            proc_cls = self._stdout_proc(b"Plain extracted text.")

            async def _fake_spawn(*args, **kwargs):
                return proc_cls()

            monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
            result = await convert_pdf(real_pdf, "test", "fast-nolock", mode="fast")

            assert result.get("busy") is not True
            assert result["conversion_mode"] == "fast"
            assert result["cached"] is False
        finally:
            papers.convert._global_convert_lock.release()

    @pytest.mark.asyncio
    async def test_fast_mode_cached_markdown_skips_subprocess(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        # Seed the markdown cache, then assert the subprocess is never spawned.
        md_path = papers.markdown_path("test", "fast-cached")
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("## Cached\n\nAlready converted.")

        async def _fail(*args, **kwargs):
            raise AssertionError("fast mode must not spawn when markdown is cached")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)
        result = await convert_pdf(real_pdf, "test", "fast-cached", mode="fast")
        assert result["cached"] is True
        assert result["sections"]

    @pytest.mark.asyncio
    async def test_fast_convert_cached_preserves_full_conversion_mode(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        # _convert_fast's cached re-check fires when a racing writer creates
        # the markdown after convert_pdf's top-level cache miss. In that window
        # it must NOT relabel a previously FULL-converted paper as degraded
        # "fast". Call _convert_fast directly to exercise exactly that branch.
        ns, canonical = "test", "fast-preserve-full"
        md_path = papers.markdown_path(ns, canonical)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("## Intro\n\nFull-quality body.")
        cache.put(
            ns,
            "sections",
            papers.sections_key(canonical),
            {
                "sections": papers.parse_sections(md_path.read_text()),
                "markdown_checksum": markdown_checksum(md_path),
                "conversion_mode": "full",
            },
        )

        async def _fail(*args, **kwargs):
            raise AssertionError("must not spawn when markdown is cached")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)
        result = await papers.convert._convert_fast(real_pdf, ns, canonical, 0.1)
        assert result["cached"] is True
        assert result["conversion_mode"] == "full"
        # And the recorded mode in the sections cache stays "full".
        cached = cache.get(ns, "sections", papers.sections_key(canonical))
        assert cached["conversion_mode"] == "full"

    @pytest.mark.asyncio
    async def test_fast_mode_nonzero_exit_returns_error(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        proc_cls = self._stdout_proc(b"", returncode=2, stderr=b"pdftotext: boom")

        async def _fake_spawn(*args, **kwargs):
            return proc_cls()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "fast-fail", mode="fast")
        assert "error" in result
        assert "exit 2" in result["error"]
        assert result["retryable"] is False
        assert result["conversion_mode"] == "fast"

    @pytest.mark.asyncio
    async def test_fast_mode_empty_output_returns_error(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        # Exit 0 but whitespace-only stdout (e.g. a scanned image-only PDF).
        proc_cls = self._stdout_proc(b"   \n\f\n  ", returncode=0)

        async def _fake_spawn(*args, **kwargs):
            return proc_cls()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "fast-empty", mode="fast")
        assert "error" in result
        assert "no text" in result["error"]
        assert result["conversion_mode"] == "fast"
        # Nothing should have been cached.
        assert not papers.markdown_path("test", "fast-empty").exists()

    @pytest.mark.asyncio
    async def test_fast_mode_spawn_failure_returns_error(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        async def _spawn_fail(*args, **kwargs):
            raise FileNotFoundError("pdftotext: not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_fail)
        result = await convert_pdf(real_pdf, "test", "fast-spawn", mode="fast")
        assert "error" in result
        assert "Could not start" in result["error"]
        assert result["retryable"] is False
        assert result["conversion_mode"] == "fast"

    @pytest.mark.asyncio
    async def test_fast_mode_timeout_kills_and_returns_error(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        killed_pgids: list[int] = []

        class HangingProc:
            pid = 717171
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            async def wait(self):
                self.returncode = -9
                return -9

        async def _fake_spawn(*args, **kwargs):
            assert kwargs.get("start_new_session") is True
            return HangingProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        monkeypatch.setattr("academic_tools_mcp.papers.convert.os.getpgid", lambda pid: pid)
        monkeypatch.setattr(
            "academic_tools_mcp.papers.convert.os.killpg",
            lambda pgid, sig: killed_pgids.append(pgid),
        )
        monkeypatch.setattr(
            "academic_tools_mcp.papers.convert.config.get",
            lambda key: "0.05" if key == "PDF_FAST_CONVERT_TIMEOUT" else None,
        )
        result = await convert_pdf(real_pdf, "test", "fast-timeout", mode="fast")
        assert result.get("timed_out") is True
        assert result["conversion_mode"] == "fast"
        assert result["retryable"] is False
        assert killed_pgids == [HangingProc.pid]


# ---------------------------------------------------------------------------
# The transforms convert_pdf applies to converter output
# ---------------------------------------------------------------------------


class TestFinalizeMarkdown:
    """``_finalize_markdown``'s two rewrites, previously asserted nowhere.

    ``manual.import_markdown`` pins the *opposite* side (an operator's own file
    is stored verbatim), which left the converter side — the one that runs on
    every conversion — unguarded.
    """

    def _finalize(self, tmp_path, monkeypatch, raw):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        md_path = papers.markdown_path("test", "finalize")
        papers.convert._finalize_markdown("test", "finalize", md_path, raw, "full")
        return md_path.read_text(encoding="utf-8")

    def test_image_paths_are_stripped_but_captions_kept(self, tmp_path, monkeypatch):
        out = self._finalize(tmp_path, monkeypatch, "![Figure 1](images/fig1.png)\n")
        assert out == "![Figure 1]()\n"

    def test_a_parenthesised_image_path_does_not_leak_into_the_body(self, tmp_path, monkeypatch):
        # A flat ``\([^)]*\)`` stops at the first ``)`` inside the path and
        # leaves ``.png)`` behind as body text the agent reads as content.
        # Converter leaf filenames derive from the PDF stem, and an Elsevier
        # PII DOI carries parentheses.
        raw = "![Figure 1](images/10.1002_(sici)_fig1.png)\n"
        out = self._finalize(tmp_path, monkeypatch, raw)
        assert out == "![Figure 1]()\n"
        assert ".png" not in out

    def test_a_bracketed_caption_still_loses_its_path(self, tmp_path, monkeypatch):
        # A flat ``\[([^\]]*)\]`` skips this entirely, leaving a path into the
        # deleted extraction dir in agent-visible markdown.
        out = self._finalize(tmp_path, monkeypatch, "![see [1] for detail](/tmp/x/f.png)\n")
        assert out == "![see [1] for detail]()\n"

    def test_ordinary_links_are_untouched(self, tmp_path, monkeypatch):
        out = self._finalize(tmp_path, monkeypatch, "See [the paper](https://example.org/a).\n")
        assert out == "See [the paper](https://example.org/a).\n"

    def test_trailing_whitespace_is_normalised_per_line(self, tmp_path, monkeypatch):
        out = self._finalize(tmp_path, monkeypatch, "## A   \n\nbody\t\n")
        assert out == "## A\n\nbody\n"


class TestDisabledTimeouts:
    """``PDF_CONVERT_TIMEOUT=none`` (and its fast-mode twin) are documented in
    the README and in the timeout error messages, but the branch they select —
    awaiting ``communicate()`` with no ``wait_for`` around it — had never run.
    """

    def _no_wait_for(self, monkeypatch):
        """Any use of wait_for on this path is the bug under test."""

        async def _fail(*args, **kwargs):
            raise AssertionError("a disabled timeout must not wrap communicate() in wait_for")

        monkeypatch.setattr(asyncio, "wait_for", _fail)

    @pytest.mark.asyncio
    async def test_full_mode_runs_unbounded(self, isolated_cache, real_pdf, monkeypatch, tmp_path):
        monkeypatch.setenv("PDF_CONVERT_TIMEOUT", "none")
        assert papers.convert._resolve_convert_timeout() is None

        extract = tmp_path / "extract"
        extract.mkdir()
        (extract / "fake.md").write_text("## Done\n\nbody\n")
        monkeypatch.setattr(papers.convert, "_make_extraction_dir", lambda canonical: extract)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawning(fake_proc()))
        self._no_wait_for(monkeypatch)

        result = await convert_pdf(real_pdf, "test", "unbounded-full")
        assert [s["title"] for s in result["sections"]] == ["Done"]
        assert result["conversion_mode"] == "full"

    @pytest.mark.asyncio
    async def test_fast_mode_runs_unbounded(self, isolated_cache, real_pdf, monkeypatch):
        monkeypatch.setenv("PDF_FAST_CONVERT_TIMEOUT", "0")
        assert papers.convert._resolve_fast_convert_timeout() is None

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            spawning(fake_proc(stdout=b"## Done\n\nbody\n")),
        )
        self._no_wait_for(monkeypatch)

        result = await convert_pdf(real_pdf, "test", "unbounded-fast", mode="fast")
        assert [s["title"] for s in result["sections"]] == ["Done"]
        assert result["conversion_mode"] == "fast"
