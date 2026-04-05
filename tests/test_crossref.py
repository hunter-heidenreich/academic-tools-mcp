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
