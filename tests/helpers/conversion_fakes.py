"""Shared fixtures and subprocess stand-ins for the conversion tests.

Sibling of ``download_fakes``. The conversion suites split along the same
seams the package does, and all of them need a cache root, a PDF that exists,
a way to pin ``config.get``, and a process that never really ran.
"""

import contextlib
import locale
from pathlib import Path
from unittest.mock import patch

import pytest

from academic_tools_mcp import papers
from academic_tools_mcp.store import cache, stems


@contextlib.contextmanager
def c_ctype_locale():
    """Pin LC_CTYPE to C for the body so an encoding-less read() picks ASCII
    — the situation in an LC_ALL=C container/cron job. Restored on exit."""
    saved = locale.setlocale(locale.LC_CTYPE)
    try:
        locale.setlocale(locale.LC_CTYPE, "C")
    except locale.Error:
        pytest.skip("C locale not available on this host")
    try:
        yield
    finally:
        locale.setlocale(locale.LC_CTYPE, saved)


def seed_markdown(namespace: str, canonical: str, body: str) -> Path:
    """Write markdown straight into the cache path (no subprocess)."""
    md_path = stems.markdown_path(namespace, canonical)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(body, encoding="utf-8")
    return md_path


def seed_index(namespace: str, canonical: str, markdown: str, mode: str | None) -> Path:
    """Seed markdown plus a matching, non-stale section index."""
    md_path = seed_markdown(namespace, canonical, markdown)
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
    return md_path


def env(**overrides):
    """Patch ``config.get`` to return only these settings."""
    return patch("academic_tools_mcp.papers.convert.config.get", side_effect=overrides.get)


def fake_proc(*, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"", pid: int = 909090):
    """A stand-in for ``asyncio.subprocess.Process`` that never really ran."""

    class FakeProc:
        def __init__(self):
            self.pid = pid
            self.returncode = returncode

        async def communicate(self):
            return stdout, stderr

        async def wait(self):
            self.returncode = -9
            return -9

    return FakeProc


def spawning(proc_cls):
    """A ``create_subprocess_exec`` replacement yielding ``proc_cls``."""

    async def _spawn(*args, **kwargs):
        return proc_cls()

    return _spawn
