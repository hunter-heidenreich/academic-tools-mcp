import contextlib
import json

import pytest

from academic_tools_mcp import cache


def test_cache_dir_is_public_and_namespaced(tmp_path, monkeypatch):
    # Public name (no leading underscore) so the PDF-handling modules can
    # build canonical paths without reaching for a private helper.
    assert cache.cache_dir("openalex", "works") == tmp_path / "openalex" / "works"


def test_put_and_get(tmp_path, monkeypatch):
    data = {"id": "W123", "title": "Test Paper"}
    cache.put("openalex", "works", "10.1234/test", data)

    result = cache.get("openalex", "works", "10.1234/test")
    assert result == data


def test_get_miss(tmp_path, monkeypatch):
    assert cache.get("openalex", "works", "nonexistent") is None


def test_namespacing(tmp_path, monkeypatch):
    cache.put("openalex", "works", "key1", {"source": "openalex"})
    cache.put("arxiv", "papers", "key1", {"source": "arxiv"})

    assert cache.get("openalex", "works", "key1")["source"] == "openalex"
    assert cache.get("arxiv", "papers", "key1")["source"] == "arxiv"


def test_unicode_data(tmp_path, monkeypatch):
    data = {"author": "Müller, François-René"}
    cache.put("openalex", "works", "unicode-test", data)

    result = cache.get("openalex", "works", "unicode-test")
    assert result["author"] == "Müller, François-René"


def test_cache_file_is_valid_json(tmp_path, monkeypatch):
    data = {"title": "Test", "year": 2022}
    cache.put("openalex", "works", "json-test", data)

    # Find the file and verify it's readable JSON. After atomic-write,
    # only the canonical .json should remain — no leftover .tmp files.
    files = list((tmp_path / "openalex" / "works").iterdir())
    json_files = [f for f in files if f.suffix == ".json"]
    tmp_files = [f for f in files if f.suffix == ".tmp"]
    assert len(json_files) == 1
    assert tmp_files == []
    parsed = json.loads(json_files[0].read_text())
    assert parsed == data


# ---------------------------------------------------------------------------
# Atomic writes & corruption recovery
# ---------------------------------------------------------------------------


def test_corrupt_cache_file_self_heals_on_get(tmp_path, monkeypatch):
    """A truncated/garbage JSON file (e.g. left behind by a process that
    died mid-write before atomic writes existed) must not poison the cache.
    get() returns None, the bad file is removed, and the next put() can
    write a clean entry."""
    # Manually plant a corrupt file at the exact path get() will look up.
    directory = tmp_path / "openalex" / "works"
    directory.mkdir(parents=True)
    bad_path = directory / f"{cache._cache_key('corrupt-1')}.json"
    bad_path.write_text('{"title": "Te')  # truncated mid-string

    assert cache.get("openalex", "works", "corrupt-1") is None
    assert not bad_path.exists(), "corrupt file should be unlinked on read"

    # And we can write a fresh value with no special handling.
    cache.put("openalex", "works", "corrupt-1", {"title": "Test"})
    assert cache.get("openalex", "works", "corrupt-1") == {"title": "Test"}


def test_failed_write_does_not_clobber_existing_value(tmp_path, monkeypatch):
    """If put() fails partway through (e.g. the JSON encoder raises on
    non-serialisable input), the previously cached value at the canonical
    path must remain intact — the temp file gets cleaned up, the rename
    never happens, and the existing entry is unaffected."""
    cache.put("openalex", "works", "k", {"title": "good"})
    assert cache.get("openalex", "works", "k") == {"title": "good"}

    class Unserializable:
        pass

    try:
        cache.put("openalex", "works", "k", {"obj": Unserializable()})
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError on unserialisable payload")

    # Original survives, no leftover .tmp files.
    assert cache.get("openalex", "works", "k") == {"title": "good"}
    leftover_tmps = list((tmp_path / "openalex" / "works").glob("*.tmp"))
    assert leftover_tmps == [], leftover_tmps


# ---------------------------------------------------------------------------
# Negative cache (TTL-bounded)
# ---------------------------------------------------------------------------


