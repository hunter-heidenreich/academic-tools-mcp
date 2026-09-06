"""Counter, discovery and DEBUG_REQUESTS tests for ``_stats``.

Wired into cache, the PDF write path and the per-provider throttles; these
tests confirm the counters move when the underlying paths fire, that the
snapshot files every row under the provider's own namespace, and that the
debug-logging gate respects DEBUG_REQUESTS at runtime so an operator can flip
it without restarting the server.
"""

import importlib
import pkgutil
import sys
from collections import Counter
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

import academic_tools_mcp
from academic_tools_mcp import _singleflight, _stats, cache


def _all_package_modules():
    """Every module in the package, imported. The suite imports most of them
    anyway; this makes the discovery tests below exhaustive rather than
    dependent on what ran first."""
    modules = []
    for info in pkgutil.walk_packages(
        academic_tools_mcp.__path__, prefix=f"{academic_tools_mcp.__name__}."
    ):
        modules.append(importlib.import_module(info.name))
    return modules


def _module_throttles() -> dict[str, Any]:
    """``{module name: its shared Throttle}`` across the whole package."""
    found = {}
    for module in _all_package_modules():
        throttle = getattr(module, "_throttle", None)
        if hasattr(throttle, "pending") and hasattr(throttle, "namespace"):
            found[module.__name__] = throttle
    return found


class TestCounters:
    def test_a_serve_from_cache_counts_a_hit(self):
        assert cache.get("openalex", "works", "k1") is None
        snap = _stats.snapshot()["providers"].get("openalex", {})
        assert snap.get("cache_misses", 0) == 0, (
            "a bare read that finds nothing went nowhere — it is not a miss"
        )

        cache.put("openalex", "works", "k1", {"title": "X"})
        assert cache.get("openalex", "works", "k1") is not None
        snap = _stats.snapshot()["providers"]["openalex"]
        assert snap["cache_hits"] == 1
        assert snap.get("cache_misses", 0) == 0

    @pytest.mark.asyncio
    async def test_a_miss_is_counted_where_the_fetch_happens(self):
        """``cache_misses`` means "went upstream", so it is booked by the one
        thing that knows: the branch about to call ``fetch``. Counting it in
        ``get`` instead billed a miss for every negative-cache hit and every
        lookup of purely local data."""
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            cache.put("openalex", "works", "k2", {"title": "X"})
            return {"title": "X"}

        async def lookup():
            return await cache.cached_lookup(
                single_flight=_singleflight.SingleFlight(),
                namespace="openalex",
                entity="works",
                canonical="k2",
                positive_ttl=3600.0,
                fetch=fetch,
            )

        await lookup()
        await lookup()

        assert calls == 1
        snap = _stats.snapshot()["providers"]["openalex"]
        assert snap["cache_misses"] == 1, snap
        assert snap["cache_hits"] == 1, snap

    @pytest.mark.asyncio
    async def test_a_negative_hit_is_not_also_a_miss(self):
        """The whole point of the negative cache is that a known-bad identifier
        stops costing upstream calls; billing each one a miss hid that."""
        cache.put_negative("openalex", "works", "gone", {"error": "404"})

        async def fetch():
            raise AssertionError("must be served from the negative cache")

        for _ in range(3):
            await cache.cached_lookup(
                single_flight=_singleflight.SingleFlight(),
                namespace="openalex",
                entity="works",
                canonical="gone",
                positive_ttl=3600.0,
                fetch=fetch,
            )

        snap = _stats.snapshot()["providers"]["openalex"]
        assert snap["negative_hits"] == 3, snap
        assert snap.get("cache_misses", 0) == 0, snap

    @pytest.mark.asyncio
    async def test_stale_eviction_counts_as_miss(self, tmp_path):
        """A TTL-evicted entry sends us back upstream, so it must look identical
        to a never-cached miss — that is how TTL pressure stays visible."""
        import os

        cache.put("biorxiv", "papers", "k", {"x": 1})
        path = tmp_path / "biorxiv" / "papers" / f"{cache._cache_key('k')}.json"
        old = path.stat().st_mtime - 9999
        os.utime(path, (old, old))

        async def fetch():
            return {"x": 2}

        result = await cache.cached_lookup(
            single_flight=_singleflight.SingleFlight(),
            namespace="biorxiv",
            entity="papers",
            canonical="k",
            positive_ttl=60.0,
            fetch=fetch,
        )
        assert result == {"x": 2}
        assert _stats.snapshot()["providers"]["biorxiv"]["cache_misses"] == 1

    def test_negative_hit_counter(self):
        cache.put_negative("arxiv", "papers", "bogus", {"error": "404"})
        assert cache.get_negative("arxiv", "papers", "bogus") == {"error": "404"}
        assert _stats.snapshot()["providers"]["arxiv"]["negative_hits"] == 1

    def test_reset_clears_counters(self):
        cache.put("openalex", "works", "k", {"x": 1})
        cache.get("openalex", "works", "k")
        assert _stats.snapshot()["providers"]["openalex"]["cache_hits"] == 1

        _stats.reset()

        # An `or` over two ways of passing would make this test unfailable:
        # the row may survive (in_flight is recomputed from live module state)
        # but the counter itself must be gone, not merely absent-by-default.
        row = _stats.snapshot()["providers"].get("openalex", {})
        assert "cache_hits" not in row, row

    @given(
        st.lists(
            st.tuples(
                st.sampled_from(["arxiv", "openalex", "probe"]),
                st.sampled_from(["cache_hits", "http_calls"]),
            ),
            max_size=40,
        )
    )
    def test_every_increment_is_reflected_exactly_once(self, events):
        """The counter for each (provider, metric) is the number of ``incr``
        calls for it — no double counting, no cross-key leakage."""
        _stats.reset()
        for provider, metric in events:
            _stats.incr(provider, metric)

        expected = Counter(events)
        providers = _stats.snapshot()["providers"]
        for (provider, metric), count in expected.items():
            assert providers[provider][metric] == count
        for provider, row in providers.items():
            for metric, value in row.items():
                if metric == "in_flight":
                    continue
                assert value == expected[(provider, metric)]


