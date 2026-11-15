"""Tests for ``server.py`` itself.

Nearly everything reachable as ``server.<tool>`` is defined in
``tools/`` and tested there. What is left is the debug-tool gate.
"""

from pathlib import Path

import pytest

from academic_tools_mcp import app, manual, server
from academic_tools_mcp.net import clients
from academic_tools_mcp.store import cache, stems

# ---------------------------------------------------------------------------
# Debug tools are NOT registered in the default configuration
# ---------------------------------------------------------------------------


class TestDebugToolsGating:
    """get_server_stats exists in the codebase but must only register
    when ENABLE_DEBUG_TOOLS is truthy in the env. The default-off
    posture matters because the snapshot exposes operational data
    (counter values, in-flight queues) that agents shouldn't branch on.
    """

    def test_debug_tool_not_registered_by_default(self):
        # The env var was unset on import, so the @mcp.tool block was
        # skipped and the function should not exist at module scope.
        assert not hasattr(server, "get_server_stats"), (
            "get_server_stats must NOT be registered when "
            "ENABLE_DEBUG_TOOLS is unset — agents would see it"
        )
        assert server._DEBUG_TOOLS_ENABLED is False


class TestLifespanSweeps:
    @pytest.mark.asyncio
    async def test_lifespan_runs_sweeps_stamps_migrations_and_closes_clients(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(cache, "gc_orphan_tmp_files", lambda: (calls.append("gc_tmp"), 0)[1])
        monkeypatch.setattr(cache, "gc_expired_negatives", lambda: (calls.append("gc_neg"), 0)[1])
        monkeypatch.setattr(
            stems, "migrate_legacy_stems", lambda: (calls.append("mig_stems"), 0)[1]
        )
        monkeypatch.setattr(
            manual, "migrate_misrouted_arxiv", lambda: (calls.append("mig_arxiv"), 0)[1]
        )
        monkeypatch.setattr(manual, "migrate_acl_stems", lambda: (calls.append("mig_acl"), 0)[1])

        async def fake_aclose_all() -> None:
            calls.append("aclose_all")

        monkeypatch.setattr(clients, "aclose_all", fake_aclose_all)

        async with app._lifespan(app.mcp):
            calls.append("yielded")

        assert calls == [
            "gc_tmp",
            "gc_neg",
            "mig_stems",
            "mig_arxiv",
            "mig_acl",
            "yielded",
            "aclose_all",
        ]
        assert (tmp_path / app._MIGRATION_STAMP).exists()

        # Second startup on the same CACHE_ROOT runs GC but skips legacy migrations.
        calls.clear()
        async with app._lifespan(app.mcp):
            calls.append("yielded")

        assert calls == ["gc_tmp", "gc_neg", "yielded", "aclose_all"]
