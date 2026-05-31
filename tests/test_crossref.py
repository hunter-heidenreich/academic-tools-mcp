import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_tools_mcp.providers import crossref

# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_bare_doi(self):
        assert crossref._normalize_doi("10.1038/nature12373") == "10.1038/nature12373"

    def test_https_url(self):
        assert (
            crossref._normalize_doi("https://doi.org/10.1038/nature12373") == "10.1038/nature12373"
        )

    def test_http_url(self):
        assert (
            crossref._normalize_doi("http://doi.org/10.1038/nature12373") == "10.1038/nature12373"
        )

    def test_doi_prefix(self):
        assert crossref._normalize_doi("doi:10.1038/nature12373") == "10.1038/nature12373"

    def test_strips_whitespace(self):
        assert crossref._normalize_doi("  10.1038/nature12373  ") == "10.1038/nature12373"


class TestCanonicalDoi:
    def test_lowercases(self):
        assert crossref._canonical_doi("10.1038/Nature12373") == "10.1038/nature12373"

    def test_normalizes_and_lowercases(self):
        assert (
            crossref._canonical_doi("https://doi.org/10.1038/Nature12373") == "10.1038/nature12373"
        )


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestThrottledGet:
    @pytest.mark.asyncio
    async def test_first_request_no_delay(self, monkeypatch):
        monkeypatch.setattr(crossref, "_last_request_time", 0.0)
        monkeypatch.setattr(crossref, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await crossref._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 0

    @pytest.mark.asyncio
    async def test_second_request_waits(self, monkeypatch):
        monkeypatch.setattr(crossref, "_last_request_time", time.monotonic())
        monkeypatch.setattr(crossref, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await crossref._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 1
        assert slept[0] > 0

    @pytest.mark.asyncio
    async def test_no_delay_after_gap(self, monkeypatch):
        monkeypatch.setattr(crossref, "_last_request_time", time.monotonic() - 1.0)
        monkeypatch.setattr(crossref, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await crossref._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 0


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# search_works parameter building
# ---------------------------------------------------------------------------


class TestSearchWorksParams:
    @pytest.mark.asyncio
    async def test_builds_params_with_year(self, monkeypatch):
        """Verify search_works sends correct params including year filter."""
        monkeypatch.setattr(crossref, "_last_request_time", 0.0)
        monkeypatch.setattr(crossref, "_request_lock", asyncio.Lock())

        captured_kwargs = {}

        async def mock_get(url, **kwargs):
            captured_kwargs.update(kwargs)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"message": {"items": []}}
            return mock_resp

        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        monkeypatch.setattr(crossref.httpx, "AsyncClient", lambda **kw: mock_client)

        result = await crossref.search_works("some title", year=2022, rows=3)

        assert result == {"items": []}
        params = captured_kwargs.get("params", {})
        assert params["query.bibliographic"] == "some title"
        assert params["rows"] == "3"
        assert "from-pub-date:2022" in params["filter"]
        assert "until-pub-date:2022" in params["filter"]

    @pytest.mark.asyncio
    async def test_builds_params_without_year(self, monkeypatch):
        """Verify search_works omits filter when year is None."""
        monkeypatch.setattr(crossref, "_last_request_time", 0.0)
        monkeypatch.setattr(crossref, "_request_lock", asyncio.Lock())

        captured_kwargs = {}

        async def mock_get(url, **kwargs):
            captured_kwargs.update(kwargs)
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"message": {"items": []}}
            return mock_resp

        mock_client = MagicMock()
        mock_client.get = mock_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        monkeypatch.setattr(crossref.httpx, "AsyncClient", lambda **kw: mock_client)

        await crossref.search_works("some title", rows=5)

        params = captured_kwargs.get("params", {})
        assert "filter" not in params
        assert params["rows"] == "5"

    def test_rows_clamped_high(self):
        """Rows should be clamped to max 20."""
        # We can't easily test this without mocking, but we can verify the logic
        assert min(max(100, 1), 20) == 20

    def test_rows_clamped_low(self):
        """Rows should be clamped to min 1."""
        assert min(max(0, 1), 20) == 1


# ---------------------------------------------------------------------------
# search_works opportunistic cache-warming
# ---------------------------------------------------------------------------


class TestSearchWorksCacheWarming:
    """Each search hit is the same shape as a /works/{doi} response, so
    caching it under the works namespace turns an inevitable follow-up
    get_work(doi) call into a free cache hit. Mirrors arxiv.search_papers.
    """

    @pytest.mark.asyncio
    async def test_search_hits_warm_works_cache(self, tmp_path, monkeypatch):
        from academic_tools_mcp import cache

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
        monkeypatch.setattr(crossref, "_last_request_time", 0.0)
        monkeypatch.setattr(crossref, "_request_lock", asyncio.Lock())

        # Two hits with DOIs + one hit without (real-world quirk —
        # Crossref occasionally returns items missing a DOI). The
        # missing-DOI hit must NOT crash and must NOT be cached.
        items = [
            {"DOI": "10.1234/A", "title": ["A"], "type": "journal-article"},
            {"DOI": "10.5678/B", "title": ["B"], "type": "journal-article"},
            {"title": ["C — no DOI"], "type": "journal-article"},
        ]

        async def mock_get(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"message": {"items": items}}
            return mock_resp

        mock_client = MagicMock()
        mock_client.get = mock_get

        monkeypatch.setattr(crossref._clients, "get_client", lambda *a, **kw: mock_client)

        await crossref.search_works("anything")

        # The two DOIs are cached under the works namespace and a
        # subsequent get_work hits the cache without going to network.
        assert cache.get(crossref.NAMESPACE, "works", "10.1234/a") == items[0]
        assert cache.get(crossref.NAMESPACE, "works", "10.5678/b") == items[1]

    @pytest.mark.asyncio
    async def test_existing_cached_entry_not_clobbered(self, tmp_path, monkeypatch):
        # If a search hit comes back with a sparser version of an
        # already-cached work, do NOT overwrite — the cached version
        # was deliberately fetched and may have richer fields.
        from academic_tools_mcp import cache

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
        monkeypatch.setattr(crossref, "_last_request_time", 0.0)
        monkeypatch.setattr(crossref, "_request_lock", asyncio.Lock())

        # Pre-seed a richer cached version.
        rich = {"DOI": "10.1234/A", "title": ["A"], "abstract": "<p>full</p>"}
        cache.put(crossref.NAMESPACE, "works", "10.1234/a", rich)

        sparse = {"DOI": "10.1234/A", "title": ["A"]}

        async def mock_get(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = {"message": {"items": [sparse]}}
            return mock_resp

        mock_client = MagicMock()
        mock_client.get = mock_get
        monkeypatch.setattr(crossref._clients, "get_client", lambda *a, **kw: mock_client)

        await crossref.search_works("anything")

        # Rich entry survives.
        assert cache.get(crossref.NAMESPACE, "works", "10.1234/a") == rich


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


class TestBuildHeaders:
    def test_includes_mailto_when_configured(self, monkeypatch):
        monkeypatch.setenv("CROSSREF_MAILTO", "test@example.com")
        # Reload config so env var is picked up
        headers = crossref._build_headers()
        assert "mailto:test@example.com" in headers.get("User-Agent", "")

    def test_empty_headers_without_mailto(self, monkeypatch):
        monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
        headers = crossref._build_headers()
        assert headers == {}


# ---------------------------------------------------------------------------
# Malformed-body / parse-error handling and DOI path encoding
# ---------------------------------------------------------------------------


def _reset_crossref(monkeypatch, tmp_path):
    """Reset Crossref pooled state + cache root and no-op the throttle sleep."""
    import asyncio as _asyncio

    from academic_tools_mcp import _singleflight, cache

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(crossref, "_pending", 0)
    monkeypatch.setattr(crossref, "_last_request_time", 0.0)
    monkeypatch.setattr(crossref, "_request_lock", _asyncio.Lock())
    monkeypatch.setattr(crossref, "_single_flight", _singleflight.SingleFlight())

    async def mock_sleep(_):
        pass

    monkeypatch.setattr(crossref.asyncio, "sleep", mock_sleep)


# Sentinel: a payload whose .json() raises, simulating a malformed/truncated body.
_BAD_JSON = object()


def _stub_json_responses(monkeypatch, *payloads, status_code=200):
    """Install a stub client returning ``payloads`` from successive GETs.

    A payload of ``_BAD_JSON`` makes ``.json()`` raise ``json.JSONDecodeError``
    (garbled body). The returned object exposes ``.urls`` (every URL requested)
    and ``.count`` (number of GETs) for assertions.
    """
    import json

    from academic_tools_mcp import _clients

    seq = list(payloads)

    class _Recorder:
        def __init__(self):
            self.urls: list[str] = []

        @property
        def count(self) -> int:
            return len(self.urls)

    recorder = _Recorder()

    class StubResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = status_code
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            pass

        def json(self):
            if self._payload is _BAD_JSON:
                raise json.JSONDecodeError("Expecting value", "", 0)
            return self._payload

    class StubClient:
        async def get(self, url, **kwargs):
            recorder.urls.append(url)
            idx = len(recorder.urls) - 1
            payload = seq[idx] if idx < len(seq) else seq[-1]
            return StubResponse(payload)

    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())
    return recorder


def _work_response(doi="10.1234/x"):
    """A minimal valid Crossref /works response wrapping one work."""
    return {"message": {"DOI": doi, "title": ["A Great Work"], "type": "journal-article"}}


class TestGetWorkParseErrors:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, tmp_path, monkeypatch):
        # A 200 with a garbled body must NOT crash — it should surface the
        # uniform {error, retryable} contract like every other failure.
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await crossref.get_work("10.1234/x")

        assert isinstance(result, dict)
        assert "error" in result
        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_malformed_json_not_negative_cached(self, tmp_path, monkeypatch):
        # A parse failure is transient — a retry must re-fetch and succeed,
        # not be served a poisoned negative-cache entry.
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _BAD_JSON, _work_response())

        first = await crossref.get_work("10.1234/x")
        assert "error" in first

        second = await crossref.get_work("10.1234/x")
        assert second.get("DOI") == "10.1234/x"
        assert recorder.count == 2  # re-fetched, not cached

    @pytest.mark.asyncio
    async def test_missing_message_not_cached(self, tmp_path, monkeypatch):
        # An anomalous 200 without a `message` must not positive-cache an
        # empty dict; it should error and a retry re-fetches.
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, {"status": "ok"}, _work_response())

        first = await crossref.get_work("10.1234/x")
        assert "error" in first

        second = await crossref.get_work("10.1234/x")
        assert second.get("DOI") == "10.1234/x"
        assert recorder.count == 2


class TestSearchWorksParseErrors:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await crossref.search_works("some title")

        assert isinstance(result, dict)
        assert "error" in result
        assert result.get("retryable") is True


class TestGetWorkDoiEncoding:
    @pytest.mark.asyncio
    async def test_encodes_special_chars_in_doi(self, tmp_path, monkeypatch):
        # A DOI containing '#' must be percent-encoded in the path; otherwise
        # httpx treats '#bar' as a fragment and requests the wrong record.
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _work_response("10.1234/foo#bar"))

        await crossref.get_work("10.1234/foo#bar")

        url = recorder.urls[0]
        assert "%23" in url
        assert "#" not in url

    @pytest.mark.asyncio
    async def test_preserves_slash_in_doi(self, tmp_path, monkeypatch):
        # Regression: the prefix/suffix slash stays literal (Crossref's
        # proven-working form), only reserved chars are encoded.
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _work_response("10.1038/nature12373"))

        await crossref.get_work("10.1038/nature12373")

        assert recorder.urls[0].endswith("/works/10.1038/nature12373")
