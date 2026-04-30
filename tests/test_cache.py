import json

from academic_tools_mcp import cache


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


def test_put_then_get_negative_returns_payload_without_internals(
    tmp_path, monkeypatch
):
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
        "openalex", "works", "expired-1",
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
