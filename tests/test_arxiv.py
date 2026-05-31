import asyncio
import time
import xml.etree.ElementTree as ET
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from academic_tools_mcp.providers import arxiv

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

    def test_abs_url_with_query_string(self):
        assert (
            arxiv._normalize_arxiv_id("https://arxiv.org/abs/2301.00001?context=cs") == "2301.00001"
        )

    def test_abs_url_with_fragment(self):
        assert (
            arxiv._normalize_arxiv_id("https://arxiv.org/abs/2301.00001#abstract") == "2301.00001"
        )

    def test_pdf_url_with_extension_and_query(self):
        assert (
            arxiv._normalize_arxiv_id("https://arxiv.org/pdf/2301.00001v2.pdf?download=1")
            == "2301.00001v2"
        )


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
        pdf_links = [link for link in result["links"] if link.get("title") == "pdf"]
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
        monkeypatch.setattr(arxiv, "_last_request_time", time.monotonic() - 5.0)
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


# ---------------------------------------------------------------------------
# Burst-cap backpressure
# ---------------------------------------------------------------------------


class TestThrottleBackpressure:
    """The throttle refuses to stack more than ``_MAX_PENDING`` callers
    behind itself. Past that, the next caller raises
    ``LocalBackpressureError`` so the agent gets fast feedback rather
    than quietly queueing for tens of seconds.
    """

    @pytest.mark.asyncio
    async def test_overflow_raises_local_backpressure(self, monkeypatch):
        # Force the gauge to the cap so the next call trips it.
        monkeypatch.setattr(arxiv, "_pending", arxiv._MAX_PENDING)
        monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())

        mock_client = MagicMock()
        # An overflow must NOT issue a network request; if it did, the
        # AsyncMock would record the call.
        mock_client.get = AsyncMock(
            side_effect=AssertionError("throttle should refuse before issuing a request")
        )

        with pytest.raises(arxiv._http.LocalBackpressureError) as ei:
            await arxiv._throttled_get(mock_client, "http://example.com")

        exc = ei.value
        assert exc.provider == "arXiv"
        assert exc.pending == arxiv._MAX_PENDING
        assert exc.max_pending == arxiv._MAX_PENDING

        # The gauge must not have been bumped by an overflow call;
        # otherwise legitimate callers further down the line would
        # trip the cap when they shouldn't.
        assert arxiv._pending == arxiv._MAX_PENDING

    @pytest.mark.asyncio
    async def test_pending_resets_when_request_succeeds(self, monkeypatch):
        # One happy-path call should bump the gauge and drop it back.
        monkeypatch.setattr(arxiv, "_pending", 0)
        monkeypatch.setattr(arxiv, "_last_request_time", 0.0)
        monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())

        async def mock_sleep(_):
            pass

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=MagicMock())

        await arxiv._throttled_get(mock_client, "http://example.com")
        assert arxiv._pending == 0

    @pytest.mark.asyncio
    async def test_pending_resets_when_request_raises(self, monkeypatch):
        # An upstream failure mid-request must still drop the gauge —
        # otherwise a single transient error would permanently lower
        # our effective burst budget.
        monkeypatch.setattr(arxiv, "_pending", 0)
        monkeypatch.setattr(arxiv, "_last_request_time", 0.0)
        monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())

        async def mock_sleep(_):
            pass

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await arxiv._throttled_get(mock_client, "http://example.com")
        assert arxiv._pending == 0


# ---------------------------------------------------------------------------
# Single-flight on get_paper
# ---------------------------------------------------------------------------


