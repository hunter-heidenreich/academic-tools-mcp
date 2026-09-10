import asyncio
import time
import xml.etree.ElementTree as ET
from typing import ClassVar

import httpx
import pytest

from academic_tools_mcp.providers import arxiv

# ---------------------------------------------------------------------------
# ID normalization
# ---------------------------------------------------------------------------


class TestNormalizeArxivId:
    def test_bare_new_style(self):
        assert arxiv.normalize_arxiv_id("2301.00001") == "2301.00001"

    def test_bare_new_style_with_version(self):
        assert arxiv.normalize_arxiv_id("2301.00001v2") == "2301.00001v2"

    def test_bare_old_style(self):
        assert arxiv.normalize_arxiv_id("hep-th/9901001") == "hep-th/9901001"

    def test_bare_old_style_with_version(self):
        assert arxiv.normalize_arxiv_id("hep-th/9901001v1") == "hep-th/9901001v1"

    def test_abs_url(self):
        assert arxiv.normalize_arxiv_id("https://arxiv.org/abs/2301.00001") == "2301.00001"

    def test_abs_url_with_version(self):
        assert arxiv.normalize_arxiv_id("https://arxiv.org/abs/2301.00001v2") == "2301.00001v2"

    def test_pdf_url_with_extension(self):
        assert arxiv.normalize_arxiv_id("https://arxiv.org/pdf/2301.00001.pdf") == "2301.00001"

    def test_pdf_url_without_extension(self):
        assert arxiv.normalize_arxiv_id("https://arxiv.org/pdf/2301.00001v2") == "2301.00001v2"

    def test_old_style_abs_url(self):
        assert arxiv.normalize_arxiv_id("https://arxiv.org/abs/hep-th/9901001") == "hep-th/9901001"

    def test_html_url(self):
        """arXiv's own rendering — the default landing page, so the spelling a
        pasted browser tab carries. Unrecognised, it files the paper a second
        time under ``manual``."""
        assert arxiv.normalize_arxiv_id("https://arxiv.org/html/2301.00001v2") == "2301.00001v2"

    def test_old_style_html_url(self):
        assert arxiv.normalize_arxiv_id("https://arxiv.org/html/hep-th/9901001") == "hep-th/9901001"

    def test_html_url_with_fragment(self):
        assert arxiv.normalize_arxiv_id("https://arxiv.org/html/2301.00001#S3") == "2301.00001"

    def test_strips_whitespace(self):
        assert arxiv.normalize_arxiv_id("  2301.00001  ") == "2301.00001"

    def test_http_url(self):
        assert arxiv.normalize_arxiv_id("http://arxiv.org/abs/2301.00001") == "2301.00001"

    def test_abs_url_with_query_string(self):
        assert (
            arxiv.normalize_arxiv_id("https://arxiv.org/abs/2301.00001?context=cs") == "2301.00001"
        )

    def test_abs_url_with_fragment(self):
        assert arxiv.normalize_arxiv_id("https://arxiv.org/abs/2301.00001#abstract") == "2301.00001"

    def test_pdf_url_with_extension_and_query(self):
        assert (
            arxiv.normalize_arxiv_id("https://arxiv.org/pdf/2301.00001v2.pdf?download=1")
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

        monkeypatch.setattr(arxiv.clients, "get_client", fake_get_client)
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
# longer per-provider code — they live in ``throttle.Throttle`` and are
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
        from academic_tools_mcp.net import clients
        from academic_tools_mcp.store import cache, singleflight

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(arxiv, "_single_flight", singleflight.SingleFlight())

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
            headers: ClassVar[dict[str, str]] = {}

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

        monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())

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
        from academic_tools_mcp.net import clients
        from academic_tools_mcp.store import cache, singleflight

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(arxiv, "_single_flight", singleflight.SingleFlight())

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
            headers: ClassVar[dict[str, str]] = {}

            def raise_for_status(self):
                pass

        class StubClient:
            async def get(self, url, **kwargs):
                nonlocal get_calls
                get_calls += 1
                return StubResponse()

        monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())

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
        from academic_tools_mcp.net import clients
        from academic_tools_mcp.store import cache, singleflight

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(arxiv, "_single_flight", singleflight.SingleFlight())

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
            headers: ClassVar[dict[str, str]] = {}

            def raise_for_status(self):
                pass

        class StubClient:
            async def get(self, url, **kwargs):
                nonlocal get_calls
                get_calls += 1
                return StubResponse()

        monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())

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
        from academic_tools_mcp.net import clients
        from academic_tools_mcp.store import cache, singleflight

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
        monkeypatch.setattr(arxiv, "_single_flight", singleflight.SingleFlight())

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
                        "headers": {},
                        "raise_for_status": lambda self: None,
                    },
                )()

        monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())

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

    The conftest autouse fixture already resets each provider's ``throttle``
    (pending / last-start map / lock / sem) and ``_single_flight`` between
    tests; here we additionally zero the inter-start gap so a multi-request test
    doesn't wait out arxiv's 3 s pacing.
    """
    from academic_tools_mcp.store import cache, singleflight

    monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(arxiv._throttle, "min_gap_seconds", 0.0)
    monkeypatch.setattr(arxiv, "_single_flight", singleflight.SingleFlight())


def _stub_text_response(monkeypatch, text, *, status_code=200, raises=None):
    """Install a stub client returning ``text`` with a configurable
    ``raise_for_status``. Returns a 1-element list whose [0] counts GETs."""
    from academic_tools_mcp.net import clients

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

    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())
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
        from academic_tools_mcp.store import cache

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
        from academic_tools_mcp.store import cache

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
        from academic_tools_mcp.store import cache

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

        from academic_tools_mcp.store import cache

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
        from academic_tools_mcp.net import clients

        requested: list[str] = []
        outer = self

        class StubResponse:
            def __init__(self, text):
                self.text = text
                self.status_code = 200
                self.headers: dict[str, str] = {}

            def raise_for_status(self):
                pass

        class StubClient:
            async def get(self, url, **kwargs):
                ident = kwargs.get("params", {}).get("id_list", "")
                requested.append(ident)
                version = "v2" if ident.endswith("v2") else "v1"
                title = "Version Two Title" if version == "v2" else "Version One Title"
                return StubResponse(outer._feed(version, title))

        monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())
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


class TestArxivPrefixNormalization:
    """``arXiv:2301.00001`` is the form arXiv's own "Cite as" box prints.

    Left unstripped it was neither an arXiv shape (so the router sent the
    paper to the ``manual`` namespace) nor a valid ``id_list`` value, and the
    canonical key carried the prefix — a second cache entry for one paper.
    """

    @pytest.mark.parametrize(
        ("spelling", "expected"),
        [
            ("arXiv:2301.00001", "2301.00001"),
            ("arxiv:2301.00001", "2301.00001"),
            ("ARXIV:2301.00001", "2301.00001"),
            ("arXiv: 2301.00001", "2301.00001"),
            ("  arXiv:2301.00001v2  ", "2301.00001v2"),
            ("arXiv:hep-th/9901001", "hep-th/9901001"),
            # Prefix stripped before the URL handling, as `doinorm.normalize`
            # does for `doi:` — both nested forms occur in the wild.
            ("arXiv:https://arxiv.org/abs/2301.00001", "2301.00001"),
            ("arXiv:arXiv:2301.00001", "2301.00001"),
        ],
    )
    def test_the_prefix_is_stripped(self, spelling, expected):
        assert arxiv.normalize_arxiv_id(spelling) == expected
        assert arxiv.canonical_arxiv_id(spelling) == expected.lower()

    def test_normalization_is_idempotent(self):
        for spelling in ("arXiv:2301.00001", "arXiv:arXiv:2301.00001", "2301.00001", "notes"):
            once = arxiv.normalize_arxiv_id(spelling)
            assert arxiv.normalize_arxiv_id(once) == once

    def test_a_string_that_merely_contains_arxiv_is_untouched(self):
        assert arxiv.normalize_arxiv_id("my-arxiv:notes") == "my-arxiv:notes"
        assert arxiv.normalize_arxiv_id("arxivorama") == "arxivorama"


# ---------------------------------------------------------------------------
# search_papers: cap, error entry, totalResults, warming
# ---------------------------------------------------------------------------


def _feed(*entries: str, total: str = "1") -> str:
    """An Atom search feed wrapping *entries*, with a totalResults of *total*."""
    body = "".join(entries)
    return (
        '<?xml version="1.0"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom">\n'
        "  <opensearch:totalResults"
        ' xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"{total}</opensearch:totalResults>\n{body}</feed>"
    )


def _search_entry(arxiv_id: str, title: str = "A Paper") -> str:
    return (
        f"  <entry>\n    <id>http://arxiv.org/abs/{arxiv_id}</id>\n"
        f"    <title>{title}</title>\n    <summary>An abstract.</summary>\n"
        "    <published>2023-01-01T00:00:00Z</published>\n"
        "    <updated>2023-01-01T00:00:00Z</updated>\n"
        "    <author><name>Jane Doe</name></author>\n  </entry>\n"
    )


# arXiv answers a malformed search_query with HTTP 200 and this entry.
_ERROR_ENTRY = (
    "  <entry>\n"
    "    <id>http://arxiv.org/api/errors#incorrect_id_format_for_x</id>\n"
    "    <title>Error</title>\n"
    "    <summary>incorrect id format for x</summary>\n"
    "    <published>2023-01-01T00:00:00Z</published>\n"
    "    <updated>2023-01-01T00:00:00Z</updated>\n"
    "    <author><name>arXiv api core</name></author>\n"
    "  </entry>\n"
)


def _stub_capturing_client(monkeypatch, text):
    """Install a stub client returning ``text`` and recording each GET's params."""
    from academic_tools_mcp.net import clients

    seen: list[dict] = []

    class StubResponse:
        def __init__(self):
            self.text = text
            self.status_code = 200
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            pass

    class StubClient:
        async def get(self, url, **kwargs):
            seen.append(kwargs.get("params") or {})
            return StubResponse()

    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())
    return seen