def test_get_negative_returns_none_when_absent(tmp_path, monkeypatch):
    assert cache.get_negative("openalex", "works", "missing") is None


def test_put_then_get_negative_returns_payload_without_internals(tmp_path, monkeypatch):
    # The agent should see the same {error: ...} shape it would have
    # gotten from a fresh 404 — _expires_at is bookkeeping and must not
    # leak through.
    err = {"error": "No paper found for arXiv ID: bogus"}
    cache.put_negative("arxiv", "papers", "bogus", err)

    cached = cache.get_negative("arxiv", "papers", "bogus")
    assert cached == err
    assert "_expires_at" not in cached


def test_negative_does_not_collide_with_positive(tmp_path, monkeypatch):
    # Sibling _neg/ subdirectory means the same key can hold a positive
    # and a negative entry without one masking the other. Important so
    # that if a previously-not-found DOI later resolves, we can write a
    # positive entry and have cache.get find it even before the negative
    # expires.
    cache.put("openalex", "works", "10.1/x", {"title": "Real"})
    cache.put_negative("openalex", "works", "10.1/x", {"error": "stale"})

    assert cache.get("openalex", "works", "10.1/x") == {"title": "Real"}
    assert cache.get_negative("openalex", "works", "10.1/x") == {"error": "stale"}


def test_expired_negative_entry_self_heals(tmp_path, monkeypatch):
    # Past-its-TTL negative entries must be treated as a cache miss
    # AND removed on read so they don't accumulate forever.
    cache.put_negative(
        "openalex",
        "works",
        "expired-1",
        {"error": "stale"},
        ttl_seconds=-1.0,  # already expired the moment it was written
    )
    assert cache.get_negative("openalex", "works", "expired-1") is None

    # The file is gone, so the next put writes cleanly.
    neg_path = cache._neg_path("openalex", "works", "expired-1")
    assert not neg_path.exists()


def test_corrupt_negative_entry_self_heals(tmp_path, monkeypatch):
    # A truncated or otherwise unparseable negative entry must not poison
    # subsequent reads. Same self-heal contract as the positive cache.
    neg_path = cache._neg_path("arxiv", "papers", "junk-1")
    neg_path.parent.mkdir(parents=True, exist_ok=True)
    neg_path.write_text('{"error": "tru')  # truncated mid-string

    assert cache.get_negative("arxiv", "papers", "junk-1") is None
    assert not neg_path.exists()


def test_negative_entry_missing_expires_at_self_heals(tmp_path, monkeypatch):
    # A negative file that's syntactically valid JSON but missing the
    # _expires_at sentinel must not be trusted forever — treat it as
    # expired so the next put rebuilds it cleanly.
    neg_path = cache._neg_path("arxiv", "papers", "no-ttl")
    neg_path.parent.mkdir(parents=True, exist_ok=True)
    neg_path.write_text(json.dumps({"error": "x"}))

    assert cache.get_negative("arxiv", "papers", "no-ttl") is None
    assert not neg_path.exists()


def test_max_age_seconds_evicts_stale_entry(tmp_path, monkeypatch):
    """A positive entry older than max_age_seconds is treated as a miss
    and unlinked, so a stale citation count or published_doi can't pin
    the cache for an entire session."""
    import os

    cache.put("openalex", "works", "10.1/x", {"title": "Stale"})
    path = tmp_path / "openalex" / "works" / f"{cache._cache_key('10.1/x')}.json"
    assert path.exists()

    # Backdate the file by an hour so the TTL test fires.
    old = path.stat().st_mtime - 3600
    os.utime(path, (old, old))

    # Tight TTL → treated as expired → unlinked.
    assert cache.get("openalex", "works", "10.1/x", max_age_seconds=60) is None
    assert not path.exists(), "stale entry should self-heal on read"


def test_max_age_seconds_keeps_fresh_entry(tmp_path, monkeypatch):
    cache.put("openalex", "works", "fresh", {"title": "Fresh"})
    # Generous TTL → entry survives.
    assert cache.get("openalex", "works", "fresh", max_age_seconds=3600) == {"title": "Fresh"}


# ---------------------------------------------------------------------------
# TTL / sweep boundaries — exactly at the limit must fall on the safe side
# ---------------------------------------------------------------------------


