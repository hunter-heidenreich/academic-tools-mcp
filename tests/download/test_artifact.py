"""Tests for `download/artifact.py` — is the file on disk a usable PDF."""

from __future__ import annotations

from pathlib import Path

from academic_tools_mcp.download import artifact


class TestIsUsablePdf:
    def test_missing_file(self, tmp_path):
        assert artifact.is_usable_pdf(tmp_path / "nope.pdf") is False

    def test_zero_byte_file(self, tmp_path):
        p = tmp_path / "empty.pdf"
        p.write_bytes(b"")
        assert artifact.is_usable_pdf(p) is False

    def test_html_landing_page(self, tmp_path):
        p = tmp_path / "landing.pdf"
        p.write_bytes(b"<!DOCTYPE html><html>Paywall</html>")
        assert artifact.is_usable_pdf(p) is False

    def test_real_pdf_header(self, tmp_path):
        p = tmp_path / "real.pdf"
        p.write_bytes(b"%PDF-1.4\n...")
        assert artifact.is_usable_pdf(p) is True

    def test_directory_is_not_usable(self, tmp_path):
        d = tmp_path / "adir.pdf"
        d.mkdir()
        assert artifact.is_usable_pdf(d) is False


class TestCachedHit:
    def test_returns_none_for_zero_byte(self, tmp_path):
        p = tmp_path / "empty.pdf"
        p.write_bytes(b"")
        assert artifact.cached_hit(p) is None

    def test_returns_payload_for_real_pdf(self, tmp_path):
        p = tmp_path / "real.pdf"
        p.write_bytes(b"%PDF-1.4\nxyz")
        hit = artifact.cached_hit(p)
        assert hit == {"path": str(p), "size_bytes": 12, "cached": True}

    def test_an_unlink_between_the_check_and_the_stat_is_a_miss(self, tmp_path, monkeypatch):
        """The race cached_hit exists to absorb: a concurrent unlink after
        ``is_usable_pdf`` says yes but before the size read. Callers must get
        a miss they can re-download, not an OSError out of an MCP tool."""
        p = tmp_path / "real.pdf"
        p.write_bytes(b"%PDF-1.4\nxyz")

        real_stat = Path.stat
        seen = {"n": 0}

        def vanishing_stat(self, *args, **kwargs):
            seen["n"] += 1
            # Let is_usable_pdf's stat through; fail the size read after it.
            if seen["n"] > 1 and self == p:
                raise OSError("file vanished")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", vanishing_stat)
        assert artifact.cached_hit(p) is None


# ---------------------------------------------------------------------------
# is_definitive_failure / cached_download — the shared protocol
# ---------------------------------------------------------------------------
#
# Verified once here rather than per provider, mirroring how test_throttle.py
# covers the gating primitive.


class TestPdfHeaderScan:
    """`%PDF-` is looked for in a prefix, not demanded at byte 0."""

    _LEADING = b"\xef\xbb\xbf\n  %PDF-1.7\nbody"

    def test_is_usable_pdf_agrees(self, tmp_path):
        p = tmp_path / "bom.pdf"
        p.write_bytes(self._LEADING)
        assert artifact.is_usable_pdf(p)

    def test_a_landing_page_is_still_rejected(self, tmp_path):
        p = tmp_path / "landing.pdf"
        p.write_bytes(b"<html><head><title>Paywall</title></head></html>")
        assert not artifact.is_usable_pdf(p)

    def test_the_scan_does_not_run_past_its_window(self, tmp_path):
        p = tmp_path / "late.pdf"
        p.write_bytes(b"x" * 4096 + b"%PDF-1.7")
        assert not artifact.is_usable_pdf(p)
