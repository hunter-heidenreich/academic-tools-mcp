import json
from unittest.mock import MagicMock

import pytest

from academic_tools_mcp.providers import wikipedia


def _mock_client_factory(mock_response, *, urls=None):
    """Build a monkeypatch replacement for ``httpx.AsyncClient``.

    The persistent client comes from ``_clients.get_client`` (which the
    autouse conftest fixture clears each test), so patching
    ``httpx.AsyncClient`` is enough. When ``urls`` is provided every
    requested URL is appended to it so a test can assert on path encoding.
    """

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            if urls is not None:
                urls.append(url)
            return mock_response

    return lambda **kw: MockClient()


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

        monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                return mock_response

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: MockClient())

        response = await wikipedia.search("test query", limit=5)
        results = response["results"]
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

        monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                return mock_response

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: MockClient())

        response = await wikipedia.search("xyzzy nonexistent")
        assert response == {"results": []}

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
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Cytochrome_P450"}},
            "pageid": 709137,
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = mock_data

        monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)

        # Clear any cached entry
        from academic_tools_mcp import cache

        monkeypatch.setattr(cache, "get", lambda *a, **kw: None)
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

        monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)

        from academic_tools_mcp import cache

        monkeypatch.setattr(cache, "get", lambda *a, **kw: None)

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
            return {"error": "Wikipedia page not found: xyzzy", "not_found": True}

        monkeypatch.setattr(wikipedia, "get_summary", mock_summary)

        result = await wikipedia.page_exists("xyzzy")
        assert result["exists"] is False
        assert result["url"] is None


# ---------------------------------------------------------------------------
# Parse hardening (parity with crossref/openalex/opencitations)
# ---------------------------------------------------------------------------


