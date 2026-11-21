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
        monkeypatch.setattr(cache, "gc_expired_positives", lambda: (calls.append("gc_pos"), 0)[1])
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
            "gc_pos",
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

        assert calls == ["gc_tmp", "gc_neg", "gc_pos", "yielded", "aclose_all"]


class TestToolMetadataAndInstructions:
    @pytest.mark.asyncio
    async def test_every_tool_declares_complete_annotations(self) -> None:
        tools = await server.mcp.list_tools()
        assert len(tools) >= 34

        write_tools = {"download_pdf", "convert_paper", "import_paper"}
        local_tools = {
            "get_paper_sections",
            "get_paper_section",
            "get_paper_markdown",
            "find_in_paper",
            "search_cached_papers",
            "import_paper",
            "list_cached_papers",
            "get_server_stats",
        }

        for tool in tools:
            ann = tool.annotations
            assert ann is not None, f"{tool.name} is missing ToolAnnotations"
            assert ann.destructiveHint is False, f"{tool.name} should not be destructive"
            assert ann.idempotentHint is True, f"{tool.name} should be idempotent"
            expected_read_only = tool.name not in write_tools
            assert ann.readOnlyHint is expected_read_only, (
                f"{tool.name}: expected readOnlyHint={expected_read_only}, got {ann.readOnlyHint}"
            )
            expected_open_world = tool.name not in local_tools
            assert ann.openWorldHint is expected_open_world, (
                f"{tool.name}: expected openWorldHint={expected_open_world}, got {ann.openWorldHint}"
            )

    def test_instructions_do_not_claim_citations_takes_source_auto(self) -> None:
        instructions = server.mcp.instructions or ""
        assert "get_paper_references and get_paper_citations default to source='auto'" not in (
            instructions
        )
        assert "get_paper_citations defaults to source='opencitations'" in instructions

    @pytest.mark.asyncio
    async def test_get_paper_versions_schema_uses_preprint_identifier(self) -> None:
        tools = {t.name: t for t in await server.mcp.list_tools()}
        desc = tools["get_paper_versions"].parameters["properties"]["identifier"]["description"]
        assert "Preprint identifier" in desc
        assert "refused locally" in desc


class TestDisabledToolGroups:
    @pytest.mark.asyncio
    async def test_tool_groups_cover_every_registered_tool(self) -> None:
        registered = {t.name for t in await server.mcp.list_tools()}
        grouped = {name for names in server.TOOL_GROUPS.values() for name in names}
        assert registered == grouped

    def test_resolve_disabled_tools_parses_groups_case_insensitively(self) -> None:
        disabled = server.resolve_disabled_tools(" Wikipedia , paperswithcode , unknown_group ")
        assert disabled == {
            *server.TOOL_GROUPS["wikipedia"],
            *server.TOOL_GROUPS["paperswithcode"],
        }
        assert server.resolve_disabled_tools(None) == set()
        assert server.resolve_disabled_tools("") == set()