class TestSnapshot:
    def test_includes_in_flight_from_the_live_throttle(self, monkeypatch):
        """In-flight pending counts come from each provider module's shared
        ``_throttle.pending`` count, not from the cumulative counters."""
        from academic_tools_mcp.providers import arxiv

        monkeypatch.setattr(arxiv._throttle, "pending", 3)
        assert _stats.snapshot()["providers"]["arxiv"]["in_flight"] == 3

    def test_in_flight_lands_in_the_row_holding_that_providers_counters(self, monkeypatch):
        """One row per provider. Keying in-flight off anything but the
        throttle's own namespace splits a provider across two rows, and an
        operator reading the hit rate of a module sees half its story."""
        from academic_tools_mcp import oa_download

        _stats.incr(oa_download.NAMESPACE, "cache_hits")
        monkeypatch.setattr(oa_download._throttle, "pending", 2)

        row = _stats.snapshot()["providers"][oa_download.NAMESPACE]
        assert row == {"cache_hits": 1, "in_flight": 2}

    def test_reports_which_env_file_won(self, monkeypatch, tmp_path):
        """``config.ENV_FILE`` is otherwise unobservable.

        An operator debugging "my CACHE_DIR isn't taking effect" needs to know
        which of the candidate files the process actually read — from an
        installed wheel that is rarely the one they just edited. Reported as a
        string: a ``Path`` is not JSON-serialisable, and this rides the MCP
        boundary.
        """
        from academic_tools_mcp import config

        monkeypatch.setattr(config, "ENV_FILE", tmp_path / "chosen.env")
        assert _stats.snapshot()["env_file"] == str(tmp_path / "chosen.env")

    def test_env_file_is_null_when_no_file_was_found(self, monkeypatch):
        from academic_tools_mcp import config

        monkeypatch.setattr(config, "ENV_FILE", None)
        assert _stats.snapshot()["env_file"] is None

    def test_rows_are_copies(self):
        """The snapshot is a report, not a handle: an operator printing it,
        or a caller stripping a key before logging, must not edit the live
        counters."""
        _stats.incr("probe", "http_calls")

        snap = _stats.snapshot()
        snap["providers"]["probe"]["http_calls"] = 999
        snap["providers"]["injected"] = {"http_calls": 1}

        providers = _stats.snapshot()["providers"]
        assert providers["probe"]["http_calls"] == 1
        assert "injected" not in providers

    def test_does_not_import_modules_to_sample_them(self, monkeypatch):
        """``snapshot()`` is an operator's read. Importing a provider in order
        to measure it would make that read side-effectful — and would report
        in-flight rows for providers the process has never used."""
        name = "academic_tools_mcp.providers.wikipedia"
        importlib.import_module(name)
        monkeypatch.delitem(sys.modules, name)

        providers = _stats.snapshot()["providers"]

        assert name not in sys.modules, "snapshot() imported a module to sample it"
        assert "wikipedia" not in providers