class TestGetPaperSingleFlight:
    """Concurrent get_paper(id) calls for the same canonical ID must
    collapse into one outbound HTTP fetch. Without this, four parallel
    unified-paper tools (metadata / authors / abstract / bibtex) for
    the same arXiv ID would each fetch the same paper and collectively
    burn ~12s of throttle gap.
    """

    @pytest.mark.asyncio
    async def test_concurrent_same_id_collapses_to_one_fetch(self, tmp_path, monkeypatch):
        from academic_tools_mcp import _clients, _singleflight, cache

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
        monkeypatch.setattr(arxiv, "_pending", 0)
        monkeypatch.setattr(arxiv, "_last_request_time", 0.0)
        monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())
        monkeypatch.setattr(arxiv, "_single_flight", _singleflight.SingleFlight())

        async def mock_sleep(_):
            pass

        monkeypatch.setattr(arxiv.asyncio, "sleep", mock_sleep)

        atom_xml = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>Test Title</title>
    <summary>Test summary.</summary>
    <published>2023-01-01T00:00:00Z</published>
    <updated>2023-01-01T00:00:00Z</updated>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>"""

        get_calls = 0

        class StubResponse:
            text = atom_xml
            status_code = 200

            def raise_for_status(self):
                pass

        class StubClient:
            async def get(self, url, **kwargs):
                nonlocal get_calls
                get_calls += 1
                # Yield so the other 4 callers pile up behind the
                # single-flight slot before this leader resolves.
                await asyncio.sleep(0)
                return StubResponse()

        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())

        results = await asyncio.gather(*[arxiv.get_paper("2301.00001") for _ in range(5)])

        assert get_calls == 1, (
            f"single-flight should have coalesced 5 calls into 1 fetch, got {get_calls}"
        )
        assert all(r["title"] == "Test Title" for r in results)
        assert all(r["authors"][0]["name"] == "Jane Doe" for r in results)

    @pytest.mark.asyncio
    async def test_404_is_negative_cached_no_second_fetch(self, tmp_path, monkeypatch):
        # arXiv returns 200 with an "api/errors" entry for invalid IDs.
        # That's a definitive "not found" — the second call for the
        # same bad ID must NOT hit the network. Without negative
        # caching, an agent that retries on error would re-fetch on
        # every attempt and burn through the throttle budget.
        from academic_tools_mcp import _clients, _singleflight, cache

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
        monkeypatch.setattr(arxiv, "_pending", 0)
        monkeypatch.setattr(arxiv, "_last_request_time", 0.0)
        monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())
        monkeypatch.setattr(arxiv, "_single_flight", _singleflight.SingleFlight())

        async def mock_sleep(_):
            pass

        monkeypatch.setattr(arxiv.asyncio, "sleep", mock_sleep)

        not_found_atom = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format</id>
    <title>Error</title>
    <summary>incorrect id format</summary>
    <published>2023-01-01T00:00:00Z</published>
    <updated>2023-01-01T00:00:00Z</updated>
  </entry>
</feed>"""

        get_calls = 0

        class StubResponse:
            text = not_found_atom
            status_code = 200

            def raise_for_status(self):
                pass

        class StubClient:
            async def get(self, url, **kwargs):
                nonlocal get_calls
                get_calls += 1
                return StubResponse()

        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())

        # First call: hits the network, gets the not-found, caches it.
        result1 = await arxiv.get_paper("bogus-id")
        assert "error" in result1
        assert "No paper found" in result1["error"]
        assert get_calls == 1

        # Second call: served from negative cache, no network.
        result2 = await arxiv.get_paper("bogus-id")
        assert result2 == result1, (
            "negative cache must return the same error payload as the "
            "original not-found, byte-for-byte"
        )
        assert "_expires_at" not in result2, "negative cache bookkeeping must not leak to the agent"
        assert get_calls == 1, (
            f"second call should be served from negative cache, got {get_calls} network calls"
        )

        # Different bad ID — separate entry, must hit the network.
        await arxiv.get_paper("another-bogus-id")
        assert get_calls == 2

    @pytest.mark.asyncio
    async def test_force_refresh_drops_cache_and_refetches(self, tmp_path, monkeypatch):
        """force_refresh must invalidate both positive and negative
        entries before fetching, so an agent can re-pull a paper whose
        cached record might be stale (e.g. a new version uploaded)."""
        from academic_tools_mcp import _clients, _singleflight, cache

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
        monkeypatch.setattr(arxiv, "_pending", 0)
        monkeypatch.setattr(arxiv, "_last_request_time", 0.0)
        monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())
        monkeypatch.setattr(arxiv, "_single_flight", _singleflight.SingleFlight())

        async def mock_sleep(_):
            pass

        monkeypatch.setattr(arxiv.asyncio, "sleep", mock_sleep)

        atom_xml = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>Test Title</title>
    <summary>Test summary.</summary>
    <published>2023-01-01T00:00:00Z</published>
    <updated>2023-01-01T00:00:00Z</updated>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>"""

        get_calls = 0

        class StubResponse:
            text = atom_xml
            status_code = 200

            def raise_for_status(self):
                pass

        class StubClient:
            async def get(self, url, **kwargs):
                nonlocal get_calls
                get_calls += 1
                return StubResponse()

        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())

        # Warm the cache.
        await arxiv.get_paper("2301.00001")
        assert get_calls == 1

        # Default behaviour: cache hit, no second network call.
        await arxiv.get_paper("2301.00001")
        assert get_calls == 1

        # force_refresh: cache is dropped, network is hit again.
        await arxiv.get_paper("2301.00001", force_refresh=True)
        assert get_calls == 2

        # Negative cache also dropped: a previously-404'd identifier
        # can resolve on a forced retry.
        cache.put_negative(
            arxiv.NAMESPACE,
            "papers",
            "2301.99999",
            {"error": "stale 404"},
            ttl_seconds=86400,
        )
        await arxiv.get_paper("2301.99999", force_refresh=True)
        assert get_calls == 3, "force_refresh should drop the negative cache and re-fetch"

    @pytest.mark.asyncio
    async def test_different_ids_dont_block_each_other(self, tmp_path, monkeypatch):
        # Different canonical IDs must NOT share a single-flight slot.
        # Otherwise unrelated papers would serialise on each other,
        # which defeats the point.
        from academic_tools_mcp import _clients, _singleflight, cache

        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
        monkeypatch.setattr(arxiv, "_pending", 0)
        monkeypatch.setattr(arxiv, "_last_request_time", 0.0)
        monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())
        monkeypatch.setattr(arxiv, "_single_flight", _singleflight.SingleFlight())

        async def mock_sleep(_):
            pass

        monkeypatch.setattr(arxiv.asyncio, "sleep", mock_sleep)

        get_calls = 0

        def _atom(arxiv_id):
            return f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/{arxiv_id}v1</id>
    <title>Title {arxiv_id}</title>
    <summary>Summary.</summary>
    <published>2023-01-01T00:00:00Z</published>
    <updated>2023-01-01T00:00:00Z</updated>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>"""

        class StubClient:
            async def get(self, url, **kwargs):
                nonlocal get_calls
                get_calls += 1
                aid = kwargs["params"]["id_list"]
                await asyncio.sleep(0)
                return type(
                    "R",
                    (),
                    {
                        "text": _atom(aid),
                        "status_code": 200,
                        "raise_for_status": lambda self: None,
                    },
                )()

        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())

        results = await asyncio.gather(
            arxiv.get_paper("2301.00001"),
            arxiv.get_paper("2302.00002"),
        )

        assert get_calls == 2, f"two different IDs should hit the network twice, got {get_calls}"
        titles = sorted(r["title"] for r in results)
        assert titles == ["Title 2301.00001", "Title 2302.00002"]


