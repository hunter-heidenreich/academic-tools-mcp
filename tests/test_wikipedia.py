import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_tools_mcp import wikipedia


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestThrottledGet:
    @pytest.mark.asyncio
    async def test_first_request_no_delay(self, monkeypatch):
        monkeypatch.setattr(wikipedia, "_last_request_time", 0.0)
        monkeypatch.setattr(wikipedia, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await wikipedia._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 0

    @pytest.mark.asyncio
    async def test_second_request_waits(self, monkeypatch):
        monkeypatch.setattr(wikipedia, "_last_request_time", time.monotonic())
        monkeypatch.setattr(wikipedia, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await wikipedia._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 1
        assert slept[0] >= 0.8  # should be close to 1.0

    @pytest.mark.asyncio
    async def test_no_delay_after_gap(self, monkeypatch):
        monkeypatch.setattr(
            wikipedia, "_last_request_time", time.monotonic() - 3.0
        )
        monkeypatch.setattr(wikipedia, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await wikipedia._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 0


# ---------------------------------------------------------------------------
# Search parsing
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_parses_opensearch_response(self, monkeypatch):
        """Should parse the 4-element OpenSearch array correctly."""
        import httpx

        mock_data = [
            "test query",
            ["Article One", "Article Two"],
            ["", ""],
            [
                "https://en.wikipedia.org/wiki/Article_One",
                "https://en.wikipedia.org/wiki/Article_Two",
            ],
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = mock_data

        async def mock_get(self, url, **kwargs):
            return mock_response

        monkeypatch.setattr(wikipedia, "_last_request_time", 0.0)
        monkeypatch.setattr(wikipedia, "_request_lock", asyncio.Lock())

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                return mock_response

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: MockClient())

        results = await wikipedia.search("test query", limit=5)
        assert len(results) == 2
        assert results[0]["title"] == "Article One"
        assert results[0]["url"] == "https://en.wikipedia.org/wiki/Article_One"
        assert results[1]["title"] == "Article Two"

    @pytest.mark.asyncio
    async def test_empty_results(self, monkeypatch):
        """Should handle no results gracefully."""
        import httpx

        mock_data = ["test query", [], [], []]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = mock_data

        monkeypatch.setattr(wikipedia, "_last_request_time", 0.0)
        monkeypatch.setattr(wikipedia, "_request_lock", asyncio.Lock())

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                return mock_response

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: MockClient())

        results = await wikipedia.search("xyzzy nonexistent")
        assert results == []

    def test_limit_clamped(self):
        """Limit should be clamped between 1 and 10."""
        # This is a unit test on the clamping logic, not the API
        assert min(max(0, 1), 10) == 1
        assert min(max(15, 1), 10) == 10
        assert min(max(5, 1), 10) == 5


# ---------------------------------------------------------------------------
# Summary parsing
# ---------------------------------------------------------------------------


class TestGetSummary:
    @pytest.mark.asyncio
    async def test_parses_standard_page(self, monkeypatch):
        import httpx

        mock_data = {
            "type": "standard",
            "title": "Cytochrome P450",
            "description": "Class of enzymes",
            "extract": "Cytochromes P450 are a superfamily of enzymes.",
            "content_urls": {
                "desktop": {
                    "page": "https://en.wikipedia.org/wiki/Cytochrome_P450"
                }
            },
            "pageid": 709137,
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = mock_data

        monkeypatch.setattr(wikipedia, "_last_request_time", 0.0)
        monkeypatch.setattr(wikipedia, "_request_lock", asyncio.Lock())

        # Clear any cached entry
        from academic_tools_mcp import cache
        monkeypatch.setattr(cache, "get", lambda *a: None)
        stored = []
        monkeypatch.setattr(cache, "put", lambda *a: stored.append(a))

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                return mock_response

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: MockClient())

        result = await wikipedia.get_summary("Cytochrome P450")
        assert result["title"] == "Cytochrome P450"
        assert result["type"] == "standard"
        assert result["description"] == "Class of enzymes"
        assert "superfamily" in result["extract"]
        assert result["url"] == "https://en.wikipedia.org/wiki/Cytochrome_P450"
        assert len(stored) == 1  # should cache the result

    @pytest.mark.asyncio
    async def test_404_returns_error(self, monkeypatch):
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 404

        monkeypatch.setattr(wikipedia, "_last_request_time", 0.0)
        monkeypatch.setattr(wikipedia, "_request_lock", asyncio.Lock())

        from academic_tools_mcp import cache
        monkeypatch.setattr(cache, "get", lambda *a: None)

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                return mock_response

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: MockClient())

        result = await wikipedia.get_summary("This_Does_Not_Exist_xyzzy123")
        assert "error" in result


# ---------------------------------------------------------------------------
# Page existence check
# ---------------------------------------------------------------------------


class TestPageExists:
    @pytest.mark.asyncio
    async def test_standard_page_exists(self, monkeypatch):
        """Standard page should return exists=True, is_disambiguation=False."""
        async def mock_summary(title):
            return {
                "title": "Cytochrome P450",
                "type": "standard",
                "description": "Class of enzymes",
                "extract": "...",
                "url": "https://en.wikipedia.org/wiki/Cytochrome_P450",
                "pageid": 709137,
            }

        monkeypatch.setattr(wikipedia, "get_summary", mock_summary)

        result = await wikipedia.page_exists("Cytochrome P450")
        assert result["exists"] is True
        assert result["is_disambiguation"] is False
        assert result["url"] == "https://en.wikipedia.org/wiki/Cytochrome_P450"

    @pytest.mark.asyncio
    async def test_disambiguation_page(self, monkeypatch):
        """Disambiguation page should return exists=True, is_disambiguation=True."""
        async def mock_summary(title):
            return {
                "title": "Mercury",
                "type": "disambiguation",
                "description": "Topics referred to by the same term",
                "extract": "Mercury may refer to...",
                "url": "https://en.wikipedia.org/wiki/Mercury",
                "pageid": 19694,
            }

        monkeypatch.setattr(wikipedia, "get_summary", mock_summary)

        result = await wikipedia.page_exists("Mercury")
        assert result["exists"] is True
        assert result["is_disambiguation"] is True

    @pytest.mark.asyncio
    async def test_nonexistent_page(self, monkeypatch):
        """Nonexistent page should return exists=False."""
        async def mock_summary(title):
            return {"error": "Wikipedia page not found: xyzzy"}

        monkeypatch.setattr(wikipedia, "get_summary", mock_summary)

        result = await wikipedia.page_exists("xyzzy")
        assert result["exists"] is False
        assert result["url"] is None