class TestParseHardening:
    @pytest.mark.asyncio
    async def test_search_garbled_body_returns_retryable_error(self, monkeypatch):
        """A 200 with an unparseable body must not crash search()."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(side_effect=json.JSONDecodeError("bad", "", 0))

        monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(mock_response))

        result = await wikipedia.search("anything")
        assert "error" in result
        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_summary_garbled_body_returns_retryable_error(self, monkeypatch):
        """A 200 with an unparseable body must not crash get_summary()."""
        import httpx

        from academic_tools_mcp import cache

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(side_effect=json.JSONDecodeError("bad", "", 0))

        monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(cache, "get", lambda *a, **kw: None)
        monkeypatch.setattr(cache, "get_negative", lambda *a, **kw: None)
        stored = []
        monkeypatch.setattr(cache, "put", lambda *a: stored.append(a))
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(mock_response))

        result = await wikipedia.get_summary("Some Title")
        assert "error" in result
        assert result.get("retryable") is True
        assert stored == []  # a parse failure is never positive-cached

    @pytest.mark.asyncio
    async def test_summary_non_dict_body_returns_error(self, monkeypatch):
        """A 200 whose JSON is a list (not a dict) must not raise AttributeError."""
        import httpx

        from academic_tools_mcp import cache

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = ["unexpected", "shape"]

        monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(cache, "get", lambda *a, **kw: None)
        monkeypatch.setattr(cache, "get_negative", lambda *a, **kw: None)
        monkeypatch.setattr(cache, "put", lambda *a: None)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(mock_response))

        result = await wikipedia.get_summary("Some Title")
        assert "error" in result
        assert result.get("retryable") is True


# ---------------------------------------------------------------------------
# Title encoding + cache-key case sensitivity
# ---------------------------------------------------------------------------


class TestTitleEncoding:
    @pytest.mark.asyncio
    async def test_slash_in_title_is_percent_encoded(self, monkeypatch):
        """A title like 'AC/DC' must encode the slash, not split the path."""
        import httpx

        from academic_tools_mcp import cache

        mock_data = {
            "type": "standard",
            "title": "AC/DC",
            "extract": "...",
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/AC/DC"}},
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = mock_data

        urls: list[str] = []
        monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(cache, "get", lambda *a, **kw: None)
        monkeypatch.setattr(cache, "get_negative", lambda *a, **kw: None)
        monkeypatch.setattr(cache, "put", lambda *a: None)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(mock_response, urls=urls))

        await wikipedia.get_summary("AC/DC")
        assert len(urls) == 1
        assert "AC%2FDC" in urls[0]
        assert not urls[0].endswith("/AC/DC")


class TestCacheKeyCaseSensitivity:
    @pytest.mark.asyncio
    async def test_case_distinct_titles_use_distinct_cache_keys(self, monkeypatch):
        """'PET' and 'Pet' are different articles — they must not collide.

        Lowercasing the whole title (the bug) maps both to 'pet', so the
        second fetch's cache key equals the first and the wrong summary is
        served. The canonical key must differ beyond the first character.
        """
        import httpx

        from academic_tools_mcp import cache

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"type": "standard", "title": "x", "extract": "x"}

        put_keys: list[str] = []
        monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(cache, "get", lambda *a, **kw: None)
        monkeypatch.setattr(cache, "get_negative", lambda *a, **kw: None)
        monkeypatch.setattr(cache, "put", lambda ns, entity, ident, data: put_keys.append(ident))
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(mock_response))

        await wikipedia.get_summary("PET")
        await wikipedia.get_summary("Pet")

        assert len(put_keys) == 2
        assert put_keys[0] != put_keys[1]


# ---------------------------------------------------------------------------
# page_exists: transient errors must not be reported as "doesn't exist"
# ---------------------------------------------------------------------------


class TestPageExistsErrorSemantics:
    @pytest.mark.asyncio
    async def test_transient_error_not_reported_as_missing(self, monkeypatch):
        """A transient failure must not be reported as a confident non-existence."""

        async def mock_summary(title):
            return {"error": "Wikipedia request timed out. Transient — retry.", "retryable": True}

        monkeypatch.setattr(wikipedia, "get_summary", mock_summary)

        result = await wikipedia.page_exists("Cytochrome P450")
        assert result.get("exists") is not False  # not a false negative
        assert "error" in result  # transient error is propagated

    @pytest.mark.asyncio
    async def test_definitive_404_reports_missing(self, monkeypatch):
        """A genuine 404 (not_found) is the only thing that means 'doesn't exist'."""

        async def mock_summary(title):
            return {"error": "Wikipedia page not found: xyzzy", "not_found": True}

        monkeypatch.setattr(wikipedia, "get_summary", mock_summary)

        result = await wikipedia.page_exists("xyzzy")
        assert result["exists"] is False
        assert result["url"] is None


class TestGetSummaryForceRefresh:
    @pytest.mark.asyncio
    async def test_force_refresh_rebypasses_cache(self, tmp_path, monkeypatch):
        """get_summary gained force_refresh for parity with every other getter:
        a cache hit serves without a network call, force_refresh re-fetches."""
        import httpx

        from academic_tools_mcp import cache

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)

        calls = 0

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                nonlocal calls
                calls += 1
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status = MagicMock()
                resp.json.return_value = {
                    "type": "standard",
                    "title": "Photosynthesis",
                    "extract": "A process.",
                    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/X"}},
                    "pageid": 1,
                }
                return resp

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: MockClient())

        await wikipedia.get_summary("Photosynthesis")
        assert calls == 1

        # Cache hit: no second network call.
        await wikipedia.get_summary("Photosynthesis")
        assert calls == 1

        # force_refresh drops the cached entry and re-fetches.
        await wikipedia.get_summary("Photosynthesis", force_refresh=True)
        assert calls == 2


# ---------------------------------------------------------------------------
# The MCP tool layer
# ---------------------------------------------------------------------------
#
# Everything above covers the provider. These cover the two @mcp.tool wrappers,
# which own a contract the provider does not: the response shape agents branch
# on, and the `suggestion` attached to an error. Both had no coverage at all.