def test_entry_at_exactly_max_age_still_hits(tmp_path, monkeypatch):
    """`age > max_age_seconds` — an entry aged exactly the TTL is still fresh.
    The clock is frozen so the assertion is about the comparison, not about how
    long the test itself took."""
    import os

    cache.put("openalex", "works", "edge", {"title": "Edge"})
    path = tmp_path / "openalex" / "works" / f"{cache._cache_key('edge')}.json"
    now = 1_700_000_000.0
    os.utime(path, (now - 60.0, now - 60.0))
    monkeypatch.setattr(cache.time, "time", lambda: now)

    assert cache.get("openalex", "works", "edge", max_age_seconds=60.0) == {"title": "Edge"}
    assert path.exists(), "an entry exactly at the TTL must not be swept"

    # One second past it, the same entry is a miss and self-heals.
    assert cache.get("openalex", "works", "edge", max_age_seconds=59.0) is None
    assert not path.exists()


def test_negative_entry_at_exactly_its_expiry_is_still_live(tmp_path, monkeypatch):
    """`expires_at < time.time()` — an entry at its exact expiry instant is
    still served, matching get()'s boundary on the positive side."""
    now = 1_700_000_000.0
    monkeypatch.setattr(cache.time, "time", lambda: now)
    cache.put_negative("arxiv", "papers", "edge-neg", {"error": "404"}, ttl_seconds=0.0)

    assert cache.get_negative("arxiv", "papers", "edge-neg") == {"error": "404"}

    # A hair past it, the same entry is a miss and is unlinked.
    monkeypatch.setattr(cache.time, "time", lambda: now + 0.001)
    assert cache.get_negative("arxiv", "papers", "edge-neg") is None
    assert not cache._neg_path("arxiv", "papers", "edge-neg").exists()


def test_gc_sweeps_a_tmp_file_at_exactly_the_cutoff(tmp_path, monkeypatch):
    """`st_mtime > cutoff` — a temp exactly at the cutoff age is swept, and one
    a hair younger is not. Erring towards sweeping is safe only because no
    writer backdates its temp file."""
    import os

    stale = tmp_path / "ns" / "ent" / "a.tmp"
    stale.parent.mkdir(parents=True)
    stale.write_text("x")
    fresh = tmp_path / "ns" / "ent" / "b.tmp"
    fresh.write_text("x")

    now = 1_700_000_000.0
    os.utime(stale, (now - 3600.0, now - 3600.0))
    os.utime(fresh, (now - 3599.0, now - 3599.0))
    monkeypatch.setattr(cache.time, "time", lambda: now)

    assert cache.gc_orphan_tmp_files(max_age_seconds=3600.0) == 1
    assert not stale.exists()
    assert fresh.exists()


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------


def test_invalidate_drops_positive_and_negative(tmp_path, monkeypatch):
    """force_refresh drops both halves so a previously-404'd identifier
    can resolve on the retry — a stale negative wouldn't expire for 24h
    on its own."""
    cache.put("openalex", "works", "10.1/x", {"title": "Real"})
    cache.put_negative("openalex", "works", "10.1/x", {"error": "stale"})
    assert cache.get("openalex", "works", "10.1/x") is not None
    assert cache.get_negative("openalex", "works", "10.1/x") is not None

    cache.invalidate("openalex", "works", "10.1/x")

    assert cache.get("openalex", "works", "10.1/x") is None
    assert cache.get_negative("openalex", "works", "10.1/x") is None


def test_invalidate_is_idempotent(tmp_path, monkeypatch):
    """Calling invalidate on a key that has nothing cached is a silent
    no-op so callers don't need to feature-detect."""
    cache.invalidate("openalex", "works", "never-cached")  # must not raise


