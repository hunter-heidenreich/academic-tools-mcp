import asyncio
import time
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_tools_mcp import arxiv


# ---------------------------------------------------------------------------
# ID normalization
# ---------------------------------------------------------------------------


class TestNormalizeArxivId:
    def test_bare_new_style(self):
        assert arxiv._normalize_arxiv_id("2301.00001") == "2301.00001"

    def test_bare_new_style_with_version(self):
        assert arxiv._normalize_arxiv_id("2301.00001v2") == "2301.00001v2"

    def test_bare_old_style(self):
        assert arxiv._normalize_arxiv_id("hep-th/9901001") == "hep-th/9901001"

    def test_bare_old_style_with_version(self):
        assert arxiv._normalize_arxiv_id("hep-th/9901001v1") == "hep-th/9901001v1"

    def test_abs_url(self):
        assert arxiv._normalize_arxiv_id("https://arxiv.org/abs/2301.00001") == "2301.00001"

    def test_abs_url_with_version(self):
        assert arxiv._normalize_arxiv_id("https://arxiv.org/abs/2301.00001v2") == "2301.00001v2"

    def test_pdf_url_with_extension(self):
        assert arxiv._normalize_arxiv_id("https://arxiv.org/pdf/2301.00001.pdf") == "2301.00001"

    def test_pdf_url_without_extension(self):
        assert arxiv._normalize_arxiv_id("https://arxiv.org/pdf/2301.00001v2") == "2301.00001v2"

    def test_old_style_abs_url(self):
        assert arxiv._normalize_arxiv_id("https://arxiv.org/abs/hep-th/9901001") == "hep-th/9901001"

    def test_strips_whitespace(self):
        assert arxiv._normalize_arxiv_id("  2301.00001  ") == "2301.00001"

    def test_http_url(self):
        assert arxiv._normalize_arxiv_id("http://arxiv.org/abs/2301.00001") == "2301.00001"


class TestCanonicalArxivId:
    def test_strips_version(self):
        assert arxiv._canonical_arxiv_id("2301.00001v2") == "2301.00001"

    def test_no_version(self):
        assert arxiv._canonical_arxiv_id("2301.00001") == "2301.00001"

    def test_lowercases(self):
        assert arxiv._canonical_arxiv_id("hep-TH/9901001") == "hep-th/9901001"

    def test_url_strips_version_and_lowercases(self):
        assert arxiv._canonical_arxiv_id("https://arxiv.org/abs/2301.00001v3") == "2301.00001"

    def test_old_style_strips_version(self):
        assert arxiv._canonical_arxiv_id("hep-th/9901001v1") == "hep-th/9901001"


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------

_SAMPLE_ENTRY_XML = """\
<entry xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <id>http://arxiv.org/abs/1706.03762v7</id>
  <updated>2023-08-02T00:52:10Z</updated>
  <published>2017-06-12T17:57:34Z</published>
  <title>Attention Is All
    You Need</title>
  <summary>The dominant sequence transduction models are based on complex
    recurrent or convolutional neural networks.</summary>
  <author>
    <name>Ashish Vaswani</name>
    <arxiv:affiliation>Google Brain</arxiv:affiliation>
  </author>
  <author>
    <name>Noam Shazeer</name>
  </author>
  <arxiv:comment>15 pages, 5 figures</arxiv:comment>
  <arxiv:journal_ref>Advances in Neural Information Processing Systems 30 (2017)</arxiv:journal_ref>
  <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
  <link href="http://arxiv.org/abs/1706.03762v7" rel="alternate" type="text/html"/>
  <link href="http://arxiv.org/pdf/1706.03762v7" rel="related" type="application/pdf" title="pdf"/>
  <arxiv:primary_category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
  <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
  <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
</entry>
"""

_MINIMAL_ENTRY_XML = """\
<entry xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <id>http://arxiv.org/abs/2301.00001v1</id>
  <updated>2023-01-01T00:00:00Z</updated>
  <published>2023-01-01T00:00:00Z</published>
  <title>A Simple Paper</title>
  <summary>A short abstract.</summary>
  <author>
    <name>Jane Doe</name>
  </author>
  <link href="http://arxiv.org/abs/2301.00001v1" rel="alternate" type="text/html"/>
  <arxiv:primary_category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
  <category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/>
</entry>
"""