class TestSearchMaxResults:
    """The cap is a boundary: exactly at it must pass, one past it must clamp."""

    @pytest.mark.parametrize(
        ("requested", "sent"),
        [
            (-5, "1"),
            (0, "1"),
            (1, "1"),
            (10, "10"),
            (arxiv.MAX_SEARCH_RESULTS, str(arxiv.MAX_SEARCH_RESULTS)),
            (arxiv.MAX_SEARCH_RESULTS + 1, str(arxiv.MAX_SEARCH_RESULTS)),
            (10_000, str(arxiv.MAX_SEARCH_RESULTS)),
        ],
    )
    @pytest.mark.asyncio
    async def test_max_results_is_clamped_to_the_cap(self, tmp_path, monkeypatch, requested, sent):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_capturing_client(monkeypatch, _feed(total="0"))

        await arxiv.search_papers("anything", max_results=requested)

        assert seen[0]["max_results"] == sent

    @pytest.mark.asyncio
    async def test_the_tool_bound_is_the_provider_constant(self):
        """`search_arxiv`'s validation bound is arxiv's constant, not a copy of it."""
        from academic_tools_mcp.tools import search as search_tools

        field = search_tools.search_arxiv.__annotations__["max_results"].__metadata__[0]
        assert [m.le for m in field.metadata if hasattr(m, "le")] == [arxiv.MAX_SEARCH_RESULTS]
        assert str(arxiv.MAX_SEARCH_RESULTS) in field.description