def test_gc_orphan_tmp_files_removes_old_tmps(tmp_path, monkeypatch):
    """Killed writers leave .tmp siblings around forever; the startup
    sweep must clean up files older than the threshold while leaving
    the canonical .json (and any recent .tmp from a live writer)
    completely alone."""
    import os

    # Plant a normal cache entry and an orphan .tmp from a "previous run".
    cache.put("openalex", "works", "k", {"x": 1})
    json_path = tmp_path / "openalex" / "works" / f"{cache._cache_key('k')}.json"
    assert json_path.exists()

    orphan = tmp_path / "openalex" / "works" / "leftover.foo.tmp"
    orphan.write_text("garbage")
    old = orphan.stat().st_mtime - 7200  # 2h ago, well past 1h cutoff
    os.utime(orphan, (old, old))

    fresh = tmp_path / "biorxiv" / "papers" / "fresh.bar.tmp"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_text("from a live writer")  # mtime = now

    removed = cache.gc_orphan_tmp_files()

    assert removed == 1, f"expected to sweep one orphan, got {removed}"
    assert not orphan.exists(), "stale orphan must be unlinked"
    assert fresh.exists(), "live-writer's tmp must NOT be touched"
    assert json_path.exists(), "canonical entry must survive the sweep"


def test_gc_orphan_tmp_files_no_cache_dir(tmp_path, monkeypatch):
    """First boot has no .cache yet; sweep must not error."""
    nonexistent = tmp_path / "does-not-exist"
    monkeypatch.setattr(cache, "CACHE_ROOT", nonexistent)
    assert cache.gc_orphan_tmp_files() == 0


def test_concurrent_writers_dont_corrupt_file(tmp_path, monkeypatch):
    """Stress test: many writers hammering the same key produce a final
    file that is always valid JSON and matches one of the inputs. With
    write_text() this could leave a half-written file; with os.replace
    the worst case is "last writer wins", which is fine."""
    import threading

    errors: list[BaseException] = []

    def writer(i: int):
        try:
            for _ in range(20):
                cache.put("ns", "ent", "shared", {"writer": i, "payload": "x" * 500})
        except BaseException as e:  # noqa: BLE001 # pragma: no cover - surfaced via assert
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors

    # File is parseable and matches one of the legitimate writes.
    final = cache.get("ns", "ent", "shared")
    assert final is not None
    assert 0 <= final["writer"] < 8
    assert final["payload"] == "x" * 500

    # No stray temp files survived.
    leftover_tmps = list((tmp_path / "ns" / "ent").glob("*.tmp"))
    assert leftover_tmps == [], leftover_tmps


# ---------------------------------------------------------------------------
# Encoding: reads must be UTF-8 regardless of the host locale
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _c_ctype_locale():
    """Pin LC_CTYPE to C for the body, so the process default text encoding
    (what an encoding-less open() picks up) becomes ASCII — the situation in
    an LC_ALL=C container/cron job. Restored on exit."""
    import locale

    saved = locale.setlocale(locale.LC_CTYPE)
    locale.setlocale(locale.LC_CTYPE, "C")
    try:
        yield
    finally:
        locale.setlocale(locale.LC_CTYPE, saved)


def test_get_decodes_utf8_under_c_locale(tmp_path, monkeypatch):
    """Cache files are always written UTF-8 (ensure_ascii=False), so reads
    must decode UTF-8 too — not the locale default. Under LC_ALL=C the
    preferred encoding is ASCII; a locale-default read of an accented author
    name raises UnicodeDecodeError, and the self-heal path would silently
    delete a perfectly good entry."""
    data = {"author": "Müller, François-René"}
    cache.put("openalex", "works", "loc-test", data)  # write is explicit UTF-8
    path = tmp_path / "openalex" / "works" / f"{cache._cache_key('loc-test')}.json"
    assert path.exists()

    with _c_ctype_locale():
        result = cache.get("openalex", "works", "loc-test")

    assert result == data
    assert path.exists(), "a readable entry must not be deleted as 'corrupt'"


def test_get_negative_decodes_utf8_under_c_locale(tmp_path, monkeypatch):
    """Same UTF-8 contract for the negative cache: a 404 payload carrying a
    non-ASCII identifier must survive a read under a non-UTF-8 locale."""
    err = {"error": "No paper found for: Müller (François-René)"}
    cache.put_negative("crossref", "works", "müller-2020", err)
    path = cache._neg_path("crossref", "works", "müller-2020")
    assert path.exists()

    with _c_ctype_locale():
        cached = cache.get_negative("crossref", "works", "müller-2020")

    assert cached == err
    assert path.exists(), "a readable negative entry must not be deleted"


