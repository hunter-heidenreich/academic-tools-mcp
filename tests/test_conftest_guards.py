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

import academic_tools_mcp
from academic_tools_mcp import papers
from academic_tools_mcp.store import cache
from academic_tools_mcp.util import config
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


def _is_config_call(node: ast.AST) -> bool:
    """True for the ``config.<fn>(...)`` spelling every module uses."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"get", "flag", "number"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "config"
        and bool(node.args)
    )


def _src_trees() -> list[ast.Module]:
    """Every module in the package, resolved by package name.

    Not ``Path(config.__file__).parent`` — that is ``util/``, five files, and
    the scan below then reports nothing while every assertion on it still
    passes. Not ``parents[n]`` either (`.claude/rules/python-design.md`): the
    package root is the directory ``academic_tools_mcp`` names, at any depth.
    """
    src = Path(academic_tools_mcp.__file__).parent
    return [ast.parse(p.read_text(encoding="utf-8")) for p in sorted(src.rglob("*.py"))]


def _forwarders(trees: list[ast.Module]) -> tuple[dict[str, int], set[int]]:
    """Functions that hand one of their own parameters to ``config.<fn>``.

    ``papers.convert._resolve_timeout(env_var, default)`` is the live instance,
    and it is why a scan of ``config.<fn>("KEY")`` alone is not enough: the key
    is a literal at the *call* sites, never at the ``config.number`` call. Two
    real settings (``PDF_CONVERT_TIMEOUT``, ``PDF_FAST_CONVERT_TIMEOUT``) sit
    behind it, and a third added the same way would be invisible.

    Returns ``{function name: index of the forwarded parameter}`` plus the
    ``id()`` of each in-forwarder ``config`` call, so the dynamic-key backstop
    below knows those are accounted for rather than unresolvable.
    """
    forwarders: dict[str, int] = {}
    resolved: set[int] = set()
    for tree in trees:
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
            for node in ast.walk(fn):
                if (
                    _is_config_call(node)
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in params
                ):
                    forwarders[fn.name] = params.index(node.args[0].id)
                    resolved.add(id(node))
    return forwarders, resolved


def _settings_read_in_src() -> set[str]:
    """Every key reaching ``config.get`` / ``flag`` / ``number``, direct or via
    a one-level wrapper.

    A scan rather than a second hand-maintained list: a roster that has to be
    updated by hand is a roster that goes stale the first time a provider adds
    a setting, and the failure is silent — that setting simply keeps leaking
    in from the developer's own ``.env``.

    Matches on the ``config.<fn>("KEY")`` spelling every module uses, plus a
    literal passed to a ``_forwarders()`` function. A module that instead did
    ``from .config import get`` would still slip past, which is one more reason
    the package-qualified form is the convention.
    """
    trees = _src_trees()
    forwarders, _ = _forwarders(trees)
    found: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if _is_config_call(node):
                index = 0
            else:
                name = node.func.id if isinstance(node.func, ast.Name) else None
                if name is None and isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name not in forwarders:
                    continue
                index = forwarders[name]
            if index < len(node.args):
                arg = node.args[index]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.add(arg.value)
    return found


def _unresolvable_config_keys() -> list[str]:
    """``config.<fn>`` calls whose key this scan cannot resolve to a literal.

    The backstop that keeps the blind spot loud. A new indirection the
    ``_forwarders`` pass doesn't model would otherwise make settings invisible
    again *and* leave this file passing — the exact silent-guard failure the
    module docstring is about.
    """
    trees = _src_trees()
    _, resolved = _forwarders(trees)
    return [
        ast.unparse(node)
        for tree in trees
        for node in ast.walk(tree)
        if _is_config_call(node)
        and not isinstance(node.args[0], ast.Constant)
        and id(node) not in resolved
    ]


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

    def test_the_scan_actually_reaches_the_whole_package(self):
        """The roster check is a subset assertion, so a scan that reports
        nothing passes it for every setting at once. Pin keys from four
        different subpackages rather than a count: breadth is what a wrong
        scan root destroys, and a count would go stale on the next setting."""
        found = _settings_read_in_src()
        assert {
            "CACHE_DIR",  # store/cache.py
            "CROSSREF_MAILTO",  # providers/crossref.py
            "MAX_PDF_BYTES",  # download/streaming.py
            "PDF_CONVERTER",  # papers/convert.py
        } <= found

    def test_the_scan_sees_keys_passed_through_a_wrapper(self):
        """The scan must follow `_resolve_timeout("KEY", ...)`, not just
        `config.number("KEY", ...)` — the two PDF timeouts are only reachable
        that way, and were in the roster by hand rather than by this check."""
        found = _settings_read_in_src()
        assert {"PDF_CONVERT_TIMEOUT", "PDF_FAST_CONVERT_TIMEOUT"} <= found

    def test_no_config_key_is_beyond_the_scan(self):
        """A key the scan cannot resolve makes the roster check vacuous for
        that setting, so it fails here instead of passing quietly."""
        unresolvable = _unresolvable_config_keys()
        assert not unresolvable, f"config keys this scan cannot see: {unresolvable}"


class TestConversionStateReset:
    def test_convert_lock_starts_unlocked(self):
        assert not papers.convert._global_convert_lock.locked()

    def test_current_conversion_starts_empty(self):
        assert papers.convert._current_conversion is None

    def test_section_locks_start_empty(self):
        assert len(papers.index._section_locks) == 0
