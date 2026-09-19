"""The temp-and-rename guarantee, and every way it can be broken."""

import os
import shutil
import stat
import time
from unittest import mock

import pytest

from academic_tools_mcp.store import atomic, cache


def test_write_text_roundtrips_utf8(tmp_path):
    """write_text writes UTF-8 regardless of host locale and lands
    the file in place."""
    path = tmp_path / "nested" / "note.md"
    payload = "## Café\n\nMüller, François-René — 数据\n"
    atomic.write_text(path, payload)
    assert path.read_text(encoding="utf-8") == payload


def test_write_text_no_torn_file_on_failure(tmp_path, monkeypatch):
    """If the rename fails, the canonical path is never created and the
    sibling temp is cleaned up — a reader can't see a half-written file."""
    path = tmp_path / "note.md"

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(atomic.os, "replace", boom)

    with pytest.raises(OSError):
        atomic.write_text(path, "some content")

    assert not path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_text_cleans_up_on_keyboard_interrupt(tmp_path):
    """The cleanup catches BaseException, not Exception: a Ctrl-C landing
    between the write and the rename must still take the temp file with it."""
    path = tmp_path / "note.md"

    def boom(*_a, **_kw):
        raise KeyboardInterrupt

    with mock.patch.object(atomic.os, "replace", boom), pytest.raises(KeyboardInterrupt):
        atomic.write_text(path, "some content")

    assert not path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_copy_cleans_up_on_keyboard_interrupt(tmp_path):
    """Same BaseException contract for the binary copy path."""
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4 source bytes")
    dst = tmp_path / "dst.pdf"

    def boom(*_a, **_kw):
        raise KeyboardInterrupt

    with mock.patch.object(atomic.shutil, "copyfileobj", boom), pytest.raises(KeyboardInterrupt):
        atomic.copy(src, dst)

    assert not dst.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_copy_roundtrips_bytes(tmp_path):
    """copy reproduces the source bytes and lands the file in place."""
    src = tmp_path / "src.bin"
    body = b"%PDF-1.4 binary \x00\x01\x02 payload"
    src.write_bytes(body)
    dst = tmp_path / "sub" / "dst.bin"

    atomic.copy(src, dst)
    assert dst.read_bytes() == body


def test_copy_takes_source_mode_but_its_own_mtime(tmp_path):
    """copymode carries the source's mode across, but the destination's mtime
    is the *write* time. Copying the source mtime would backdate the in-flight
    temp file, and gc_orphan_tmp_files decides what to sweep by mtime."""

    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4 x")
    src.chmod(0o640)
    old = time.time() - 7200
    os.utime(src, (old, old))

    dst = tmp_path / "out" / "dst.pdf"
    atomic.copy(src, dst)

    assert stat.S_IMODE(dst.stat().st_mode) == 0o640, "mode must come from the source"
    assert dst.stat().st_mtime > old + 3600, "mtime must be the write time, not the source's"


@pytest.mark.parametrize("sweep_at", ["copymode", "replace"])
def test_copy_temp_survives_a_concurrent_orphan_sweep(tmp_path, sweep_at):
    """Regression: importing a PDF older than the orphan cutoff used to hand
    gc_orphan_tmp_files a live temp file that looked like an orphan, so a sweep
    racing the copy unlinked the temp and the rename failed with
    FileNotFoundError.

    Swept at both points the copy passes through after the bytes land, not just
    at the rename: the original fix (copystat, then utime back) left the temp
    backdated *between* those two lines and only the later point was covered.
    copymode never backdates it, so there is no such interval left."""

    src = tmp_path / "ancient.pdf"
    src.write_bytes(b"%PDF-1.4 ancient")
    old = time.time() - 30 * 86400  # a PDF the user downloaded a month ago
    os.utime(src, (old, old))

    dst = cache.cache_dir("manual", "pdfs") / "ancient.pdf"

    swept = []

    def sweep_then(real):
        def wrapper(*args):
            # A second server process starting up mid-copy.
            swept.append(cache.gc_orphan_tmp_files())
            return real(*args)

        return wrapper

    if sweep_at == "copymode":
        patch = mock.patch.object(atomic.shutil, "copymode", sweep_then(shutil.copymode))
    else:
        patch = mock.patch.object(atomic.os, "replace", sweep_then(os.replace))

    with patch:
        atomic.copy(src, dst)

    assert swept == [0], "the live temp file must not look like an orphan"
    assert dst.read_bytes() == b"%PDF-1.4 ancient"


def test_copy_does_not_carry_the_sources_flags(tmp_path):
    """copystat carried st_flags too, so importing a quarantined or ``uchg``
    PDF stamped those flags onto the temp file. An immutable cache file is one
    neither gc_orphan_tmp_files nor a forced re-download can remove — and the
    import failed outright, because utime on an immutable file raises."""
    if not hasattr(os, "chflags") or not hasattr(stat, "UF_IMMUTABLE"):
        pytest.skip("no BSD file flags on this platform")

    src = tmp_path / "locked.pdf"
    src.write_bytes(b"%PDF-1.4 x")
    dst = tmp_path / "out" / "locked.pdf"
    try:
        os.chflags(src, stat.UF_IMMUTABLE)
    except OSError:
        pytest.skip("filesystem does not support file flags")

    try:
        atomic.copy(src, dst)
        assert not dst.stat().st_flags & stat.UF_IMMUTABLE
        dst.unlink()  # the point: still removable
    finally:
        os.chflags(src, 0)


def test_copy_no_torn_file_on_failure(tmp_path, monkeypatch):
    """A copy that blows up mid-stream leaves no canonical file (the failure
    mode a plain shutil.copy to dst would torn-write) and no leftover temp."""
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4 source bytes")
    dst = tmp_path / "dst.pdf"

    def boom(*_a, **_kw):
        raise OSError("copy interrupted")

    monkeypatch.setattr(atomic.shutil, "copyfileobj", boom)

    with pytest.raises(OSError):
        atomic.copy(src, dst)

    assert not dst.exists()
    assert list(tmp_path.glob("*.tmp")) == []