class TestSearchErrorEntry:
    @pytest.mark.asyncio
    async def test_arxiv_error_entry_is_not_returned_as_a_hit(self, tmp_path, monkeypatch):
        """Regression: a rejected query surfaced as a result whose id was an errors URL.

        `get_paper` has always classified this shape; `search_papers` parsed it
        as a normal entry, so the agent got `{arxiv_id: ".../api/errors#...",
        title: "Error"}` and chained the next tool call onto garbage.
        """
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, _feed(_ERROR_ENTRY))

        result = await arxiv.search_papers("ti:(unbalanced")

        assert "entries" not in result
        assert "incorrect id format" in result["error"]
        # The query is what's wrong; retrying it verbatim cannot help.
        assert result["retryable"] is False

    @pytest.mark.asyncio
    async def test_the_error_entry_never_warms_the_cache(self, tmp_path, monkeypatch):
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, _feed(_ERROR_ENTRY))

        await arxiv.search_papers("ti:(unbalanced")

        assert not list(cache.cache_dir(arxiv.NAMESPACE, "papers").glob("*.json"))


class TestSearchTotalResults:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1", 1),
            ("0", 0),
            ("12345", 12345),
            ("  7  ", 7),
            ("", 0),
            ("many", 0),
            ("-3", 0),
            ("1.5", 0),
            # `isdigit` accepts a superscript that `int()` rejects; `isdecimal`
            # does not, which is what keeps a ValueError out of the response.
            ("²", 0),
        ],
    )
    @pytest.mark.asyncio
    async def test_total_results_degrades_to_zero_rather_than_raising(
        self, tmp_path, monkeypatch, raw, expected
    ):
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, _feed(_search_entry("2301.00001v1"), total=raw))

        result = await arxiv.search_papers("anything")

        assert result["total_results"] == expected

    @pytest.mark.asyncio
    async def test_a_missing_total_results_element_is_zero(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(
            monkeypatch,
            '<?xml version="1.0"?>\n<feed xmlns="http://www.w3.org/2005/Atom">'
            f"{_search_entry('2301.00001v1')}</feed>",
        )

        result = await arxiv.search_papers("anything")

        assert result["total_results"] == 0
        assert len(result["entries"]) == 1


class TestSearchFailurePaths:
    @pytest.mark.asyncio
    async def test_http_error_returns_the_structured_contract(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, "", status_code=503, raises=_http_status_error(503))

        result = await arxiv.search_papers("anything")

        assert "entries" not in result
        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_entity_expansion_payload_is_refused(self, tmp_path, monkeypatch):
        """defusedxml refuses the bomb; search classifies it as get_paper does."""
        _reset_throttle(monkeypatch, tmp_path)
        bomb = (
            '<?xml version="1.0"?>\n'
            "<!DOCTYPE feed [\n"
            '  <!ENTITY a "aaaaaaaaaa">\n'
            '  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n'
            "]>\n"
            '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>&b;</title></entry></feed>'
        )
        _stub_text_response(monkeypatch, bomb)

        result = await arxiv.search_papers("anything")

        assert "entries" not in result
        assert result["retryable"] is True


class TestSearchWarmsBothKeys:
    @pytest.mark.asyncio
    async def test_both_the_versioned_and_the_bare_key_are_warmed(self, tmp_path, monkeypatch):
        """A search hit is the current version, so it answers both lookups.

        Warming only the versioned key leaves every bare `get_paper` a miss —
        and the bare form is what an agent pastes.
        """
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, _feed(_search_entry("2301.00001v7", "Fresh")))

        await arxiv.search_papers("anything")

        for key in ("2301.00001v7", "2301.00001"):
            entry = cache.get(arxiv.NAMESPACE, "papers", key)
            assert entry is not None, key
            assert entry["title"] == "Fresh"

    @pytest.mark.asyncio
    async def test_a_within_ttl_entry_is_never_clobbered(self, tmp_path, monkeypatch):
        """The probe is TTL-aware, not a presence test — but a live entry wins."""
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        cache.put(arxiv.NAMESPACE, "papers", "2301.00001v7", {"title": "Live", "id": "kept"})
        _stub_text_response(monkeypatch, _feed(_search_entry("2301.00001v7", "Fresh")))

        await arxiv.search_papers("anything")

        assert cache.get(arxiv.NAMESPACE, "papers", "2301.00001v7")["title"] == "Live"
        # The bare key had no entry, so it is warmed from the search hit.
        assert cache.get(arxiv.NAMESPACE, "papers", "2301.00001")["title"] == "Fresh"

    @pytest.mark.asyncio
    async def test_a_hit_whose_id_is_not_arxiv_shaped_is_skipped(self, tmp_path, monkeypatch):
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(
            monkeypatch,
            _feed("  <entry><id>http://example.com/nonsense</id><title>X</title></entry>\n"),
        )

        result = await arxiv.search_papers("anything")

        assert len(result["entries"]) == 1
        assert not list(cache.cache_dir(arxiv.NAMESPACE, "papers").glob("*.json"))


