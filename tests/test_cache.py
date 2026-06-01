import contextlib
import json

from academic_tools_mcp import cache


def test_cache_dir_is_public_and_namespaced(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    # Public name (no leading underscore) so the PDF-handling modules can
    # build canonical paths without reaching for a private helper.
    assert cache.cache_dir("openalex", "works") == tmp_path / "openalex" / "works"


def test_put_and_get(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    data = {"id": "W123", "title": "Test Paper"}
    cache.put("openalex", "works", "10.1234/test", data)

    result = cache.get("openalex", "works", "10.1234/test")
    assert result == data


def test_get_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    assert cache.get("openalex", "works", "nonexistent") is None


def test_has(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    assert cache.has("openalex", "works", "10.1234/test") is False

    cache.put("openalex", "works", "10.1234/test", {"title": "Test"})
    assert cache.has("openalex", "works", "10.1234/test") is True


def test_namespacing(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    cache.put("openalex", "works", "key1", {"source": "openalex"})
    cache.put("arxiv", "papers", "key1", {"source": "arxiv"})

    assert cache.get("openalex", "works", "key1")["source"] == "openalex"
    assert cache.get("arxiv", "papers", "key1")["source"] == "arxiv"


def test_unicode_data(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    data = {"author": "Müller, François-René"}
    cache.put("openalex", "works", "unicode-test", data)

    result = cache.get("openalex", "works", "unicode-test")
    assert result["author"] == "Müller, François-René"


def test_cache_file_is_valid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
    assert cache.get_negative("openalex", "works", "missing") is None


def test_put_then_get_negative_returns_payload_without_internals(tmp_path, monkeypatch):
    # The agent should see the same {error: ...} shape it would have
    # gotten from a fresh 404 — _expires_at is bookkeeping and must not
    # leak through.
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    cache.put("openalex", "works", "10.1/x", {"title": "Real"})
    cache.put_negative("openalex", "works", "10.1/x", {"error": "stale"})

    assert cache.get("openalex", "works", "10.1/x") == {"title": "Real"}
    assert cache.get_negative("openalex", "works", "10.1/x") == {"error": "stale"}


def test_expired_negative_entry_self_heals(tmp_path, monkeypatch):
    # Past-its-TTL negative entries must be treated as a cache miss
    # AND removed on read so they don't accumulate forever.
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    neg_path = cache._neg_path("arxiv", "papers", "junk-1")
    neg_path.parent.mkdir(parents=True, exist_ok=True)
    neg_path.write_text('{"error": "tru')  # truncated mid-string

    assert cache.get_negative("arxiv", "papers", "junk-1") is None
    assert not neg_path.exists()


def test_negative_entry_missing_expires_at_self_heals(tmp_path, monkeypatch):
    # A negative file that's syntactically valid JSON but missing the
    # _expires_at sentinel must not be trusted forever — treat it as
    # expired so the next put rebuilds it cleanly.
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
    cache.put("openalex", "works", "fresh", {"title": "Fresh"})
    # Generous TTL → entry survives.
    assert cache.get("openalex", "works", "fresh", max_age_seconds=3600) == {"title": "Fresh"}


def test_invalidate_drops_positive_and_negative(tmp_path, monkeypatch):
    """force_refresh drops both halves so a previously-404'd identifier
    can resolve on the retry — a stale negative wouldn't expire for 24h
    on its own."""
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
    cache.invalidate("openalex", "works", "never-cached")  # must not raise


def test_gc_orphan_tmp_files_removes_old_tmps(tmp_path, monkeypatch):
    """Killed writers leave .tmp siblings around forever; the startup
    sweep must clean up files older than the threshold while leaving
    the canonical .json (and any recent .tmp from a live writer)
    completely alone."""
    import os

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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
    monkeypatch.setattr(cache, "_CACHE_ROOT", nonexistent)
    assert cache.gc_orphan_tmp_files() == 0


def test_concurrent_writers_dont_corrupt_file(tmp_path, monkeypatch):
    """Stress test: many writers hammering the same key produce a final
    file that is always valid JSON and matches one of the inputs. With
    write_text() this could leave a half-written file; with os.replace
    the worst case is "last writer wins", which is fine."""
    import threading

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    errors: list[BaseException] = []

    def writer(i: int):
        try:
            for _ in range(20):
                cache.put("ns", "ent", "shared", {"writer": i, "payload": "x" * 500})
        except BaseException as e:  # pragma: no cover - surfaced via assert
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
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

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
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    directory = tmp_path / "openalex" / "works"
    directory.mkdir(parents=True)
    bad_path = directory / f"{cache._cache_key('listy')}.json"
    bad_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert cache.get("openalex", "works", "listy") is None
    assert not bad_path.exists(), "non-dict entry should be unlinked on read"


def test_get_negative_non_dict_json_self_heals(tmp_path, monkeypatch):
    """A non-dict negative entry must self-heal rather than crash on the
    _expires_at lookup (entry.get(...) would AttributeError on a list)."""
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    neg_path = cache._neg_path("arxiv", "papers", "listy-neg")
    neg_path.parent.mkdir(parents=True, exist_ok=True)
    neg_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    assert cache.get_negative("arxiv", "papers", "listy-neg") is None
    assert not neg_path.exists()


def test_get_negative_preserves_underscore_payload_keys(tmp_path, monkeypatch):
    """Only the internal _expires_at bookkeeping key is stripped on read —
    caller payload keys that happen to start with '_' (e.g. _canonical_id,
    used elsewhere in the codebase) must round-trip untouched."""
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)

    err = {"error": "not found", "_canonical_id": "10.1/y", "not_found": True}
    cache.put_negative("openalex", "works", "u-keys", err)

    cached = cache.get_negative("openalex", "works", "u-keys")
    assert cached == err
    assert "_expires_at" not in cached


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
# Atomic primitives: _atomic_write_text / _atomic_copy
# ---------------------------------------------------------------------------


def test_atomic_write_text_roundtrips_utf8(tmp_path):
    """_atomic_write_text writes UTF-8 regardless of host locale and lands
    the file in place."""
    path = tmp_path / "nested" / "note.md"
    payload = "## Café\n\nMüller, François-René — 数据\n"
    cache._atomic_write_text(path, payload)
    assert path.read_text(encoding="utf-8") == payload


def test_atomic_write_text_no_torn_file_on_failure(tmp_path, monkeypatch):
    """If the rename fails, the canonical path is never created and the
    sibling temp is cleaned up — a reader can't see a half-written file."""
    path = tmp_path / "note.md"

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(cache.os, "replace", boom)

    import pytest

    with pytest.raises(OSError):
        cache._atomic_write_text(path, "some content")

    assert not path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_copy_roundtrips_bytes_and_metadata(tmp_path):
    """_atomic_copy reproduces the source bytes and lands the file in place."""
    src = tmp_path / "src.bin"
    body = b"%PDF-1.4 binary \x00\x01\x02 payload"
    src.write_bytes(body)
    dst = tmp_path / "sub" / "dst.bin"

    cache._atomic_copy(src, dst)
    assert dst.read_bytes() == body


def test_atomic_copy_no_torn_file_on_failure(tmp_path, monkeypatch):
    """A copy that blows up mid-stream leaves no canonical file (the failure
    mode a plain shutil.copy to dst would torn-write) and no leftover temp."""
    src = tmp_path / "src.pdf"
    src.write_bytes(b"%PDF-1.4 source bytes")
    dst = tmp_path / "dst.pdf"

    def boom(*_a, **_kw):
        raise OSError("copy interrupted")

    monkeypatch.setattr(cache.shutil, "copyfileobj", boom)

    import pytest

    with pytest.raises(OSError):
        cache._atomic_copy(src, dst)

    assert not dst.exists()
    assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# cached_lookup — the shared "force_refresh -> outer check -> single-flight ->
# inner re-check" protocol that every provider getter used to hand-roll.
# ---------------------------------------------------------------------------


def test_cached_lookup_serves_positive_hit_without_fetch(tmp_path, monkeypatch):
    import asyncio

    from academic_tools_mcp import _singleflight

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
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

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
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

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
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

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
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

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
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

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
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
