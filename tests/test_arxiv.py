import asyncio
import time
import xml.etree.ElementTree as ET

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
    """The version is part of a paper's identity, so it is part of the key.

    Stripping it (the previous behaviour) meant the key and the *fetch*
    disagreed: whichever version was requested first won the shared key, and
    every later version was served that one's metadata and PDF bytes.
    """

    def test_keeps_version(self):
        assert arxiv.canonical_arxiv_id("2301.00001v2") == "2301.00001v2"

    def test_no_version(self):
        assert arxiv.canonical_arxiv_id("2301.00001") == "2301.00001"

    def test_versions_do_not_collide(self):
        v1 = arxiv.canonical_arxiv_id("2301.00001v1")
        v2 = arxiv.canonical_arxiv_id("2301.00001v2")
        bare = arxiv.canonical_arxiv_id("2301.00001")
        assert len({v1, v2, bare}) == 3

    def test_lowercases(self):
        assert arxiv.canonical_arxiv_id("hep-TH/9901001") == "hep-th/9901001"

    def test_url_keeps_version_and_lowercases(self):
        assert arxiv.canonical_arxiv_id("https://arxiv.org/abs/2301.00001v3") == "2301.00001v3"

    def test_old_style_keeps_version(self):
        assert arxiv.canonical_arxiv_id("hep-th/9901001v1") == "hep-th/9901001v1"


class TestBaseArxivId:
    """``base_arxiv_id`` is the version-stripped "latest" form."""

    def test_strips_version(self):
        assert arxiv.base_arxiv_id("2301.00001v2") == "2301.00001"

    def test_no_version_is_identity(self):
        assert arxiv.base_arxiv_id("2301.00001") == "2301.00001"

    def test_all_versions_share_one_base(self):
        assert (
            arxiv.base_arxiv_id("2301.00001v1")
            == arxiv.base_arxiv_id("2301.00001v9")
            == arxiv.base_arxiv_id("2301.00001")
        )

    def test_old_style_strips_version(self):
        assert arxiv.base_arxiv_id("hep-th/9901001v1") == "hep-th/9901001"


# ---------------------------------------------------------------------------
# User-Agent / client headers
# ---------------------------------------------------------------------------


class TestUserAgent:
    def test_descriptive_ua_sent_without_mailto(self, monkeypatch):
        # arXiv throttles generic library User-Agents harder, so a descriptive
        # UA is sent even when no contact email is configured.
        monkeypatch.delenv("ARXIV_MAILTO", raising=False)
        headers = arxiv._build_headers()
        ua = headers["User-Agent"]
        assert ua.startswith("academic-tools-mcp/")
        assert "python-httpx" not in ua
        assert "mailto:" not in ua

    def test_mailto_appended_when_configured(self, monkeypatch):
        monkeypatch.setenv("ARXIV_MAILTO", "ops@example.com")
        ua = arxiv._build_headers()["User-Agent"]
        assert "mailto:ops@example.com" in ua

    def test_get_client_bakes_in_headers(self, monkeypatch):
        monkeypatch.setenv("ARXIV_MAILTO", "ops@example.com")
        captured: dict = {}

        def fake_get_client(name, **kwargs):
            captured["name"] = name
            captured["kwargs"] = kwargs
            return object()

        monkeypatch.setattr(arxiv._clients, "get_client", fake_get_client)
        arxiv._get_client()

        assert captured["name"] == arxiv.NAMESPACE
        assert "mailto:ops@example.com" in captured["kwargs"]["headers"]["User-Agent"]


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
# Single-flight on get_paper
# ---------------------------------------------------------------------------
#
# The rate-limiter gap, concurrency cap, and burst-cap backpressure are no
# longer per-provider code — they live in ``_throttle.Throttle`` and are
# covered once in tests/test_throttle.py.


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
        monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(arxiv, "_single_flight", _singleflight.SingleFlight())

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
        monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(arxiv, "_single_flight", _singleflight.SingleFlight())

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
        monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(arxiv, "_single_flight", _singleflight.SingleFlight())

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
        monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(arxiv, "_single_flight", _singleflight.SingleFlight())

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
    """Point the cache at tmp_path and disable the throttle gap (no real sleeps).

    The conftest autouse fixture already resets each provider's ``_throttle``
    (pending / last-start map / lock / sem) and ``_single_flight`` between
    tests; here we additionally zero the inter-start gap so a multi-request test
    doesn't wait out arxiv's 3 s pacing.
    """
    from academic_tools_mcp import _singleflight, cache

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
    monkeypatch.setattr(arxiv, "_single_flight", _singleflight.SingleFlight())


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
        canonical = arxiv.canonical_arxiv_id("2301.00001")
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

        canonical = arxiv.canonical_arxiv_id("2301.00001")
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

        canonical = arxiv.canonical_arxiv_id("2301.00001")
        assert cache.get_negative(arxiv.NAMESPACE, "papers", canonical) is None