# ---------------------------------------------------------------------------
# Reads must reject non-dict JSON (type-contract guard)
# ---------------------------------------------------------------------------


def test_get_non_dict_json_self_heals(tmp_path, monkeypatch):
    """get() is typed dict|None. A file holding a JSON list/scalar (external
    tampering, or a foreign writer) must be treated like corruption — None
    and unlinked — not passed through as a malformed 'hit'."""
    directory = tmp_path / "openalex" / "works"
    directory.mkdir(parents=True)
    bad_path = directory / f"{cache._cache_key('listy')}.json"
    bad_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert cache.get("openalex", "works", "listy") is None
    assert not bad_path.exists(), "non-dict entry should be unlinked on read"


def test_get_negative_non_dict_json_self_heals(tmp_path, monkeypatch):
    """A non-dict negative entry must self-heal rather than crash on the
    _expires_at lookup (entry.get(...) would AttributeError on a list)."""
    neg_path = cache._neg_path("arxiv", "papers", "listy-neg")
    neg_path.parent.mkdir(parents=True, exist_ok=True)
    neg_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert cache.get_negative("arxiv", "papers", "listy-neg") is None
    assert not neg_path.exists()


def test_get_negative_preserves_underscore_payload_keys(tmp_path, monkeypatch):
    """Only the internal _expires_at bookkeeping key is stripped on read —
    caller payload keys that happen to start with '_' (e.g. _canonical_id,
    used elsewhere in the codebase) must round-trip untouched."""
    err = {"error": "not found", "_canonical_id": "10.1/y", "not_found": True}
    cache.put_negative("openalex", "works", "u-keys", err)

    cached = cache.get_negative("openalex", "works", "u-keys")
    assert cached == err
    assert "_expires_at" not in cached


def test_put_negative_reserves_the_expires_at_key(tmp_path, monkeypatch):
    """_expires_at is this module's bookkeeping slot, so a payload carrying it
    has it overwritten on write and stripped on read. No caller does this; the
    test pins the documented behaviour rather than an accident."""
    cache.put_negative("arxiv", "papers", "reserved", {"error": "x", "_expires_at": "mine"})

    cached = cache.get_negative("arxiv", "papers", "reserved")
    assert cached == {"error": "x"}


# ---------------------------------------------------------------------------
# Configurable cache root
# ---------------------------------------------------------------------------


def test_resolve_cache_root_honors_env(tmp_path, monkeypatch):
    """CACHE_DIR relocates the on-disk cache root (e.g. for an installed
    wheel); unset falls back to the project-local .cache."""
    custom = tmp_path / "custom-cache"
    monkeypatch.setenv("CACHE_DIR", str(custom))
    assert cache._resolve_cache_root() == custom

    monkeypatch.delenv("CACHE_DIR", raising=False)
    assert cache._resolve_cache_root().name == ".cache"


# ---------------------------------------------------------------------------
# cached_lookup — the shared "force_refresh -> outer check -> single-flight ->
# inner re-check" protocol that every provider getter used to hand-roll.
# ---------------------------------------------------------------------------


def test_cached_lookup_serves_positive_hit_without_fetch(tmp_path, monkeypatch):
    import asyncio

    from academic_tools_mcp import _singleflight

    cache.put("openalex", "works", "10.1/x", {"id": "W1"})
    sf = _singleflight.SingleFlight()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"id": "fetched"}

    out = asyncio.run(
        cache.cached_lookup(
            single_flight=sf,
            namespace="openalex",
            entity="works",
            canonical="10.1/x",
            positive_ttl=999.0,
            fetch=fetch,
        )
    )
    assert out == {"id": "W1"}
    assert calls == 0, "a positive cache hit must short-circuit before fetch"