class TestParseEntry:
    def _parse(self, xml_str: str) -> dict:
        element = ET.fromstring(xml_str)
        return arxiv._parse_entry(element)

    def test_parses_id(self):
        result = self._parse(_SAMPLE_ENTRY_XML)
        assert result["id"] == "http://arxiv.org/abs/1706.03762v7"

    def test_collapses_whitespace_in_title(self):
        result = self._parse(_SAMPLE_ENTRY_XML)
        assert result["title"] == "Attention Is All You Need"

    def test_collapses_whitespace_in_summary(self):
        result = self._parse(_SAMPLE_ENTRY_XML)
        assert "complex\n" not in result["summary"]
        assert "complex recurrent" in result["summary"]

    def test_parses_dates(self):
        result = self._parse(_SAMPLE_ENTRY_XML)
        assert result["published"] == "2017-06-12T17:57:34Z"
        assert result["updated"] == "2023-08-02T00:52:10Z"

    def test_parses_authors_with_affiliations(self):
        result = self._parse(_SAMPLE_ENTRY_XML)
        assert len(result["authors"]) == 2
        assert result["authors"][0]["name"] == "Ashish Vaswani"
        assert result["authors"][0]["affiliations"] == ["Google Brain"]
        assert result["authors"][1]["name"] == "Noam Shazeer"
        assert result["authors"][1]["affiliations"] == []

    def test_parses_categories(self):
        result = self._parse(_SAMPLE_ENTRY_XML)
        assert result["categories"] == ["cs.CL", "cs.LG"]
        assert result["primary_category"] == "cs.CL"

    def test_parses_links(self):
        result = self._parse(_SAMPLE_ENTRY_XML)
        assert len(result["links"]) == 2
        pdf_links = [l for l in result["links"] if l.get("title") == "pdf"]
        assert len(pdf_links) == 1
        assert "1706.03762v7" in pdf_links[0]["href"]

    def test_parses_comment(self):
        result = self._parse(_SAMPLE_ENTRY_XML)
        assert result["comment"] == "15 pages, 5 figures"

    def test_parses_journal_ref(self):
        result = self._parse(_SAMPLE_ENTRY_XML)
        assert "Neural Information Processing" in result["journal_ref"]

    def test_parses_doi(self):
        result = self._parse(_SAMPLE_ENTRY_XML)
        assert result["doi"] == "10.48550/arXiv.1706.03762"

    def test_missing_optional_fields(self):
        result = self._parse(_MINIMAL_ENTRY_XML)
        assert result["comment"] is None
        assert result["journal_ref"] is None
        assert result["doi"] is None

    def test_single_author_no_affiliation(self):
        result = self._parse(_MINIMAL_ENTRY_XML)
        assert len(result["authors"]) == 1
        assert result["authors"][0]["name"] == "Jane Doe"
        assert result["authors"][0]["affiliations"] == []


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestThrottledGet:
    @pytest.mark.asyncio
    async def test_first_request_no_delay(self, monkeypatch):
        """First request (when _last_request_time is 0) should not sleep."""
        monkeypatch.setattr(arxiv, "_last_request_time", 0.0)
        monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())

        slept = []
        original_sleep = asyncio.sleep

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await arxiv._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 0

    @pytest.mark.asyncio
    async def test_second_request_waits(self, monkeypatch):
        """Second request made immediately should sleep ~3 seconds."""
        monkeypatch.setattr(arxiv, "_last_request_time", time.monotonic())
        monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await arxiv._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 1
        assert slept[0] >= 2.5  # should be close to 3.0

    @pytest.mark.asyncio
    async def test_no_delay_after_gap(self, monkeypatch):
        """No sleep needed when enough time has passed."""
        monkeypatch.setattr(
            arxiv, "_last_request_time", time.monotonic() - 5.0
        )
        monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await arxiv._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 0
