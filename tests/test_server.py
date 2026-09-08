"""Tests for ``server.py`` itself.

Nearly everything reachable as ``server.<tool>`` is defined in
``tools/`` and tested there. What is left is the debug-tool gate.
"""

from academic_tools_mcp import server

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
