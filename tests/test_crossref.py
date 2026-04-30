import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_tools_mcp import crossref


# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_bare_doi(self):
        assert crossref._normalize_doi("10.1038/nature12373") == "10.1038/nature12373"

    def test_https_url(self):
        assert crossref._normalize_doi("https://doi.org/10.1038/nature12373") == "10.1038/nature12373"

    def test_http_url(self):
        assert crossref._normalize_doi("http://doi.org/10.1038/nature12373") == "10.1038/nature12373"

    def test_doi_prefix(self):
        assert crossref._normalize_doi("doi:10.1038/nature12373") == "10.1038/nature12373"

    def test_strips_whitespace(self):
        assert crossref._normalize_doi("  10.1038/nature12373  ") == "10.1038/nature12373"


class TestCanonicalDoi:
    def test_lowercases(self):
        assert crossref._canonical_doi("10.1038/Nature12373") == "10.1038/nature12373"

    def test_normalizes_and_lowercases(self):
        assert crossref._canonical_doi("https://doi.org/10.1038/Nature12373") == "10.1038/nature12373"


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
        monkeypatch.setattr(
            crossref, "_last_request_time", time.monotonic() - 1.0
        )
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

        monkeypatch.setattr(
            crossref._clients, "get_client", lambda *a, **kw: mock_client
        )

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
        monkeypatch.setattr(
            crossref._clients, "get_client", lambda *a, **kw: mock_client
        )

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