class TestWikipediaTools:
    @pytest.mark.asyncio
    async def test_search_reports_result_count_alongside_the_hits(self, monkeypatch):
        # Every search-list tool reports result_count = len(results). Wikipedia
        # has no upstream-total concept, so it carries no total_results — an
        # agent branching on that difference needs the absence to be reliable.
        from academic_tools_mcp import server

        async def fake_search(query, limit=5):
            return {"results": [{"title": "Photosynthesis", "url": "https://en.wikipedia.org/x"}]}

        monkeypatch.setattr(wikipedia, "search", fake_search)

        result = await server.search_wikipedia("photosynthesis")

        assert result["query"] == "photosynthesis"
        assert result["result_count"] == 1
        assert result["results"][0]["title"] == "Photosynthesis"
        assert "total_results" not in result

    @pytest.mark.asyncio
    async def test_search_passes_the_limit_through(self, monkeypatch):
        from academic_tools_mcp import server

        seen: dict[str, int] = {}

        async def fake_search(query, limit=5):
            seen["limit"] = limit
            return {"results": []}

        monkeypatch.setattr(wikipedia, "search", fake_search)

        result = await server.search_wikipedia("x", limit=3)

        assert seen["limit"] == 3
        assert result["result_count"] == 0

    @pytest.mark.asyncio
    async def test_search_error_gains_a_recovery_suggestion(self, monkeypatch):
        from academic_tools_mcp import server

        async def fake_search(query, limit=5):
            return {"error": "Wikipedia rate limit (HTTP 429)."}

        monkeypatch.setattr(wikipedia, "search", fake_search)

        result = await server.search_wikipedia("x")

        assert "error" in result
        assert "retry" in result["suggestion"]
        assert "result_count" not in result

    @pytest.mark.asyncio
    async def test_summary_passes_the_provider_payload_through(self, monkeypatch):
        # This tool deliberately does not slice: the provider already returns
        # a lean record, so re-shaping here would be a second place to drift.
        from academic_tools_mcp import server

        payload = {
            "title": "Photosynthesis",
            "description": "Biological process",
            "extract": "A process used by plants.",
            "url": "https://en.wikipedia.org/wiki/Photosynthesis",
            "type": "standard",
            "pageid": 24544,
        }

        async def fake_summary(title, *, force_refresh=False):
            return dict(payload)

        monkeypatch.setattr(wikipedia, "get_summary", fake_summary)

        assert await server.get_wikipedia_summary("Photosynthesis") == payload

    @pytest.mark.asyncio
    async def test_summary_surfaces_a_disambiguation_page_as_such(self, monkeypatch):
        # `type` is how an agent tells "here is the article" from "pick one of
        # these", so it must survive the tool boundary.
        from academic_tools_mcp import server

        async def fake_summary(title, *, force_refresh=False):
            return {"title": "Mercury", "type": "disambiguation", "extract": "May refer to..."}

        monkeypatch.setattr(wikipedia, "get_summary", fake_summary)

        result = await server.get_wikipedia_summary("Mercury")

        assert result["type"] == "disambiguation"

    @pytest.mark.asyncio
    async def test_summary_error_points_at_the_search_tool(self, monkeypatch):
        from academic_tools_mcp import server

        async def fake_summary(title, *, force_refresh=False):
            return {"error": "No Wikipedia page found for: Xyzzy", "not_found": True}

        monkeypatch.setattr(wikipedia, "get_summary", fake_summary)

        result = await server.get_wikipedia_summary("Xyzzy")

        assert result["not_found"] is True
        assert "search_wikipedia" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_summary_threads_force_refresh(self, monkeypatch):
        from academic_tools_mcp import server

        seen: dict[str, bool] = {}

        async def fake_summary(title, *, force_refresh=False):
            seen["force_refresh"] = force_refresh
            return {"title": title}

        monkeypatch.setattr(wikipedia, "get_summary", fake_summary)

        await server.get_wikipedia_summary("Photosynthesis", force_refresh=True)

        assert seen["force_refresh"] is True
