"""``_fast_extract`` — the bundled pymupdf runner for ``mode="fast"``.

This module was at 0% coverage: the only test mentioning it asserted on the
*command string* built for it, never running it. Its whole job is to behave
correctly when things go wrong (pymupdf absent, PDF corrupt, wrong argv), so
none of that was exercised.
"""

import subprocess
import sys

import pytest

from academic_tools_mcp import _fast_extract


class TestArgvHandling:
    @pytest.mark.parametrize("argv", [[], ["prog"], ["prog", "a", "b"]])
    def test_wrong_arity_exits_2_with_usage(self, argv, capsys):
        assert _fast_extract.main(argv) == 2
        assert "usage:" in capsys.readouterr().err

    def test_usage_goes_to_stderr_not_stdout(self, capsys):
        # stdout is the extracted-text channel; anything else there would be
        # cached as if it were the paper.
        _fast_extract.main(["prog"])
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err


class TestMissingDependency:
    def test_import_error_exits_1_with_actionable_message(self, monkeypatch, capsys):
        import builtins

        real_import = builtins.__import__

        def no_pymupdf(name, *args, **kwargs):
            if name == "pymupdf":
                raise ImportError("No module named 'pymupdf'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pymupdf)

        assert _fast_extract.main(["prog", "/nonexistent.pdf"]) == 1
        err = capsys.readouterr().err
        assert "pymupdf is not installed" in err
        assert "academic-tools-mcp[fast]" in err
        assert "PDF_FAST_CONVERTER" in err

    def test_nothing_is_written_to_stdout_on_failure(self, monkeypatch, capsys):
        import builtins

        real_import = builtins.__import__

        def no_pymupdf(name, *args, **kwargs):
            if name == "pymupdf":
                raise ImportError("nope")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_pymupdf)
        _fast_extract.main(["prog", "/nonexistent.pdf"])
        # A non-zero exit with empty markdown must not be cached as a paper.
        assert capsys.readouterr().out == ""


try:
    import pymupdf
except ImportError:  # pragma: no cover - depends on whether [fast] is installed
    pymupdf = None

# Only the extraction tests need the optional dependency; argv handling and the
# missing-dependency path must run everywhere, which a module-level
# importorskip would have prevented.
requires_pymupdf = pytest.mark.skipif(
    pymupdf is None, reason="needs the optional [fast] extra (pip install academic-tools-mcp[fast])"
)


@requires_pymupdf
class TestExtraction:
    def test_corrupt_pdf_exits_1_with_a_clear_message(self, tmp_path, capsys):
        bad = tmp_path / "corrupt.pdf"
        bad.write_bytes(b"not a pdf at all")

        assert _fast_extract.main(["prog", str(bad)]) == 1
        assert "failed to extract text" in capsys.readouterr().err

    def test_missing_file_exits_1(self, tmp_path, capsys):
        assert _fast_extract.main(["prog", str(tmp_path / "absent.pdf")]) == 1
        assert capsys.readouterr().err

    def test_real_pdf_text_goes_to_stdout(self, tmp_path, capsys):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Hello from a real PDF")
        pdf = tmp_path / "ok.pdf"
        doc.save(str(pdf))
        doc.close()

        assert _fast_extract.main(["prog", str(pdf)]) == 0
        assert "Hello from a real PDF" in capsys.readouterr().out

    def test_runs_as_a_module_end_to_end(self, tmp_path):
        # The contract papers._convert_fast relies on: `python -m ... <pdf>`
        # emits text on stdout and exits 0. Nothing tested this for real.
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "Module entry point works")
        pdf = tmp_path / "m.pdf"
        doc.save(str(pdf))
        doc.close()

        proc = subprocess.run(
            [sys.executable, "-m", "academic_tools_mcp._fast_extract", str(pdf)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "Module entry point works" in proc.stdout

    def test_non_ascii_text_survives_the_stdout_round_trip(self, tmp_path):
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "Schrodinger cafe resume")
        pdf = tmp_path / "u.pdf"
        doc.save(str(pdf))
        doc.close()

        proc = subprocess.run(
            [sys.executable, "-m", "academic_tools_mcp._fast_extract", str(pdf)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "Schrodinger" in proc.stdout