# ---------------------------------------------------------------------------
# Malformed / hostile XML and HTTP-error handling
# ---------------------------------------------------------------------------


def _reset_throttle(monkeypatch, tmp_path):
    """Reset arxiv pooled state + cache root and no-op the throttle sleep."""
    from academic_tools_mcp import _singleflight, cache

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(arxiv, "_pending", 0)
    monkeypatch.setattr(arxiv, "_last_request_time", 0.0)
    monkeypatch.setattr(arxiv, "_request_lock", asyncio.Lock())
    monkeypatch.setattr(arxiv, "_single_flight", _singleflight.SingleFlight())

    async def mock_sleep(_):
        pass

    monkeypatch.setattr(arxiv.asyncio, "sleep", mock_sleep)


def _stub_text_response(monkeypatch, text, *, status_code=200, raises=None):
    """Install a stub client returning ``text`` with a configurable
    ``raise_for_status``. Returns a 1-element list whose [0] counts GETs."""
    from academic_tools_mcp import _clients

    calls = [0]

    class StubResponse:
        def __init__(self):
            self.text = text
            self.status_code = status_code
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            if raises is not None:
                raise raises

    class StubClient:
        async def get(self, url, **kwargs):
            calls[0] += 1
            return StubResponse()

    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())
    return calls