# ---------------------------------------------------------------------------
# The three definitive "not found" shapes, and the two the tests missed
# ---------------------------------------------------------------------------


class TestNotFoundShapes:
    """arXiv spells "definitively absent" three ways; all three cache one payload.

    Regression: the 404 branch classified from the raised ``HTTPStatusError``,
    so what landed in the negative cache for the full TTL was
    ``http.error_dict``'s fallthrough — ``arXiv HTTP 404: <body snippet>`` —
    where the other two branches cached a clean message.
    """

    _EMPTY_FEED = _feed(total="0")
    _ERROR_FEED = _feed(_ERROR_ENTRY)

    @staticmethod
    def _shapes():
        return {
            # A *valid* feed body, so only the status check can classify it:
            # a parse failure or an empty feed would pass this test for the
            # wrong reason.
            "http_404": {"text": _feed(_search_entry("2301.99999v1")), "status_code": 404},
            "empty_feed": {"text": TestNotFoundShapes._EMPTY_FEED},
            "error_entry": {"text": TestNotFoundShapes._ERROR_FEED},
        }

    @pytest.mark.parametrize("shape", ["http_404", "empty_feed", "error_entry"])
    @pytest.mark.asyncio
    async def test_each_shape_is_negative_cached_with_one_payload(
        self, tmp_path, monkeypatch, shape
    ):
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        calls = _stub_text_response(monkeypatch, **self._shapes()[shape])

        result = await arxiv.get_paper("2301.99999")

        assert result == {
            "error": "No paper found for arXiv ID: 2301.99999",
            "not_found": True,
        }, shape
        # `not_found` is the flag openaccess and wikipedia read as
        # "definitively absent"; a body snippet is not one.
        assert "404" not in result["error"]

        canonical = arxiv.canonical_arxiv_id("2301.99999")
        assert cache.get_negative(arxiv.NAMESPACE, "papers", canonical) == result

        assert await arxiv.get_paper("2301.99999") == result
        assert calls[0] == 1, "second call must be served from the negative cache"

    @pytest.mark.asyncio
    async def test_a_404_is_checked_before_raise_for_status(self, tmp_path, monkeypatch):
        """The status check, not the exception, is what classifies it.

        A stub whose ``raise_for_status`` is a no-op still yields the clean
        not-found payload — which it could not if the branch lived in
        ``except http.HTTPX_ERRORS``.
        """
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(
            monkeypatch, _feed(_search_entry("2301.99999v1")), status_code=404, raises=None
        )

        assert (await arxiv.get_paper("2301.99999"))["not_found"] is True


