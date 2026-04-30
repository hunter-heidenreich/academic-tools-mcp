"""Counter and DEBUG_REQUESTS tests for ``_stats``.

Wired into cache and the per-provider ``_throttled_get``; these tests
confirm the counters move when the underlying paths fire, and that the
debug-logging gate respects DEBUG_REQUESTS at runtime so an operator
can flip it without restarting the server.
"""

import pytest

from academic_tools_mcp import _stats, cache


def test_cache_hit_and_miss_counters(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    # Miss before put.
    assert cache.get("openalex", "works", "k1") is None
    snap = _stats.snapshot()["providers"]["openalex"]
    assert snap["cache_misses"] == 1
    assert snap.get("cache_hits", 0) == 0

    # Hit after put.
    cache.put("openalex", "works", "k1", {"title": "X"})
    assert cache.get("openalex", "works", "k1") is not None
    snap = _stats.snapshot()["providers"]["openalex"]
    assert snap["cache_hits"] == 1


def test_stale_eviction_counts_as_miss(tmp_path, monkeypatch):
    """TTL-evicted entries should look identical to a never-cached miss
    so the operator can see TTL pressure in the same counter."""
    import os

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
    cache.put("biorxiv", "papers", "k", {"x": 1})
    path = (
        tmp_path / "biorxiv" / "papers" / f"{cache._cache_key('k')}.json"
    )
    old = path.stat().st_mtime - 9999
    os.utime(path, (old, old))

    assert cache.get("biorxiv", "papers", "k", max_age_seconds=60) is None
    assert _stats.snapshot()["providers"]["biorxiv"]["cache_misses"] == 1


def test_negative_hit_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
    cache.put_negative("arxiv", "papers", "bogus", {"error": "404"})
    assert cache.get_negative("arxiv", "papers", "bogus") == {"error": "404"}
    assert _stats.snapshot()["providers"]["arxiv"]["negative_hits"] == 1


def test_snapshot_includes_in_flight(monkeypatch):
    """In-flight pending counts come from each provider module's live
    ``_pending`` global, not from the cumulative counters."""
    from academic_tools_mcp import arxiv

    monkeypatch.setattr(arxiv, "_pending", 3, raising=False)
    snap = _stats.snapshot()
    assert snap["providers"]["arxiv"]["in_flight"] == 3


def test_reset_clears_counters(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
    cache.put("openalex", "works", "k", {"x": 1})
    cache.get("openalex", "works", "k")
    assert _stats.snapshot()["providers"]["openalex"]["cache_hits"] == 1

    _stats.reset()
    # Cache hits cleared; in_flight is still recomputed from module state.
    assert "openalex" not in _stats.snapshot()["providers"] or (
        _stats.snapshot()["providers"]["openalex"].get("cache_hits", 0) == 0
    )


@pytest.mark.parametrize("flag,expected", [
    ("1", True),
    ("true", True),
    ("YES", True),
    ("on", True),
    ("0", False),
    ("", False),
    ("nope", False),
])
def test_debug_requests_flag(monkeypatch, flag, expected):
    monkeypatch.setenv("DEBUG_REQUESTS", flag)
    assert _stats.debug_requests_enabled() is expected


def test_log_request_writes_to_stderr_when_enabled(monkeypatch, capsys):
    monkeypatch.setenv("DEBUG_REQUESTS", "1")
    _stats.log_request("arxiv", "https://example/q", 0.123)
    captured = capsys.readouterr()
    # MCP servers speak JSON-RPC on stdout; logs must go to stderr only.
    assert captured.out == ""
    assert "arxiv" in captured.err
    assert "https://example/q" in captured.err
    assert "0.123" in captured.err


def test_log_request_silent_when_disabled(monkeypatch, capsys):
    monkeypatch.delenv("DEBUG_REQUESTS", raising=False)
    _stats.log_request("arxiv", "https://example/q", 0.123)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
