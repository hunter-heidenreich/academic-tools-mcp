"""Tests for the autouse safety nets in ``conftest.py``.

A guard fixture that silently stops working is worse than no guard at all —
the suite would keep passing while writing into the operator's real cache or
reaching a live API. These assert the guards are actually armed.
"""

import socket

import pytest

from academic_tools_mcp import cache, papers


class TestCacheRootIsolation:
    def test_cache_root_points_at_this_tests_tmp_path(self, tmp_path):
        assert tmp_path == cache._CACHE_ROOT

    def test_cache_writes_land_under_tmp_path(self, tmp_path):
        cache.put("arxiv", "papers", "2301.00001", {"title": "x"})
        written = list(tmp_path.rglob("*.json"))
        assert written, "cache.put wrote nothing under the isolated root"
        assert all(str(p).startswith(str(tmp_path)) for p in written)

    def test_a_test_can_still_override_the_root(self, tmp_path, monkeypatch):
        other = tmp_path / "elsewhere"
        monkeypatch.setattr(cache, "_CACHE_ROOT", other)
        assert other == cache._CACHE_ROOT


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


class TestConversionStateReset:
    def test_convert_lock_starts_unlocked(self):
        assert not papers._global_convert_lock.locked()

    def test_current_conversion_starts_empty(self):
        assert papers._current_conversion is None

    def test_section_locks_start_empty(self):
        assert len(papers._section_locks) == 0