# ---------------------------------------------------------------------------
# download_pdf: the branches the stubs were hiding
# ---------------------------------------------------------------------------


class TestDownloadPdfMetadataBranches:
    @pytest.mark.asyncio
    async def test_a_paper_with_no_pdf_link_is_a_definitive_failure(self, tmp_path, monkeypatch):
        """How a withdrawn paper presents: an Atom entry carrying no pdf link.

        ``retryable: False`` is load-bearing — ``streaming.is_definitive_failure``
        reads exactly that key to decide whether to negative-cache.
        """
        from academic_tools_mcp.download import streaming
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)

        async def fake_get_paper(_id, **_kw):
            return {"id": "http://arxiv.org/abs/2301.00001v1", "links": []}

        monkeypatch.setattr(arxiv, "get_paper", fake_get_paper)

        result = await arxiv.download_pdf("2301.00001")

        assert result["retryable"] is False
        assert "2301.00001" in result["error"]
        assert streaming.is_definitive_failure(result)

        canonical = arxiv.canonical_arxiv_id("2301.00001")
        assert cache.get_negative(arxiv.NAMESPACE, "downloads", canonical) is not None

    @pytest.mark.asyncio
    async def test_a_metadata_error_is_returned_untouched(self, tmp_path, monkeypatch):
        """A transient metadata failure must not be recorded as a download failure."""
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)

        async def fake_get_paper(_id, **_kw):
            return {"error": "arXiv server error (HTTP 503).", "retryable": True}

        monkeypatch.setattr(arxiv, "get_paper", fake_get_paper)

        result = await arxiv.download_pdf("2301.00001")

        assert result["retryable"] is True
        canonical = arxiv.canonical_arxiv_id("2301.00001")
        assert cache.get_negative(arxiv.NAMESPACE, "downloads", canonical) is None

    @pytest.mark.parametrize("force", [False, True])
    @pytest.mark.asyncio
    async def test_force_refresh_reaches_the_metadata_lookup(self, tmp_path, monkeypatch, force):
        """A forced re-download must not resolve its URL from the record it replaces.

        Mutate `download_pdf`'s `force_refresh=force_refresh` away and this is
        the assertion that fails; every other download test swallows `**_kw`.
        """
        from tests.helpers.download_fakes import install_stream, mock_stream_response
        from tests.helpers.download_fakes import passthrough_slot as _passthrough_slot

        _reset_throttle(monkeypatch, tmp_path)
        seen: list[bool] = []

        async def fake_get_paper(_id, *, force_refresh=False):
            seen.append(force_refresh)
            return {"links": [{"title": "pdf", "href": "http://example.com/x.pdf"}]}

        monkeypatch.setattr(arxiv, "get_paper", fake_get_paper)
        monkeypatch.setattr(arxiv, "_request_slot", _passthrough_slot)
        install_stream(monkeypatch, mock_stream_response())

        result = await arxiv.download_pdf("2301.00001", force_refresh=force)

        assert "error" not in result
        assert seen == [force]


