"""Tests for the autouse safety nets in ``conftest.py``.

A guard fixture that silently stops working is worse than no guard at all —
the suite would keep passing while writing into the operator's real cache or
reaching a live API. These assert the guards are actually armed.
"""

import ast
import os
import socket
from pathlib import Path

import pytest

from academic_tools_mcp import cache, config, papers
from tests.conftest import _CONFIG_ENV_VARS, _EMPTY_ENV_FILE


class TestCacheRootIsolation:
    def test_cache_root_points_at_this_tests_tmp_path(self, tmp_path):
        assert tmp_path == cache.CACHE_ROOT

    def test_cache_writes_land_under_tmp_path(self, tmp_path):
        cache.put("arxiv", "papers", "2301.00001", {"title": "x"})
        written = list(tmp_path.rglob("*.json"))
        assert written, "cache.put wrote nothing under the isolated root"
        assert all(str(p).startswith(str(tmp_path)) for p in written)

    def test_a_test_can_still_override_the_root(self, tmp_path, monkeypatch):
        other = tmp_path / "elsewhere"
        monkeypatch.setattr(cache, "CACHE_ROOT", other)
        assert other == cache.CACHE_ROOT


class TestNetworkBlocked:
    def test_create_connection_to_remote_is_blocked(self):
        with pytest.raises(RuntimeError, match="Blocked real network"):
            socket.create_connection(("api.crossref.org", 443), timeout=2)

    def test_socket_connect_to_remote_is_blocked(self):
        with pytest.raises(RuntimeError, match="Blocked real network"):
            socket.socket().connect(("api.crossref.org", 443))

    def test_loopback_is_still_allowed(self):
        # Not a real listener, so this must fail as a *connection* error —
        # proving the guard let it through rather than raising RuntimeError.
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", 1), timeout=1)


def _settings_read_in_src() -> set[str]:
    """Every literal key passed to ``config.get`` / ``flag`` / ``number``.

    A scan rather than a second hand-maintained list: a roster that has to be
    updated by hand is a roster that goes stale the first time a provider adds
    a setting, and the failure is silent — that setting simply keeps leaking
    in from the developer's own ``.env``.

    Matches on the ``config.<fn>("KEY")`` spelling every module uses. A module
    that instead did ``from .config import get`` would slip past, which is one
    more reason the package-qualified form is the convention.
    """
    src = Path(config.__file__).parent
    found: set[str] = set()
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "flag", "number"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "config"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.add(node.args[0].value)
    return found


class TestConfigEnvScrubbed:
    def test_no_config_setting_leaks_in_from_the_environment(self):
        leaked = {name: os.environ[name] for name in _CONFIG_ENV_VARS if name in os.environ}
        # The .env pointer is the one entry that must be *present*.
        assert leaked == {"ACADEMIC_TOOLS_ENV_FILE": _EMPTY_ENV_FILE}

    def test_the_accessors_see_nothing(self):
        visible = [
            name
            for name in _CONFIG_ENV_VARS
            if name != "ACADEMIC_TOOLS_ENV_FILE" and config.get(name) is not None
        ]
        assert not visible, f"config.get still resolves {visible}"
        assert not config.flag("ENABLE_DEBUG_TOOLS")
        assert not config.flag("DEBUG_REQUESTS")

    def test_the_env_file_that_won_is_the_empty_fixture(self):
        assert Path(_EMPTY_ENV_FILE) == config.ENV_FILE

    def test_roster_covers_every_setting_src_reads(self):
        # `config.get` also reads ACADEMIC_TOOLS_ENV_FILE and XDG_CONFIG_HOME
        # from inside config.py itself, so the scan is a subset check, not an
        # equality one — the roster may be wider, never narrower.
        missing = _settings_read_in_src() - set(_CONFIG_ENV_VARS)
        assert not missing, f"conftest._CONFIG_ENV_VARS is missing {sorted(missing)}"


class TestConversionStateReset:
    def test_convert_lock_starts_unlocked(self):
        assert not papers._global_convert_lock.locked()

    def test_current_conversion_starts_empty(self):
        assert papers._current_conversion is None

    def test_section_locks_start_empty(self):
        assert len(papers._section_locks) == 0
