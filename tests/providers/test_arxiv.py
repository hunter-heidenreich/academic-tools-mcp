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


# arXiv answers a malformed search_query with this entry: HTTP 400 now, 200 before.
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


class TestSearchPagingAndSort:
    @pytest.mark.asyncio
    async def test_start_and_sort_reach_the_request(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_capturing_client(monkeypatch, _feed(total="0"))

        await arxiv.search_papers(
            "anything", max_results=5, start=40, sort_by="updated", sort_order="ascending"
        )

        assert seen[0] == {
            "search_query": "anything",
            "start": "40",
            "max_results": "5",
            "sortBy": "lastUpdatedDate",
            "sortOrder": "ascending",
        }

    @pytest.mark.parametrize(
        ("sort_by", "sent"), [("relevance", "relevance"), ("submitted", "submittedDate")]
    )
    @pytest.mark.asyncio
    async def test_each_sort_name_maps_to_the_api_value(self, tmp_path, monkeypatch, sort_by, sent):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_capturing_client(monkeypatch, _feed(total="0"))

        await arxiv.search_papers("anything", sort_by=sort_by)

        assert seen[0]["sortBy"] == sent

    @pytest.mark.asyncio
    async def test_a_negative_start_is_clamped(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_capturing_client(monkeypatch, _feed(total="0"))

        await arxiv.search_papers("anything", start=-5)

        assert seen[0]["start"] == "0"

    @pytest.mark.asyncio
    async def test_a_page_ending_exactly_at_the_window_is_requested(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_capturing_client(monkeypatch, _feed(total="0"))

        result = await arxiv.search_papers(
            "anything", max_results=10, start=arxiv.MAX_SEARCH_WINDOW - 10
        )

        assert "error" not in result
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_one_result_past_the_window_is_refused_without_a_request(
        self, tmp_path, monkeypatch
    ):
        """arXiv answers this with a 500, which the retry path would read as transient."""
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_capturing_client(monkeypatch, _feed(total="0"))

        result = await arxiv.search_papers(
            "anything", max_results=10, start=arxiv.MAX_SEARCH_WINDOW - 9
        )

        assert result["retryable"] is False
        assert result["beyond_window"] is True
        assert seen == []


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

    @pytest.mark.asyncio
    async def test_a_400_carrying_the_entry_is_a_rejection(self, tmp_path, monkeypatch):
        """Regression: since arXiv's cloud migration the rejection arrives as a 400.

        ``raise_for_status`` ran first, so the agent got ``arXiv HTTP 400: <xml…>``
        with no ``retryable`` flag and a suggestion to retry.
        """
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(
            monkeypatch, _feed(_ERROR_ENTRY), status_code=400, raises=_http_status_error(400)
        )

        result = await arxiv.search_papers("ti:(unbalanced")

        assert result == {
            "error": "arXiv rejected the search query: incorrect id format for x",
            "retryable": False,
        }
        assert not list(cache.cache_dir(arxiv.NAMESPACE, "papers").glob("*.json"))

    @pytest.mark.parametrize("body", ["<html>Bad Request</html>", _feed(total="0"), ""])
    @pytest.mark.asyncio
    async def test_a_400_without_the_entry_stays_unclassified(self, tmp_path, monkeypatch, body):
        """Not a rejection and not a parse error: neither flag, so no retry and no rewrite."""
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, body, status_code=400, raises=_http_status_error(400))

        result = await arxiv.search_papers("ti:attention")

        assert result["error"].startswith("arXiv HTTP 400")
        assert "retryable" not in result
        assert "not_found" not in result


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
            # arXiv's current spelling of the same rejection.
            "http_400_error_entry": {
                "text": TestNotFoundShapes._ERROR_FEED,
                "status_code": 400,
                "raises": _http_status_error(400),
            },
        }

    @pytest.mark.parametrize(
        "shape", ["http_404", "empty_feed", "error_entry", "http_400_error_entry"]
    )
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
    async def test_a_transient_failure_on_both_hosts_is_not_negative_cached(
        self, tmp_path, monkeypatch
    ):
        """A transient metadata failure must not be recorded as a download failure,
        even once the direct-URL fallback has failed transiently too."""
        from academic_tools_mcp.net import clients
        from academic_tools_mcp.store import cache
        from tests.helpers.download_fakes import streaming_client

        _reset_throttle(monkeypatch, tmp_path)

        async def fake_get_paper(_id, **_kw):
            return {"error": "arXiv server error (HTTP 503).", "retryable": True}

        monkeypatch.setattr(arxiv, "get_paper", fake_get_paper)
        monkeypatch.setattr(
            clients, "get_client", lambda *a, **kw: streaming_client(503, b"busy", "text/plain")
        )

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


class TestDownloadPdfWithoutMetadata:
    """Regression: every download failed while the export API was rate-limiting.

    ``download_pdf`` resolved its URL only from ``get_paper``, so a 429 from
    ``export.arxiv.org`` failed the download even while ``arxiv.org/pdf/<id>``
    was serving the PDF. A transient metadata failure now falls back to that
    URL; a definitive one still does not.
    """

    _PDF = b"%PDF-1.7 direct bytes"

    @staticmethod
    def _install(monkeypatch, tmp_path, metadata, *, pdf_status=200, pdf_body=_PDF):
        """Stub the metadata GET with ``metadata`` (a response text, or an exception
        to raise) and the PDF host with a real streamed response. Returns the
        metadata call counter and the list of streamed requests."""
        from academic_tools_mcp.net import clients
        from tests.helpers.download_fakes import streaming_client

        _reset_throttle(monkeypatch, tmp_path)
        metadata_calls = [0]
        pdf_requests: list[httpx.Request] = []

        async def fake_throttled_get(url, **_kwargs):
            metadata_calls[0] += 1
            if isinstance(metadata, BaseException):
                raise metadata
            return httpx.Response(200, text=metadata, request=httpx.Request("GET", url))

        monkeypatch.setattr(arxiv, "_throttled_get", fake_throttled_get)
        monkeypatch.setattr(
            clients,
            "get_client",
            lambda *a, **kw: streaming_client(
                pdf_status, pdf_body, "application/pdf", requests=pdf_requests
            ),
        )
        return metadata_calls, pdf_requests

    @pytest.mark.asyncio
    async def test_a_metadata_429_falls_back_to_the_pdf_host(self, tmp_path, monkeypatch):
        metadata_calls, pdf_requests = self._install(monkeypatch, tmp_path, _http_status_error(429))

        result = await arxiv.download_pdf("2301.00001")

        assert "error" not in result, result
        assert result["cached"] is False
        assert metadata_calls[0] == 1
        assert [str(r.url) for r in pdf_requests] == ["https://arxiv.org/pdf/2301.00001"]
        assert arxiv.pdf_path("2301.00001").read_bytes() == self._PDF

        # The file is the positive entry: the next call is a cache hit, no request.
        again = await arxiv.download_pdf("2301.00001")
        assert again["cached"] is True
        assert len(pdf_requests) == 1

    @pytest.mark.asyncio
    async def test_a_metadata_timeout_falls_back_to_the_pdf_host(self, tmp_path, monkeypatch):
        _, pdf_requests = self._install(
            monkeypatch, tmp_path, httpx.ReadTimeout("metadata API timed out")
        )

        result = await arxiv.download_pdf("2301.00001")

        assert "error" not in result, result
        assert [str(r.url) for r in pdf_requests] == ["https://arxiv.org/pdf/2301.00001"]

    @pytest.mark.parametrize(
        ("requested", "path"),
        [
            ("2301.00001v2", "2301.00001v2"),
            ("arXiv:2301.00001v2", "2301.00001v2"),
            # Case survives into the URL; only the cache key is folded.
            ("math.GT/0309136", "math.GT/0309136"),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_fallback_url_keeps_the_version_and_case(
        self, tmp_path, monkeypatch, requested, path
    ):
        _, pdf_requests = self._install(monkeypatch, tmp_path, _http_status_error(503))

        result = await arxiv.download_pdf(requested)

        assert "error" not in result, result
        assert [str(r.url) for r in pdf_requests] == [f"https://arxiv.org/pdf/{path}"]

    @pytest.mark.asyncio
    async def test_a_definitive_not_found_makes_no_fallback_request(self, tmp_path, monkeypatch):
        """How an invalid id presents: 200 with an ``api/errors`` entry."""
        _, pdf_requests = self._install(monkeypatch, tmp_path, _feed(_ERROR_ENTRY))

        result = await arxiv.download_pdf("2301.99999")

        assert result == {"error": "No paper found for arXiv ID: 2301.99999", "not_found": True}
        assert pdf_requests == []

    @pytest.mark.asyncio
    async def test_an_unclassified_metadata_error_makes_no_fallback_request(
        self, tmp_path, monkeypatch
    ):
        """Only ``retryable: True`` means a retry might work; a bare 400 carries neither flag."""
        _, pdf_requests = self._install(monkeypatch, tmp_path, _http_status_error(400))

        result = await arxiv.download_pdf("2301.00001")

        assert "error" in result
        assert result.get("retryable") is not True
        assert pdf_requests == []

    @pytest.mark.asyncio
    async def test_an_id_outside_the_grammar_is_never_put_in_a_pdf_url(self, tmp_path, monkeypatch):
        _, pdf_requests = self._install(monkeypatch, tmp_path, _http_status_error(429))

        result = await arxiv.download_pdf("../../2301.00001")

        assert result["retryable"] is True
        assert pdf_requests == []

    @pytest.mark.asyncio
    async def test_a_fallback_404_is_negative_cached(self, tmp_path, monkeypatch):
        from academic_tools_mcp.store import cache

        _, pdf_requests = self._install(
            monkeypatch, tmp_path, _http_status_error(429), pdf_status=404, pdf_body=b""
        )

        result = await arxiv.download_pdf("2301.00001")

        assert result["retryable"] is False
        canonical = arxiv.canonical_arxiv_id("2301.00001")
        assert cache.get_negative(arxiv.NAMESPACE, "downloads", canonical) == result
        assert len(pdf_requests) == 1

    @pytest.mark.asyncio
    async def test_a_metadata_success_uses_the_entry_pdf_link(self, tmp_path, monkeypatch):
        """The metadata-first path is unchanged: the entry's link, not the constructed URL."""
        entry = _search_entry("2301.00001v3").replace(
            "  </entry>",
            '    <link title="pdf" href="https://arxiv.org/pdf/2301.00001v3" rel="related"/>\n'
            "  </entry>",
        )
        metadata_calls, pdf_requests = self._install(monkeypatch, tmp_path, _feed(entry))

        result = await arxiv.download_pdf("2301.00001")

        assert "error" not in result, result
        assert metadata_calls[0] == 1
        assert [str(r.url) for r in pdf_requests] == ["https://arxiv.org/pdf/2301.00001v3"]


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


# ---------------------------------------------------------------------------
# get_papers_batch: one id_list request for many ids
# ---------------------------------------------------------------------------


def _stub_scripted_client(monkeypatch, *responses):
    """Install a stub client answering each GET with the next ``(status, text)``.

    Returns the list of params each GET sent. A 4xx/5xx raises from
    ``raise_for_status`` as a real response would.
    """
    from academic_tools_mcp.net import clients

    seen: list[dict] = []
    queue = list(responses)

    class StubResponse:
        def __init__(self, status_code, text):
            self.status_code = status_code
            self.text = text
            self.content = text.encode()
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise _http_status_error(self.status_code)

    class StubClient:
        async def get(self, url, **kwargs):
            seen.append(kwargs.get("params") or {"url": url})
            return StubResponse(*queue.pop(0))

    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())
    return seen


class TestGetPapersBatch:
    @pytest.mark.asyncio
    async def test_many_ids_cost_one_request(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        ids = [f"2301.{n:05d}" for n in range(20)]
        # Unordered, as arXiv answers: attribution is by id, not by position.
        feed = _feed(*(_search_entry(f"{i}v1") for i in reversed(ids)), total="20")
        seen = _stub_scripted_client(monkeypatch, (200, feed))

        out = await arxiv.get_papers_batch(ids)

        assert len(seen) == 1
        assert seen[0] == {"id_list": ",".join(ids), "max_results": "20"}
        assert list(out) == ids
        assert all(arxiv.id_from_entry(out[i]) == f"{i}v1" for i in ids)

    @pytest.mark.asyncio
    async def test_a_hit_serves_a_later_singleton_without_a_request(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_scripted_client(
            monkeypatch, (200, _feed(_search_entry("2301.00001v2"), total="1"))
        )

        await arxiv.get_papers_batch(["2301.00001"])
        paper = await arxiv.get_paper("2301.00001")

        assert arxiv.id_from_entry(paper) == "2301.00001v2"
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_cached_ids_are_not_requested(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_scripted_client(
            monkeypatch,
            (200, _feed(_search_entry("2301.00001v1"), total="1")),
            (200, _feed(_search_entry("2301.00002v1"), total="1")),
        )

        await arxiv.get_papers_batch(["2301.00001"])
        out = await arxiv.get_papers_batch(["2301.00001", "2301.00002"])

        assert seen[1]["id_list"] == "2301.00002"
        assert set(out) == {"2301.00001", "2301.00002"}

    @pytest.mark.asyncio
    async def test_force_refresh_requests_cached_ids_again(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_scripted_client(
            monkeypatch,
            (200, _feed(_search_entry("2301.00001v1"), total="1")),
            (200, _feed(_search_entry("2301.00001v2"), total="1")),
        )

        await arxiv.get_papers_batch(["2301.00001"])
        out = await arxiv.get_papers_batch(["2301.00001"], force_refresh=True)

        assert len(seen) == 2
        assert arxiv.id_from_entry(out["2301.00001"]) == "2301.00001v2"

    @pytest.mark.asyncio
    async def test_spellings_of_one_id_share_one_slot(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_scripted_client(
            monkeypatch, (200, _feed(_search_entry("hep-th/9901001v3"), total="1"))
        )

        out = await arxiv.get_papers_batch(["HEP-TH/9901001", "arXiv:hep-th/9901001"])

        assert seen[0]["id_list"] == "hep-th/9901001"
        assert list(out) == ["hep-th/9901001"]

    @pytest.mark.asyncio
    async def test_a_bare_id_gets_the_newest_version_beside_an_older_request(
        self, tmp_path, monkeypatch
    ):
        """arXiv returns both revisions; the older one must not answer "current"."""
        _reset_throttle(monkeypatch, tmp_path)
        feed = _feed(
            _search_entry("1706.03762v1", "v1"), _search_entry("1706.03762v7", "v7"), total="2"
        )
        _stub_scripted_client(monkeypatch, (200, feed))

        out = await arxiv.get_papers_batch(["1706.03762", "1706.03762v1"])

        assert out["1706.03762"]["title"] == "v7"
        assert out["1706.03762v1"]["title"] == "v1"

    @pytest.mark.asyncio
    async def test_an_omitted_id_is_negative_cached_when_the_feed_accounts_for_itself(
        self, tmp_path, monkeypatch
    ):
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        _stub_scripted_client(monkeypatch, (200, _feed(_search_entry("2301.00001v1"), total="1")))

        out = await arxiv.get_papers_batch(["2301.00001", "2301.99999"])

        expected = {"error": "No paper found for arXiv ID: 2301.99999", "not_found": True}
        assert out["2301.99999"] == expected
        assert cache.get_negative(arxiv.NAMESPACE, "papers", "2301.99999") == expected

    @pytest.mark.parametrize(
        "feed",
        [
            # totalResults says more matched than the page carries.
            _feed(_search_entry("2301.00001v1"), total="2"),
            # A record we did not ask for.
            _feed(_search_entry("2301.00001v1"), _search_entry("2399.00001v1"), total="2"),
        ],
    )
    @pytest.mark.asyncio
    async def test_an_omitted_id_is_retryable_when_the_feed_does_not(
        self, tmp_path, monkeypatch, feed
    ):
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        _stub_scripted_client(monkeypatch, (200, feed))

        out = await arxiv.get_papers_batch(["2301.00001", "2301.99999"])

        assert out["2301.99999"]["retryable"] is True
        assert "not_found" not in out["2301.99999"]
        assert cache.get_negative(arxiv.NAMESPACE, "papers", "2301.99999") is None

    @pytest.mark.asyncio
    async def test_a_rejected_chunk_falls_back_to_singletons(self, tmp_path, monkeypatch):
        """One id arXiv won't accept fails the whole id_list; only that id is not-found."""
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_scripted_client(
            monkeypatch,
            (400, _feed(_ERROR_ENTRY)),
            (200, _feed(_search_entry("2301.00001v1"), total="1")),
            (400, _feed(_ERROR_ENTRY)),
        )

        out = await arxiv.get_papers_batch(["2301.00001", "2301.00002"])

        assert [s.get("id_list") for s in seen] == [
            "2301.00001,2301.00002",
            "2301.00001",
            "2301.00002",
        ]
        assert arxiv.id_from_entry(out["2301.00001"]) == "2301.00001v1"
        assert out["2301.00002"]["not_found"] is True

    @pytest.mark.asyncio
    async def test_a_500_carrying_the_error_entry_falls_back_to_singletons(
        self, tmp_path, monkeypatch
    ):
        """Regression: one record that always 500s (hep-th/9901001v1) failed its whole chunk."""
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        monkeypatch.setattr(arxiv._throttle, "retry_attempts", 1)
        seen = _stub_scripted_client(
            monkeypatch,
            (500, _feed(_ERROR_ENTRY)),
            (500, _feed(_ERROR_ENTRY)),
            (200, _feed(_search_entry("2301.00001v1"), total="1")),
        )

        out = await arxiv.get_papers_batch(["hep-th/9901001v1", "2301.00001"])

        assert [s.get("id_list") for s in seen] == [
            "hep-th/9901001v1,2301.00001",
            "hep-th/9901001v1",
            "2301.00001",
        ]
        assert arxiv.id_from_entry(out["2301.00001"]) == "2301.00001v1"
        # Still arXiv's failure, not a claim of absence.
        assert out["hep-th/9901001v1"]["retryable"] is True
        assert cache.get_negative(arxiv.NAMESPACE, "papers", "hep-th/9901001v1") is None

    @pytest.mark.asyncio
    async def test_a_single_id_500_is_not_refetched(self, tmp_path, monkeypatch):
        """Falling back for a lone id would repeat the same request."""
        _reset_throttle(monkeypatch, tmp_path)
        monkeypatch.setattr(arxiv._throttle, "retry_attempts", 1)
        seen = _stub_scripted_client(monkeypatch, (500, _feed(_ERROR_ENTRY)))

        out = await arxiv.get_papers_batch(["hep-th/9901001v1"])

        assert len(seen) == 1
        assert out["hep-th/9901001v1"]["retryable"] is True

    @pytest.mark.asyncio
    async def test_a_transient_failure_contaminates_the_chunk_uncached(self, tmp_path, monkeypatch):
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        monkeypatch.setattr(arxiv._throttle, "retry_attempts", 1)
        _stub_scripted_client(monkeypatch, (503, ""))

        out = await arxiv.get_papers_batch(["2301.00001", "2301.00002"])

        assert all(out[c]["retryable"] is True for c in out)
        # Fresh dicts: mutating one caller's error must not corrupt the other.
        assert out["2301.00001"] is not out["2301.00002"]
        assert cache.get_negative(arxiv.NAMESPACE, "papers", "2301.00001") is None

    @pytest.mark.asyncio
    async def test_misses_are_chunked(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        monkeypatch.setattr(arxiv, "_BATCH_CHUNK_SIZE", 2)
        seen = _stub_scripted_client(
            monkeypatch,
            (200, _feed(_search_entry("2301.00001v1"), _search_entry("2301.00002v1"), total="2")),
            (200, _feed(_search_entry("2301.00003v1"), total="1")),
        )

        out = await arxiv.get_papers_batch(["2301.00001", "2301.00002", "2301.00003"])

        assert [s["id_list"] for s in seen] == ["2301.00001,2301.00002", "2301.00003"]
        assert all("error" not in p for p in out.values())


# ---------------------------------------------------------------------------
# get_versions: license and revision history from OAI-PMH arXivRaw
# ---------------------------------------------------------------------------


def _oai_record(arxiv_id="1706.03762", *, license_url=None, versions=2):
    """An OAI-PMH GetRecord body in arXivRaw form, as oaipmh.arxiv.org serves it."""
    version_xml = "".join(
        f'<version version="v{n}"><date>Mon, 12 Jun 2017 17:5{n}:34 GMT</date>'
        f"<size>11{n}kb</size><source_type>D</source_type></version>"
        for n in range(1, versions + 1)
    )
    license_xml = f"<license>{license_url}</license>" if license_url else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        "<GetRecord><record><header><identifier>oai:arXiv.org:"
        f"{arxiv_id}</identifier></header><metadata>"
        '<arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">'
        f"<id>{arxiv_id}</id><submitter>Llion Jones</submitter>{version_xml}"
        f"<title>Attention Is All You Need</title>{license_xml}"
        "</arXivRaw></metadata></record></GetRecord></OAI-PMH>"
    )


def _oai_error(code):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
        f"<error code='{code}'>The value of the identifier argument is unknown.</error>"
        "</OAI-PMH>"
    )


class TestGetVersions:
    _NONEXCLUSIVE = "http://arxiv.org/licenses/nonexclusive-distrib/1.0/"

    @pytest.mark.asyncio
    async def test_the_record_parses_license_submitter_and_versions(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, _oai_record(license_url=self._NONEXCLUSIVE))

        result = await arxiv.get_versions("1706.03762")

        assert result == {
            "arxiv_id": "1706.03762",
            "submitter": "Llion Jones",
            "license": self._NONEXCLUSIVE,
            "versions": [
                {"version": "v1", "date": "2017-06-12T17:51:34Z", "size": "111kb"},
                {"version": "v2", "date": "2017-06-12T17:52:34Z", "size": "112kb"},
            ],
        }

    @pytest.mark.asyncio
    async def test_a_record_without_a_license_reads_as_none(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, _oai_record("hep-th/9901001"))

        result = await arxiv.get_versions("hep-th/9901001")

        assert result["license"] is None
        assert len(result["versions"]) == 2

    @pytest.mark.asyncio
    async def test_every_revision_shares_one_bare_request_and_entry(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_capturing_client(monkeypatch, _oai_record())

        await arxiv.get_versions("arXiv:1706.03762v3")
        await arxiv.get_versions("1706.03762")

        assert len(seen) == 1
        assert seen[0] == {
            "verb": "GetRecord",
            "identifier": "oai:arXiv.org:1706.03762",
            "metadataPrefix": "arXivRaw",
        }

    @pytest.mark.asyncio
    async def test_the_positive_entry_lives_a_day_not_fourteen(self, tmp_path, monkeypatch):
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, _oai_record())
        ttls: list[float] = []
        real = cache.cached_lookup

        async def spy(**kwargs):
            ttls.append(kwargs["positive_ttl"])
            return await real(**kwargs)

        monkeypatch.setattr(cache, "cached_lookup", spy)

        await arxiv.get_versions("1706.03762")

        assert ttls == [arxiv._VERSIONS_TTL_SECONDS]
        assert arxiv._VERSIONS_TTL_SECONDS < arxiv._POSITIVE_TTL_SECONDS

    @pytest.mark.asyncio
    async def test_id_does_not_exist_is_negative_cached(self, tmp_path, monkeypatch):
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        calls = _stub_text_response(monkeypatch, _oai_error("idDoesNotExist"))

        result = await arxiv.get_versions("2301.99999")

        assert result == {"error": "No paper found for arXiv ID: 2301.99999", "not_found": True}
        assert cache.get_negative(arxiv.NAMESPACE, "versions", "2301.99999") == result
        assert await arxiv.get_versions("2301.99999") == result
        assert calls[0] == 1

    @pytest.mark.asyncio
    async def test_another_oai_error_is_definitive_but_uncached(self, tmp_path, monkeypatch):
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, _oai_error("badArgument"))

        result = await arxiv.get_versions("2301.00001")

        assert result["retryable"] is False
        assert "badArgument" in result["error"]
        assert cache.get_negative(arxiv.NAMESPACE, "versions", "2301.00001") is None

    @pytest.mark.parametrize(
        "body",
        [
            "<OAI-PMH><GetRec",
            '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><GetRecord/></OAI-PMH>',
        ],
    )
    @pytest.mark.asyncio
    async def test_a_garbled_or_wrong_shape_body_is_transient(self, tmp_path, monkeypatch, body):
        _reset_throttle(monkeypatch, tmp_path)
        _stub_text_response(monkeypatch, body)

        result = await arxiv.get_versions("2301.00001")

        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_retryable(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        monkeypatch.setattr(arxiv._throttle, "retry_attempts", 1)
        _stub_text_response(monkeypatch, "", status_code=503, raises=_http_status_error(503))

        assert (await arxiv.get_versions("2301.00001"))["retryable"] is True

    @pytest.mark.asyncio
    async def test_an_id_outside_the_grammar_makes_no_request(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        calls = _stub_text_response(monkeypatch, _oai_record())

        result = await arxiv.get_versions("../../2301.00001")

        assert result["not_found"] is True
        assert calls[0] == 0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Wed, 02 Aug 2023 00:41:18 GMT", "2023-08-02T00:41:18Z"),
            ("not a date", None),
            ("", None),
            (None, None),
        ],
    )
    def test_dates_convert_or_degrade_to_none(self, raw, expected):
        assert arxiv._iso_utc(raw) == expected


# ---------------------------------------------------------------------------
# get_html: arXiv's LaTeXML rendering
# ---------------------------------------------------------------------------

_RENDERING = '<html><body><article class="ltx_document"><h1>T</h1></article></body></html>'


class TestGetHtml:
    @pytest.mark.asyncio
    async def test_a_rendering_is_returned_from_the_versioned_url(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_scripted_client(monkeypatch, (200, _RENDERING))

        result = await arxiv.get_html("arXiv:1706.03762v7")

        assert result == {"html": _RENDERING}
        assert seen == [{"url": "https://arxiv.org/html/1706.03762v7"}]

    @pytest.mark.asyncio
    async def test_a_404_is_negative_cached(self, tmp_path, monkeypatch):
        """arXiv's "No HTML for …" page: non-LaTeX source or a failed conversion."""
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_scripted_client(monkeypatch, (404, "<html>No HTML for '2401.00005'</html>"))

        result = await arxiv.get_html("2401.00005")

        expected = {"error": "No HTML rendering for arXiv ID: 2401.00005", "not_found": True}
        assert result == expected
        assert cache.get_negative(arxiv.NAMESPACE, "html", "2401.00005") == expected
        assert await arxiv.get_html("2401.00005") == expected
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_force_refresh_asks_again_after_a_404(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_scripted_client(monkeypatch, (404, ""), (200, _RENDERING))

        await arxiv.get_html("2401.00005")
        result = await arxiv.get_html("2401.00005", force_refresh=True)

        assert result == {"html": _RENDERING}
        assert len(seen) == 2

    @pytest.mark.asyncio
    async def test_a_200_that_is_not_a_rendering_is_transient_and_uncached(
        self, tmp_path, monkeypatch
    ):
        from academic_tools_mcp.store import cache

        _reset_throttle(monkeypatch, tmp_path)
        _stub_scripted_client(monkeypatch, (200, "<html>Service maintenance</html>"))

        result = await arxiv.get_html("1706.03762")

        assert result["retryable"] is True
        assert cache.get_negative(arxiv.NAMESPACE, "html", "1706.03762") is None

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_retryable(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        monkeypatch.setattr(arxiv._throttle, "retry_attempts", 1)
        _stub_scripted_client(monkeypatch, (503, ""))

        assert (await arxiv.get_html("1706.03762"))["retryable"] is True

    @pytest.mark.asyncio
    async def test_a_body_past_the_byte_cap_is_refused(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        monkeypatch.setenv("MAX_PDF_BYTES", "10")
        _stub_scripted_client(monkeypatch, (200, _RENDERING))

        result = await arxiv.get_html("1706.03762")

        assert result["retryable"] is False
        assert result["max_bytes"] == 10

    @pytest.mark.asyncio
    async def test_an_id_outside_the_grammar_makes_no_request(self, tmp_path, monkeypatch):
        _reset_throttle(monkeypatch, tmp_path)
        seen = _stub_scripted_client(monkeypatch)

        result = await arxiv.get_html("../../2301.00001")

        assert result["not_found"] is True
        assert seen == []
