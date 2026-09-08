"""Shared fixtures and subprocess stand-ins for the conversion tests.

Sibling of ``_download_fakes``. The conversion suites split along the same
seams the package does, and all of them need a cache root, a PDF that exists,
a way to pin ``config.get``, and a process that never really ran.
"""

from unittest.mock import patch


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