class TestSearchOpportunisticCache:
    @pytest.mark.asyncio
    async def test_refreshes_stale_entry(self, tmp_path, monkeypatch):
        # search_papers opportunistically warms the per-paper cache. A
        # stale entry (past the positive TTL) must be refreshed by newer
        # search data, not skipped.
        import os

        from academic_tools_mcp import cache

        _reset_throttle(monkeypatch, tmp_path)

        canonical = arxiv.canonical_arxiv_id("2301.00001")
        cache.put(arxiv.NAMESPACE, "papers", canonical, {"title": "Stale Title", "id": "old"})

        # Age the cached entry well past the positive TTL.
        path = cache.cache_dir(arxiv.NAMESPACE, "papers") / f"{cache._cache_key(canonical)}.json"
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


class TestVersionedIdentity:
    """Regression: an explicitly-versioned request must never be answered
    from a different version's cache entry.

    The key used to be version-stripped while the fetch kept the version, so
    whichever version was asked for first won the shared key. Every later
    version was a silent cache hit returning the first one's metadata — and
    ``download_pdf`` handed back the first one's bytes as ``cached: True``.
    """

    @staticmethod
    def _feed(version: str, title: str) -> str:
        return f"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001{version}</id>
    <title>{title}</title>
    <summary>Summary for {version}.</summary>
    <published>2023-01-01T00:00:00Z</published>
    <updated>2023-06-01T00:00:00Z</updated>
    <author><name>Jane Doe</name></author>
  </entry>
</feed>"""

    def _install(self, monkeypatch):
        """Serve a version-specific feed based on the requested id."""
        from academic_tools_mcp import _clients

        requested: list[str] = []
        outer = self

        class StubResponse:
            def __init__(self, text):
                self.text = text
                self.status_code = 200

            def raise_for_status(self):
                pass

        class StubClient:
            async def get(self, url, **kwargs):
                ident = kwargs.get("params", {}).get("id_list", "")
                requested.append(ident)
                version = "v2" if ident.endswith("v2") else "v1"
                title = "Version Two Title" if version == "v2" else "Version One Title"
                return StubResponse(outer._feed(version, title))

        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())
        monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
        return requested

    @pytest.mark.asyncio
    async def test_v2_after_v1_is_not_served_v1(self, monkeypatch):
        requested = self._install(monkeypatch)

        v1 = await arxiv.get_paper("2301.00001v1")
        v2 = await arxiv.get_paper("2301.00001v2")

        assert v1["title"] == "Version One Title"
        assert v2["title"] == "Version Two Title", (
            "v2 was served v1's cached record — the version-stripped key is back"
        )
        assert requested == ["2301.00001v1", "2301.00001v2"]

    @pytest.mark.asyncio
    async def test_same_version_twice_still_hits_cache(self, monkeypatch):
        requested = self._install(monkeypatch)

        first = await arxiv.get_paper("2301.00001v2")
        second = await arxiv.get_paper("2301.00001v2")

        assert first["title"] == second["title"] == "Version Two Title"
        assert len(requested) == 1, "a repeat of the same version must not re-fetch"

    @pytest.mark.asyncio
    async def test_pdf_paths_differ_per_version(self):
        from academic_tools_mcp import manual

        v1 = manual.resolve_target("2301.00001v1")["pdf_path"]
        v2 = manual.resolve_target("2301.00001v2")["pdf_path"]
        bare = manual.resolve_target("2301.00001")["pdf_path"]
        assert len({str(v1), str(v2), str(bare)}) == 3