def _http_status_error(status):
    request = httpx.Request("GET", arxiv.ARXIV_BASE_URL)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class TestMalformedXml:
    @pytest.mark.asyncio
    async def test_get_paper_malformed_xml_returns_error(self, tmp_path, monkeypatch):
        # A 200 with a truncated body (flaky connection) must NOT crash —
        # it should surface the uniform {error, retryable} contract.
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, "<feed><entry></fe")

        result = await arxiv.get_paper("2301.00001")

        assert isinstance(result, dict)
        assert "error" in result
        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_malformed_xml_not_negative_cached(self, tmp_path, monkeypatch):
        # A parse failure is transient (garbled body), not "not found":
        # it must NOT be negative-cached, so a retry re-fetches.
        from academic_tools_mcp import cache

        _reset_throttle(monkeypatch, tmp_path)
        calls = _stub_text_response(monkeypatch, "<feed><entry></fe")

        await arxiv.get_paper("2301.00001")
        canonical = arxiv._canonical_arxiv_id("2301.00001")
        assert cache.get_negative(arxiv.NAMESPACE, "papers", canonical) is None

        await arxiv.get_paper("2301.00001")
        assert calls[0] == 2, "transient parse failure must not be cached; retry re-fetches"

    @pytest.mark.asyncio
    async def test_search_papers_malformed_xml_returns_error(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, "<feed><entry></fe")

        result = await arxiv.search_papers("attention is all you need")

        assert isinstance(result, dict)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_get_paper_xml_bomb_rejected(self, tmp_path, monkeypatch):
        # Entity-expansion ("billion laughs") payload. The entity sits in
        # the title of an otherwise-valid entry: a parser that *expands*
        # entities would return a valid paper (no error), so this test
        # only passes once the parser FORBIDS internal entities and we
        # surface a structured error instead.
        _reset_throttle(monkeypatch, tmp_path)
        bomb = """<?xml version="1.0"?>
<!DOCTYPE feed [
  <!ENTITY a "aaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
]>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>&c;</title>
    <summary>Summary.</summary>
    <published>2023-01-01T00:00:00Z</published>
    <updated>2023-01-01T00:00:00Z</updated>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>"""
        _stub_text_response(monkeypatch, bomb)

        result = await arxiv.get_paper("2301.00001")

        assert isinstance(result, dict)
        assert "error" in result


class TestHttpErrorCaching:
    @pytest.mark.asyncio
    async def test_http_404_negative_cached(self, tmp_path, monkeypatch):
        # A genuine HTTP 404 is definitive "not found" — negative-cache it
        # so a retrying agent doesn't re-hit the network every call.
        from academic_tools_mcp import cache

        _reset_throttle(monkeypatch, tmp_path)
        calls = _stub_text_response(
            monkeypatch, "", status_code=404, raises=_http_status_error(404)
        )

        result1 = await arxiv.get_paper("2301.00001")
        assert "error" in result1
        assert calls[0] == 1

        canonical = arxiv._canonical_arxiv_id("2301.00001")
        assert cache.get_negative(arxiv.NAMESPACE, "papers", canonical) is not None

        result2 = await arxiv.get_paper("2301.00001")
        assert "error" in result2
        assert calls[0] == 1, "second call must be served from the negative cache"

    @pytest.mark.asyncio
    async def test_transient_5xx_not_negative_cached(self, tmp_path, monkeypatch):
        # A 503 is transient — it must NOT be negative-cached, or a brief
        # outage would poison the cache for the whole negative TTL.
        from academic_tools_mcp import cache

        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, "", status_code=503, raises=_http_status_error(503))

        result = await arxiv.get_paper("2301.00001")
        assert "error" in result

        canonical = arxiv._canonical_arxiv_id("2301.00001")
        assert cache.get_negative(arxiv.NAMESPACE, "papers", canonical) is None


class TestSearchOpportunisticCache:
    @pytest.mark.asyncio
    async def test_refreshes_stale_entry(self, tmp_path, monkeypatch):
        # search_papers opportunistically warms the per-paper cache. A
        # stale entry (past the positive TTL) must be refreshed by newer
        # search data, not skipped.
        import os
        import time

        from academic_tools_mcp import cache

        _reset_throttle(monkeypatch, tmp_path)

        canonical = arxiv._canonical_arxiv_id("2301.00001")
        cache.put(arxiv.NAMESPACE, "papers", canonical, {"title": "Stale Title", "id": "old"})

        # Age the cached entry well past the positive TTL.
        path = cache._cache_dir(arxiv.NAMESPACE, "papers") / f"{cache._cache_key(canonical)}.json"
        old = time.time() - (arxiv._POSITIVE_TTL_SECONDS + 86400)
        os.utime(path, (old, old))

        search_xml = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <opensearch:totalResults xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">1</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2301.00001v2</id>
    <title>Fresh Title</title>
    <summary>Fresh summary.</summary>
    <published>2023-01-01T00:00:00Z</published>
    <updated>2023-02-01T00:00:00Z</updated>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>"""
        _stub_text_response(monkeypatch, search_xml)

        await arxiv.search_papers("anything")

        refreshed = cache.get(arxiv.NAMESPACE, "papers", canonical)
        assert refreshed is not None
        assert refreshed["title"] == "Fresh Title"
