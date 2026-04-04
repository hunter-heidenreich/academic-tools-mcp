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

    # Find the file and verify it's readable JSON
    files = list((tmp_path / "openalex" / "works").iterdir())
    assert len(files) == 1
    parsed = json.loads(files[0].read_text())
    assert parsed == data