# ---------------------------------------------------------------------------
# _parse_entry: degenerate elements a real feed can carry
# ---------------------------------------------------------------------------


class TestParseEntryDegenerate:
    @staticmethod
    def _parse(inner: str) -> dict:
        xml = (
            '<entry xmlns="http://www.w3.org/2005/Atom"'
            ' xmlns:arxiv="http://arxiv.org/schemas/atom">'
            f"{inner}</entry>"
        )
        return arxiv._parse_entry(ET.fromstring(xml))

    def test_an_author_without_a_name_becomes_an_empty_string(self):
        parsed = self._parse("<author><arxiv:affiliation>MIT</arxiv:affiliation></author>")
        assert parsed["authors"] == [{"name": "", "affiliations": ["MIT"]}]

    def test_an_empty_affiliation_is_dropped(self):
        parsed = self._parse("<author><name>Jane Doe</name><arxiv:affiliation/></author>")
        assert parsed["authors"] == [{"name": "Jane Doe", "affiliations": []}]

    def test_a_link_without_href_or_rel_keeps_the_shape(self):
        parsed = self._parse('<link title="pdf"/>')
        assert parsed["links"] == [{"href": "", "rel": "", "title": "pdf"}]

    def test_a_category_without_a_term_is_dropped(self):
        parsed = self._parse('<category scheme="x"/><category term="cs.AI"/>')
        assert parsed["categories"] == ["cs.AI"]

    def test_a_missing_id_and_primary_category_are_empty_strings(self):
        parsed = self._parse("<title>T</title>")
        assert parsed["id"] == ""
        assert parsed["primary_category"] == ""

    def test_an_empty_entry_parses_to_the_full_shape(self):
        """Every key is present whatever the feed omits — the tool layer slices it."""
        parsed = self._parse("")
        assert parsed == {
            "id": "",
            "title": "",
            "summary": "",
            "published": "",
            "updated": "",
            "authors": [],
            "categories": [],
            "primary_category": "",
            "links": [],
            "comment": None,
            "journal_ref": None,
            "doi": None,
        }


