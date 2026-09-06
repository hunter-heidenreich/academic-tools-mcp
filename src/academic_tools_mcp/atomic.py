"""Atomic file writes: land in a sibling temp, rename into place."""

import contextlib
import os
import shutil
import tempfile
from pathlib import Path


def _new_temp(dst: Path) -> tuple[int, Path]:
    # Same directory as dst: a cross-filesystem rename is not atomic (EXDEV).
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=str(dst.parent))
    return fd, Path(tmp_str)


def write_text(path: Path, payload: str) -> None:
    r"""Write ``payload`` to ``path``, never exposing a half-written file.

    UTF-8 is hardcoded, not a parameter: readers decode UTF-8, so anything else
    writes a file the next read deletes as corrupt.

    Invariant: the bytes on disk are exactly ``payload.encode("utf-8")``, which
    is what lets a caller checksum the string instead of re-reading the file.
    ``newline=""`` is what holds it — the default translates ``\n`` to
    ``os.linesep``.
    """
    fd, tmp_path = _new_temp(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def copy(src: Path, dst: Path) -> None:
    """Copy ``src`` onto ``dst``, never exposing a half-written file.

    Takes the source's mode; the destination's mtime is its own write time.
    """
    fd, tmp_path = _new_temp(dst)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            shutil.copyfileobj(inp, out)
        shutil.copystat(src, tmp_path)
        # Invariant: a temp file's mtime is its own. copystat just backdated it
        # to the source's, which cache.gc_orphan_tmp_files reads as an orphan.
        os.utime(tmp_path, None)
        os.replace(tmp_path, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
