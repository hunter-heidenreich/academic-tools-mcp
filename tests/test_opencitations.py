import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_tools_mcp import opencitations


# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_bare_doi(self):
        assert opencitations._normalize_doi("10.1038/nature12373") == "10.1038/nature12373"

    def test_https_url(self):
        assert opencitations._normalize_doi("https://doi.org/10.1038/nature12373") == "10.1038/nature12373"

    def test_http_url(self):
        assert opencitations._normalize_doi("http://doi.org/10.1038/nature12373") == "10.1038/nature12373"

    def test_doi_prefix(self):
        assert opencitations._normalize_doi("doi:10.1038/nature12373") == "10.1038/nature12373"

    def test_strips_whitespace(self):
        assert opencitations._normalize_doi("  10.1038/nature12373  ") == "10.1038/nature12373"


class TestCanonicalDoi:
    def test_lowercases(self):
        assert opencitations._canonical_doi("10.1038/Nature12373") == "10.1038/nature12373"

    def test_normalizes_and_lowercases(self):
        assert opencitations._canonical_doi("https://doi.org/10.1038/Nature12373") == "10.1038/nature12373"


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ID parsing
# ---------------------------------------------------------------------------


class TestParseIds:
    def test_full_string(self):
        result = opencitations._parse_ids(
            "omid:br/062102024238 doi:10.1103/physrevx.2.031001 openalex:W3101024234 pmid:20079334"
        )
        assert result == {
            "omid": "br/062102024238",
            "doi": "10.1103/physrevx.2.031001",
            "openalex": "W3101024234",
            "pmid": "20079334",
        }

    def test_doi_only(self):
        result = opencitations._parse_ids("doi:10.1038/nature12373")
        assert result == {"doi": "10.1038/nature12373"}

    def test_empty_string(self):
        assert opencitations._parse_ids("") == {}

    def test_none(self):
        assert opencitations._parse_ids(None) == {}


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestThrottledGet:
    @pytest.mark.asyncio
    async def test_first_request_no_delay(self, monkeypatch):
        monkeypatch.setattr(opencitations, "_last_request_time", 0.0)
        monkeypatch.setattr(opencitations, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await opencitations._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 0

    @pytest.mark.asyncio
    async def test_second_request_waits(self, monkeypatch):
        monkeypatch.setattr(opencitations, "_last_request_time", time.monotonic())
        monkeypatch.setattr(opencitations, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await opencitations._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 1
        assert slept[0] >= 0.2  # should be close to 0.334

    @pytest.mark.asyncio
    async def test_no_delay_after_gap(self, monkeypatch):
        monkeypatch.setattr(
            opencitations, "_last_request_time", time.monotonic() - 1.0
        )
        monkeypatch.setattr(opencitations, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await opencitations._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 0