# ---------------------------------------------------------------------------
# Spellings that used to route to `manual` under a key already arXiv's
# ---------------------------------------------------------------------------


class TestSpellingsThatMissedTheRouter:
    """Regression: each of these normalized to itself, so `is_arxiv_id` said no.

    A rejected spelling does not merely fail to fetch — it is not an arXiv
    *shape*, so `manual.resolve_target` files the paper under `manual` and the
    same paper caches, downloads and converts a second time.
    """

    @pytest.mark.parametrize(
        "spelling",
        [
            "https://www.arxiv.org/abs/2301.00001",
            "http://export.arxiv.org/abs/2301.00001",
            "arxiv.org/abs/2301.00001",
            "HTTPS://ARXIV.ORG/abs/2301.00001",
            "https://arxiv.org/abs/2301.00001/",
            "https://ARXIV.org/pdf/2301.00001.PDF",
            "10.48550/arXiv.2301.00001",
            "10.48550/arxiv.2301.00001",
            "https://doi.org/10.48550/arXiv.2301.00001",
            "doi:10.48550/arXiv.2301.00001",
            "arXiv:10.48550/arXiv.2301.00001",
        ],
    )
    def test_it_normalizes_to_the_bare_id(self, spelling):
        from academic_tools_mcp import manual

        assert arxiv.canonical_arxiv_id(spelling) == "2301.00001"
        assert arxiv.is_arxiv_id(spelling)
        assert manual.resolve_target(spelling)["namespace"] == arxiv.NAMESPACE

    def test_an_old_style_id_survives_the_doi_form(self):
        assert arxiv.normalize_arxiv_id("10.48550/arXiv.hep-th/9901001") == "hep-th/9901001"
        assert arxiv.canonical_arxiv_id("10.48550/arXiv.math.GT/0309136") == "math.gt/0309136"

    @pytest.mark.parametrize(
        "identifier",
        [
            # The prefix names arXiv's DOI registrant; it does not promise that
            # what follows is a paper id. Stripping it unconditionally would
            # mangle the record *and* cost normalize its idempotence.
            "10.48550/arXiv.not-a-paper",
            "10.48550/dryad.abc123",
            "10.1101/2020.01.01.123456",
            "https://example.com/abs/2301.00001",
            "notarxiv.org/abs/2301.00001",
            "my-arxiv:notes",
        ],
    )
    def test_a_look_alike_is_left_alone(self, identifier):
        assert not arxiv.is_arxiv_id(identifier)
        assert arxiv.normalize_arxiv_id(identifier) == identifier

    def test_the_doi_prefix_is_exported_for_bibtex(self):
        """One spelling of the prefix, as `acl.ACL_DOI_PREFIX` is."""
        from academic_tools_mcp import bibtex

        assert arxiv.ARXIV_DOI_PREFIX == "10.48550/arXiv."
        assert bibtex._arxiv_eprint_from_doi("10.48550/arXiv.1706.03762v2") == "1706.03762"
        # Case survives: BibTeX's eprint field keeps the archive class.
        assert bibtex._arxiv_eprint_from_doi("10.48550/arXiv.math.GT/0309136") == "math.GT/0309136"
        assert bibtex._arxiv_eprint_from_doi("10.1234/other") == ""