def test_cached_lookup_serves_negative_hit_without_fetch(tmp_path, monkeypatch):
    import asyncio

    from academic_tools_mcp import _singleflight

    cache.put_negative("openalex", "works", "10.1/x", {"error": "404"})
    sf = _singleflight.SingleFlight()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"id": "fetched"}

    out = asyncio.run(
        cache.cached_lookup(
            single_flight=sf,
            namespace="openalex",
            entity="works",
            canonical="10.1/x",
            positive_ttl=999.0,
            fetch=fetch,
        )
    )
    assert out == {"error": "404"}
    assert calls == 0


def test_cached_lookup_force_refresh_invalidates_then_fetches(tmp_path, monkeypatch):
    import asyncio

    from academic_tools_mcp import _singleflight

    cache.put("openalex", "works", "10.1/x", {"id": "stale"})
    cache.put_negative("openalex", "works", "10.1/x", {"error": "old 404"})
    sf = _singleflight.SingleFlight()

    async def fetch():
        return {"id": "fresh"}

    out = asyncio.run(
        cache.cached_lookup(
            single_flight=sf,
            namespace="openalex",
            entity="works",
            canonical="10.1/x",
            positive_ttl=999.0,
            fetch=fetch,
            force_refresh=True,
        )
    )
    assert out == {"id": "fresh"}
    # Both halves were dropped before the fetch.
    assert cache.get_negative("openalex", "works", "10.1/x") is None


def test_cached_lookup_coalesces_concurrent_callers(tmp_path, monkeypatch):
    import asyncio

    from academic_tools_mcp import _singleflight

    sf = _singleflight.SingleFlight()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)  # let followers pile up behind the leader
        data = {"id": "W1"}
        cache.put("openalex", "works", "10.1/x", data)
        return data

    async def run():
        return await asyncio.gather(
            *[
                cache.cached_lookup(
                    single_flight=sf,
                    namespace="openalex",
                    entity="works",
                    canonical="10.1/x",
                    positive_ttl=999.0,
                    fetch=fetch,
                )
                for _ in range(5)
            ]
        )

    results = asyncio.run(run())
    assert calls == 1, "single-flight must coalesce 5 concurrent callers into one fetch"
    assert all(r == {"id": "W1"} for r in results)


def test_cached_lookup_returns_independent_copies(tmp_path, monkeypatch):
    """In-batch single-flight followers must NOT alias the leader's dict.

    Before the defensive copy, every follower shared the leader's return
    object, so a caller mutating its result corrupted the others'.
    """
    import asyncio

    from academic_tools_mcp import _singleflight

    sf = _singleflight.SingleFlight()

    async def fetch():
        await asyncio.sleep(0.01)
        data = {"id": "W1", "nested": {"k": "v"}}
        cache.put("openalex", "works", "10.1/x", data)
        return data

    async def run():
        return await asyncio.gather(
            *[
                cache.cached_lookup(
                    single_flight=sf,
                    namespace="openalex",
                    entity="works",
                    canonical="10.1/x",
                    positive_ttl=999.0,
                    fetch=fetch,
                )
                for _ in range(3)
            ]
        )

    a, b, c = asyncio.run(run())
    assert a is not b and b is not c, "each caller must get its own object"
    a["mutated"] = True
    a["nested"]["k"] = "changed"
    assert "mutated" not in b and "mutated" not in c
    assert b["nested"]["k"] == "v", "nested mutation must not leak across callers"
    # The on-disk cache must also be untouched by a caller's mutation.
    assert cache.get("openalex", "works", "10.1/x")["nested"]["k"] == "v"


def test_cached_lookup_uses_custom_single_flight_key(tmp_path, monkeypatch):
    """A tuple sf_key keeps distinct sub-fetches for one canonical id apart."""
    import asyncio

    from academic_tools_mcp import _singleflight

    sf = _singleflight.SingleFlight()
    order = []

    async def make(kind):
        async def fetch():
            order.append(kind)
            await asyncio.sleep(0.01)
            return {"kind": kind}

        return await cache.cached_lookup(
            single_flight=sf,
            namespace="opencitations",
            entity=kind,
            canonical="10.1/x",
            positive_ttl=999.0,
            fetch=fetch,
            sf_key=(kind, "10.1/x"),
        )

    async def run():
        return await asyncio.gather(make("references"), make("citations"))

    refs, cites = asyncio.run(run())
    assert refs == {"kind": "references"}
    assert cites == {"kind": "citations"}
    assert sorted(order) == ["citations", "references"], "distinct keys must not coalesce"


