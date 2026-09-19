"""Tests for `download/protocol.py` — the cached-download protocol."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from academic_tools_mcp.download import artifact, protocol


class TestIsDefinitiveFailure:
    """An allowlist, not a denylist — and the difference is a live bug class.

    ``http.error_dict`` marks every *transient* branch ``retryable: True``,
    but its "other 4xx" branch carries no ``retryable`` key at all: a 403 from
    a paywall is not something we know is permanent and paper-intrinsic. Under
    a denylist ("anything not marked retryable") that unknown is classified as
    definitive and negative-cached for the full TTL.
    """

    def test_an_explicit_non_retryable_error_counts(self):
        assert protocol.is_definitive_failure({"error": "gone", "retryable": False})

    def test_an_explicit_retryable_error_does_not(self):
        assert not protocol.is_definitive_failure({"error": "blip", "retryable": True})

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ReadTimeout("boom"),
            httpx.ConnectError("boom"),
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(503, request=httpx.Request("GET", "http://x")),
            ),
            httpx.HTTPStatusError(
                "x",
                request=httpx.Request("GET", "http://x"),
                response=httpx.Response(429, request=httpx.Request("GET", "http://x")),
            ),
        ],
    )
    def test_transient_errors_are_flagged_retryable_and_must_not_count(self, exc):
        from academic_tools_mcp.net import http

        result = http.error_dict("Test", exc)
        assert result["retryable"] is True, (
            "every transient branch of error_dict must carry the flag; "
            "openaccess and tools/graph branch on it"
        )
        assert not protocol.is_definitive_failure(result)

    def test_an_unclassified_4xx_must_not_count(self):
        """The case that makes the allowlist load-bearing.

        A 403 gets no ``retryable`` key either way — we don't know whether the
        paywall is permanent. A denylist would negative-cache it for the TTL.
        """
        from academic_tools_mcp.net import http

        request = httpx.Request("GET", "http://x")
        result = http.error_dict(
            "Test",
            httpx.HTTPStatusError(
                "x", request=request, response=httpx.Response(403, request=request)
            ),
        )
        assert "retryable" not in result
        assert not protocol.is_definitive_failure(result)

    def test_a_size_cap_abort_does_not_count(self):
        # Non-retryable, but a config choice a cap bump fixes — not a fact
        # about the paper. Caching it would strand the caller behind a stale
        # miss until the TTL expired.
        assert not protocol.is_definitive_failure(
            {"error": "too big", "retryable": False, "max_bytes": 100}
        )

    def test_a_success_does_not_count(self):
        assert not protocol.is_definitive_failure({"path": "/x", "cached": False})

    def test_a_404_from_stream_to_file_counts(self):
        # The shape stream_to_file actually emits, so the classifier and the
        # producer cannot drift apart on this one.
        assert protocol.is_definitive_failure(
            {"error": "arXiv: PDF not found at http://x", "retryable": False}
        )


def _pdf(dest: Path, body: bytes = b"%PDF-1.4 body") -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return dest


class TestCachedDownload:
    """The file-artifact sibling of ``cache.cached_lookup``."""

    @staticmethod
    def _call(dest, fetch, **overrides):
        from academic_tools_mcp.store import singleflight

        kwargs = {
            "single_flight": singleflight.SingleFlight(),
            "namespace": "testns",
            "entity": "downloads",
            "canonical": "10.1234/x",
            "dest": dest,
            "fetch": fetch,
            "neg_ttl": 3600.0,
        }
        kwargs.update(overrides)
        return protocol.cached_download(**kwargs)

    @pytest.mark.asyncio
    async def test_a_usable_cached_pdf_short_circuits(self, tmp_path):
        dest = _pdf(tmp_path / "p.pdf")
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            return {"path": str(dest), "cached": False}

        result = await self._call(dest, fetch)

        assert result["cached"] is True
        assert calls == 0

    @pytest.mark.asyncio
    async def test_a_zero_byte_file_is_a_miss(self, tmp_path):
        dest = _pdf(tmp_path / "p.pdf", b"")

        async def fetch():
            return {"path": str(dest), "size_bytes": 3, "cached": False}

        assert (await self._call(dest, fetch))["cached"] is False

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_fetch(self, tmp_path):

        dest = tmp_path / "p.pdf"
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return {"path": str(dest), "cached": False}

        from academic_tools_mcp.store import singleflight

        sf = singleflight.SingleFlight()
        await asyncio.gather(*(self._call(dest, fetch, single_flight=sf) for _ in range(4)))

        assert calls == 1

    @pytest.mark.asyncio
    async def test_the_in_slot_recheck_serves_a_file_written_after_the_outer_check(
        self, tmp_path, monkeypatch
    ):
        """The protocol's core concurrency guarantee.

        A caller misses the outer check, then waits on the single-flight slot
        while a leader for the same key lands the PDF. Re-checking *inside*
        the slot is what makes it pick up those bytes instead of re-streaming
        a file that is already on disk. Simulated by failing only the outer
        ``cached_hit`` — the same window the leader writes into.
        """
        dest = tmp_path / "p.pdf"
        real_cached_hit = artifact.cached_hit
        checks = {"n": 0}

        def leader_writes_between_the_checks(path):
            checks["n"] += 1
            if checks["n"] == 1:
                return None  # outer check: the leader has not written yet
            _pdf(dest)  # ...and lands the file before we re-check in the slot
            return real_cached_hit(path)

        monkeypatch.setattr(artifact, "cached_hit", leader_writes_between_the_checks)

        async def fetch():
            raise AssertionError("the in-slot re-check must short-circuit before fetch")

        result = await self._call(dest, fetch)

        assert result["cached"] is True
        assert checks["n"] == 2, "both the outer check and the in-slot re-check must run"

    @pytest.mark.asyncio
    async def test_the_in_slot_recheck_serves_a_negative_entry_written_after_the_outer_check(
        self, tmp_path, monkeypatch
    ):
        """Same window, negative half: a leader that recorded a definitive
        failure while this caller waited must not be re-fetched."""
        from academic_tools_mcp.store import cache

        dest = tmp_path / "p.pdf"  # never created — cached_hit misses naturally
        checks = {"n": 0}

        def leader_records_between_the_checks(*args, **kwargs):
            checks["n"] += 1
            if checks["n"] == 1:
                return None
            return {"error": "gone", "retryable": False}

        monkeypatch.setattr(cache, "get_negative", leader_records_between_the_checks)

        async def fetch():
            raise AssertionError("the in-slot re-check must short-circuit before fetch")

        result = await self._call(dest, fetch)

        assert result == {"error": "gone", "retryable": False}
        assert checks["n"] == 2

    @pytest.mark.asyncio
    async def test_force_refresh_skips_the_in_slot_recheck(self, tmp_path, monkeypatch):
        """The deliberate divergence from ``cache.cached_lookup``.

        Re-checking under force_refresh would make a refresh a no-op whenever
        a usable PDF is already on disk — exactly the case force_refresh
        exists to fix (a corrupt or superseded cached file). So the forced
        path must reach ``fetch`` even with a good file and a negative entry
        both present.
        """
        from academic_tools_mcp.store import cache

        dest = _pdf(tmp_path / "p.pdf", b"%PDF-1.4 stale")
        cache.put_negative("testns", "downloads", "10.1234/x", {"error": "gone"})

        def must_not_be_consulted(path):
            raise AssertionError("force_refresh must not check the cached artifact")

        monkeypatch.setattr(artifact, "cached_hit", must_not_be_consulted)

        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            dest.write_bytes(b"%PDF-1.4 fresh")
            return {"path": str(dest), "size_bytes": 14, "cached": False}

        result = await self._call(dest, fetch, force_refresh=True)

        assert calls == 1
        assert result["cached"] is False
        assert dest.read_bytes() == b"%PDF-1.4 fresh"

    @pytest.mark.asyncio
    async def test_a_definitive_failure_is_negative_cached(self, tmp_path):
        dest = tmp_path / "p.pdf"
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            return {"error": "gone", "retryable": False}

        assert "error" in await self._call(dest, fetch)
        assert "error" in await self._call(dest, fetch)
        assert calls == 1, "the second call should be served from the negative cache"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            {"error": "blip", "retryable": True},
            {"error": "timed out"},  # no retryable key at all — the live bug
            {"error": "too big", "retryable": False, "max_bytes": 10},
        ],
    )
    async def test_a_non_definitive_failure_is_not_cached(self, tmp_path, failure):
        dest = tmp_path / "p.pdf"
        calls = 0

        async def fetch():
            nonlocal calls
            calls += 1
            return dict(failure)

        await self._call(dest, fetch)
        await self._call(dest, fetch)
        assert calls == 2

    @pytest.mark.asyncio
    async def test_a_usable_pdf_wins_over_a_negative_entry(self, tmp_path):
        """Ordering is load-bearing: a force_refresh that 404s writes a
        negative entry while a perfectly good PDF is still on disk (
        ``stream_to_file`` only replaces dest on success). The next plain call
        must serve the file, not the stale error."""
        from academic_tools_mcp.store import cache

        dest = _pdf(tmp_path / "p.pdf")
        cache.put_negative("testns", "downloads", "10.1234/x", {"error": "gone"})

        async def fetch():
            raise AssertionError("must not fetch")

        assert (await self._call(dest, fetch))["cached"] is True

    @pytest.mark.asyncio
    async def test_force_refresh_clears_the_negative_entry_and_refetches(self, tmp_path):
        from academic_tools_mcp.store import cache

        dest = tmp_path / "p.pdf"
        cache.put_negative("testns", "downloads", "10.1234/x", {"error": "gone"})

        async def fetch():
            return {"path": str(dest), "cached": False}

        assert (await self._call(dest, fetch, force_refresh=True))["cached"] is False
        assert cache.get_negative("testns", "downloads", "10.1234/x") is None

    @pytest.mark.asyncio
    async def test_extra_fields_decorate_both_success_branches(self, tmp_path):
        # ACL's provenance must be identical on the fresh and cached paths.
        # They used to be two hand-copied blocks that could drift.
        dest = tmp_path / "p.pdf"
        extra = {"anthology_id": "P16-1160"}

        async def fetch():
            _pdf(dest)
            return {"path": str(dest), "size_bytes": 13, "cached": False}

        fresh = await self._call(dest, fetch, extra_fields=extra)

        async def no_fetch():
            raise AssertionError("must not fetch")

        cached = await self._call(dest, no_fetch, extra_fields=extra)

        assert fresh["anthology_id"] == cached["anthology_id"] == "P16-1160"
        assert fresh["cached"] is False
        assert cached["cached"] is True

    @pytest.mark.asyncio
    async def test_extra_fields_do_not_decorate_errors(self, tmp_path):
        dest = tmp_path / "p.pdf"

        async def fetch():
            return {"error": "gone", "retryable": False}

        result = await self._call(dest, fetch, extra_fields={"anthology_id": "P16-1160"})

        assert "anthology_id" not in result

    @pytest.mark.asyncio
    async def test_callers_receive_independent_objects(self, tmp_path):
        # Single-flight followers share the leader's object, and
        # tools/pipeline writes `cascaded_invalidated` into what it gets back.

        from academic_tools_mcp.store import singleflight

        dest = tmp_path / "p.pdf"

        async def fetch():
            await asyncio.sleep(0.01)
            return {"path": str(dest), "cached": False}

        sf = singleflight.SingleFlight()
        a, b = await asyncio.gather(
            self._call(dest, fetch, single_flight=sf),
            self._call(dest, fetch, single_flight=sf),
        )

        assert a is not b
        a["cascaded_invalidated"] = ["markdown"]
        assert "cascaded_invalidated" not in b