class TestThrottleDiscovery:
    """``snapshot()`` and the conftest reset fixture both find throttles by
    scanning imported modules. A provider missed by that scan silently reports
    no in-flight and, worse, leaks pending state between tests.
    """

    def test_every_module_throttle_is_discovered(self):
        expected = _module_throttles()
        assert expected, "no throttled modules found — the scan is broken"

        found = {t.namespace for t in _stats.throttles()}
        for module_name, throttle in expected.items():
            assert throttle.namespace in found, (
                f"{module_name} is not sampled by _stats.throttles()"
            )

    def test_throttle_namespace_matches_the_modules_cache_namespace(self):
        """The invariant the snapshot's keying rests on: a throttle filed under
        a different string than the module's cache writes splits one provider
        into two rows."""
        checked = 0
        for module_name, throttle in _module_throttles().items():
            namespace = getattr(sys.modules[module_name], "NAMESPACE", None)
            if namespace is None:
                continue
            checked += 1
            assert throttle.namespace == namespace, module_name
        assert checked >= 8, f"only {checked} providers checked — discovery regressed"

    def test_a_second_throttle_on_a_module_is_discovered(self, monkeypatch):
        """Discovery matches the type, not the attribute name ``_throttle``.

        A module that paces one endpoint apart from the rest (crossref's search
        gate is the standing candidate) would otherwise hold a throttle that no
        test ever resets and no snapshot ever samples.
        """
        from academic_tools_mcp._throttle import Throttle
        from academic_tools_mcp.providers import wikipedia

        second = Throttle(
            namespace=wikipedia.NAMESPACE,
            label="Wikipedia search",
            max_concurrent=1,
            min_gap_seconds=0.0,
        )
        monkeypatch.setattr(wikipedia, "_search_throttle", second, raising=False)

        assert any(t is second for t in _stats.throttles())

    def test_in_flight_sums_every_throttle_in_the_namespace(self, monkeypatch):
        """Two throttles, one row: assigning instead of summing would report
        whichever the scan reached last and hide the other's traffic."""
        from academic_tools_mcp._throttle import Throttle
        from academic_tools_mcp.providers import wikipedia

        second = Throttle(
            namespace=wikipedia.NAMESPACE,
            label="Wikipedia search",
            max_concurrent=1,
            min_gap_seconds=0.0,
        )
        second.pending = 2
        monkeypatch.setattr(wikipedia, "_search_throttle", second, raising=False)
        monkeypatch.setattr(wikipedia._throttle, "pending", 3)

        assert _stats.snapshot()["providers"][wikipedia.NAMESPACE]["in_flight"] == 5

    def test_yields_each_instance_once(self):
        """Deduped by identity: a throttle re-exported into a second module
        would otherwise be reset twice and counted twice."""
        namespaces = [id(t) for t in _stats.throttles()]
        assert len(namespaces) == len(set(namespaces))

    def test_a_module_level_mock_is_not_mistaken_for_a_throttle(self, monkeypatch):
        """The scan reads arbitrary module attributes, and a MagicMock answers
        ``hasattr`` for anything — so shape-matching would file rows under a
        mock and blow up summing its ``pending``."""
        from unittest.mock import MagicMock

        from academic_tools_mcp.providers import wikipedia

        monkeypatch.setattr(wikipedia, "_probe_client", MagicMock(), raising=False)

        providers = _stats.snapshot()["providers"]

        assert all(isinstance(name, str) for name in providers)
        assert all(isinstance(row.get("in_flight", 0), int) for row in providers.values())


class TestDebugRequests:
    @pytest.mark.parametrize(
        "flag,expected",
        [
            ("1", True),
            ("true", True),
            ("YES", True),
            ("on", True),
            # A trailing space in a .env line is a typo, not a request to
            # silently disable the flag the operator just set.
            (" 1 ", True),
            ("0", False),
            ("", False),
            ("nope", False),
        ],
    )
    def test_flag_parsing(self, monkeypatch, flag, expected):
        monkeypatch.setenv("DEBUG_REQUESTS", flag)
        assert _stats.debug_requests_enabled() is expected

    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv("DEBUG_REQUESTS", raising=False)
        assert _stats.debug_requests_enabled() is False

    def test_log_request_writes_to_stderr_when_enabled(self, monkeypatch, capsys):
        monkeypatch.setenv("DEBUG_REQUESTS", "1")
        _stats.log_request("arxiv", "https://example/q", 0.123)
        captured = capsys.readouterr()
        # MCP servers speak JSON-RPC on stdout; logs must go to stderr only.
        assert captured.out == ""
        assert "arxiv" in captured.err
        assert "https://example/q" in captured.err
        assert "0.123" in captured.err

    def test_log_request_silent_when_disabled(self, monkeypatch, capsys):
        monkeypatch.delenv("DEBUG_REQUESTS", raising=False)
        _stats.log_request("arxiv", "https://example/q", 0.123)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