def test_cached_lookup_refetches_once_the_positive_ttl_expires(tmp_path, monkeypatch):
    """positive_ttl is wired through to get()'s max_age_seconds: an entry past
    it drives a fresh fetch rather than being served stale."""
    import asyncio
    import os

    from academic_tools_mcp import _singleflight

    cache.put("openalex", "works", "10.1/x", {"id": "stale"})
    path = tmp_path / "openalex" / "works" / f"{cache._cache_key('10.1/x')}.json"
    old = path.stat().st_mtime - 3600
    os.utime(path, (old, old))

    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        cache.put("openalex", "works", "10.1/x", {"id": "fresh"})
        return {"id": "fresh"}

    async def lookup(ttl):
        return await cache.cached_lookup(
            single_flight=_singleflight.SingleFlight(),
            namespace="openalex",
            entity="works",
            canonical="10.1/x",
            positive_ttl=ttl,
            fetch=fetch,
        )

    assert asyncio.run(lookup(60.0)) == {"id": "fresh"}
    assert calls == 1, "an entry past positive_ttl must not be served"

    # The refreshed entry is now within the TTL, so the next lookup is a hit.
    assert asyncio.run(lookup(3600.0)) == {"id": "fresh"}
    assert calls == 1


def test_cached_lookup_propagates_fetch_failure_and_caches_nothing(tmp_path, monkeypatch):
    """A raising fetch is a transient failure, not a result: it reaches the
    caller, nothing is written to either cache half, and the single-flight slot
    is free so the next caller retries instead of inheriting the exception."""
    import asyncio

    from academic_tools_mcp import _singleflight

    sf = _singleflight.SingleFlight()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        raise RuntimeError("upstream exploded")

    async def lookup():
        return await cache.cached_lookup(
            single_flight=sf,
            namespace="openalex",
            entity="works",
            canonical="10.1/x",
            positive_ttl=999.0,
            fetch=fetch,
        )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="upstream exploded"):
            asyncio.run(lookup())

    assert calls == 2, "a failure must not be cached in the single-flight slot"
    assert cache.get("openalex", "works", "10.1/x") is None
    assert cache.get_negative("openalex", "works", "10.1/x") is None


def test_cached_lookup_inner_recheck_spares_a_promoted_follower(tmp_path, monkeypatch):
    """The in-slot re-check earns its keep when a leader is cancelled after it
    has written the cache but before its future resolves. Single-flight then
    promotes a follower to run the factory again — and the re-check is the only
    thing standing between that promotion and a duplicate upstream call."""
    import asyncio

    from academic_tools_mcp import _singleflight

    sf = _singleflight.SingleFlight()
    calls = 0

    async def run():
        wrote = asyncio.Event()
        may_write = asyncio.Event()

        async def fetch():
            nonlocal calls
            calls += 1
            if calls > 1:
                # A duplicate call is the failure this test is about; return
                # a marker rather than blocking, so the regression shows up as
                # a failed assertion and not a hung suite.
                return {"id": "re-fetched"}
            await may_write.wait()
            cache.put("openalex", "works", "10.1/x", {"id": "from-the-leader"})
            wrote.set()
            await asyncio.sleep(3600)  # cancelled here, before the slot resolves
            raise AssertionError("unreachable")

        def lookup():
            return cache.cached_lookup(
                single_flight=sf,
                namespace="openalex",
                entity="works",
                canonical="10.1/x",
                positive_ttl=999.0,
                fetch=fetch,
            )

        leader = asyncio.create_task(lookup())
        await asyncio.sleep(0)  # let the leader claim the slot and enter fetch
        follower = asyncio.create_task(lookup())
        await asyncio.sleep(0)  # follower's outer check misses; it joins the slot

        may_write.set()
        await wrote.wait()
        leader.cancel()
        return await asyncio.wait_for(follower, timeout=5)

    result = asyncio.run(run())
    assert result == {"id": "from-the-leader"}
    assert calls == 1, "the promoted follower must read the leader's entry, not re-fetch"
