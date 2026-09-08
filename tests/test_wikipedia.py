import json
from typing import Any

import httpx
import pytest

from academic_tools_mcp import _clients, cache
from academic_tools_mcp.providers import wikipedia

# ---------------------------------------------------------------------------
# Transport fakes
#
# A real MockTransport, not a mock client: httpx's own URL handling is half of
# what these assert, and a hand-rolled fake would never reproduce it.
# ---------------------------------------------------------------------------

_BAD_JSON = object()


def _stub(monkeypatch: pytest.MonkeyPatch, *payloads: Any) -> list[httpx.Request]:
    """Serve ``payloads`` from successive GETs, returning the captured requests.

    A payload may be ``_BAD_JSON``, a ``(status, payload)`` pair, or an
    exception to raise; the last one repeats once the sequence runs out.
    """
    requests: list[httpx.Request] = []
    seq = list(payloads)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = seq[len(requests)] if len(requests) < len(seq) else seq[-1]
        requests.append(request)
        if isinstance(payload, Exception):
            raise payload
        status = 200
        if isinstance(payload, tuple):
            status, payload = payload
        body = b"{ truncated" if payload is _BAD_JSON else json.dumps(payload).encode()
        return httpx.Response(status, headers={"content-type": "application/json"}, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)
    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: client)
    return requests


def _stub_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a transport that fails the test if anything reaches it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(wikipedia._throttle, "min_gap_seconds", 0.0)
    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: client)


_SUMMARY_PAYLOAD = {
    "type": "standard",
    "title": "Cytochrome P450",
    "description": "Class of enzymes",
    "extract": "Cytochromes P450 are a superfamily of enzymes.",
    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Cytochrome_P450"}},
    "pageid": 709137,
}


# ---------------------------------------------------------------------------
# canonical_title — the single home for a title's spellings
# ---------------------------------------------------------------------------


class TestCanonicalTitle:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Cytochrome P450", "Cytochrome_P450"),
            ("Cytochrome_P450", "Cytochrome_P450"),
            ("  Cytochrome P450  ", "Cytochrome_P450"),
            ("Cytochrome  P450", "Cytochrome_P450"),  # a run of spaces is one underscore
            ("Cytochrome_ P450", "Cytochrome_P450"),  # ...mixed with underscores too
            ("Cytochrome\tP450", "Cytochrome_P450"),  # any whitespace, as MediaWiki does
            ("photosynthesis", "Photosynthesis"),  # only the leading letter is folded
            ("AC/DC", "AC/DC"),  # a slash is part of the title, untouched here
            ("ßeta", "ßeta"),  # "ß".upper() is "SS" — a two-character expansion
            ("", ""),
            ("   ", ""),
            ("___", ""),
        ],
    )
    def test_collapses_one_articles_spellings(self, title, expected):
        assert wikipedia.canonical_title(title) == expected

    def test_case_distinct_titles_stay_distinct(self):
        """'PET' (the scan) and 'Pet' (the animal) are different articles.

        Folding the whole title maps both to one key and serves one summary
        for the other; Wikipedia auto-capitalizes the leading letter only.
        """
        assert wikipedia.canonical_title("PET") != wikipedia.canonical_title("Pet")
        assert wikipedia.canonical_title("pet") == wikipedia.canonical_title("Pet")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_parses_opensearch_response(self, monkeypatch):
        """Should parse the 4-element OpenSearch array correctly."""
        _stub(
            monkeypatch,
            [
                "test query",
                ["Article One", "Article Two"],
                ["", ""],
                [
                    "https://en.wikipedia.org/wiki/Article_One",
                    "https://en.wikipedia.org/wiki/Article_Two",
                ],
            ],
        )

        results = (await wikipedia.search("test query", limit=5))["results"]

        assert results == [
            {"title": "Article One", "url": "https://en.wikipedia.org/wiki/Article_One"},
            {"title": "Article Two", "url": "https://en.wikipedia.org/wiki/Article_Two"},
        ]

    @pytest.mark.asyncio
    async def test_empty_results(self, monkeypatch):
        """A well-formed response with no hits is an empty list, not an error."""
        _stub(monkeypatch, ["xyzzy", [], [], []])

        assert await wikipedia.search("xyzzy nonexistent") == {"results": []}

    @pytest.mark.asyncio
    async def test_single_hit(self, monkeypatch):
        _stub(monkeypatch, ["q", ["Only"], ["desc"], ["https://en.wikipedia.org/wiki/Only"]])

        assert (await wikipedia.search("q"))["results"] == [
            {"title": "Only", "url": "https://en.wikipedia.org/wiki/Only"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("limit", "sent"),
        [
            (0, "1"),  # below the floor
            (1, "1"),
            (5, "5"),
            (wikipedia.MAX_SEARCH_LIMIT, str(wikipedia.MAX_SEARCH_LIMIT)),  # at the cap
            (wikipedia.MAX_SEARCH_LIMIT + 1, str(wikipedia.MAX_SEARCH_LIMIT)),  # one past
            (10_000, str(wikipedia.MAX_SEARCH_LIMIT)),
        ],
    )
    async def test_limit_is_clamped_on_the_wire(self, monkeypatch, limit, sent):
        """The clamp must reach the outgoing request, not just a local variable."""
        requests = _stub(monkeypatch, ["q", [], [], []])

        await wikipedia.search("q", limit=limit)

        assert requests[0].url.params["limit"] == sent

    def test_the_tool_validates_against_the_same_bound(self):
        """The tool's Field bound and the provider's clamp are one constant."""
        from academic_tools_mcp.tools import search as search_tools

        field = search_tools.search_wikipedia.__annotations__["limit"].__metadata__[0]
        assert [m.le for m in field.metadata if hasattr(m, "le")] == [wikipedia.MAX_SEARCH_LIMIT]
        assert str(wikipedia.MAX_SEARCH_LIMIT) in field.description


class TestSearchHardening:
    @pytest.mark.asyncio
    async def test_garbled_body_returns_retryable_error(self, monkeypatch):
        """A 200 with an unparseable body must not crash search()."""
        _stub(monkeypatch, _BAD_JSON)

        result = await wikipedia.search("anything")

        assert "error" in result
        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_transport_failure_returns_an_error(self, monkeypatch):
        _stub(monkeypatch, httpx.ConnectError("no route"))

        result = await wikipedia.search("anything")

        assert "error" in result
        assert "results" not in result

    @pytest.mark.asyncio
    async def test_rate_limit_is_reported_as_retryable(self, monkeypatch):
        _stub(monkeypatch, (429, {"error": "slow down"}))

        result = await wikipedia.search("anything")

        assert result["retryable"] is True
        assert "results" not in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"batchcomplete": ""},  # a dict, not the OpenSearch array
            None,
            "not an array",
            ["q", ["Only"]],  # truncated: no urls element
            ["q", "Article One", ["d"], "https://en.wikipedia.org/wiki/Article_One"],
            ["q", None, None, None],
        ],
    )
    async def test_wrong_shape_is_an_error_never_an_empty_result(self, monkeypatch, payload):
        """Wrong shape must read as "retry", not as "no such article".

        The string case is the sharp one: zipping two strings yields a
        per-character "hit" list an agent would then chain a tool call onto.
        """
        _stub(monkeypatch, payload)

        result = await wikipedia.search("q")

        assert result["retryable"] is True
        assert "results" not in result

    @pytest.mark.asyncio
    async def test_non_string_entries_are_dropped(self, monkeypatch):
        """A well-formed array with a junk entry keeps the usable hits."""
        _stub(
            monkeypatch,
            [
                "q",
                ["Good", 42, None],
                ["", "", ""],
                ["https://en.wikipedia.org/wiki/Good", "https://x", None],
            ],
        )

        assert (await wikipedia.search("q"))["results"] == [
            {"title": "Good", "url": "https://en.wikipedia.org/wiki/Good"}
        ]


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


class TestGetSummary:
    @pytest.mark.asyncio
    async def test_parses_standard_page(self, monkeypatch):
        _stub(monkeypatch, _SUMMARY_PAYLOAD)

        result = await wikipedia.get_summary("Cytochrome P450")

        assert result["title"] == "Cytochrome P450"
        assert result["type"] == "standard"
        assert result["description"] == "Class of enzymes"
        assert "superfamily" in result["extract"]
        assert result["url"] == "https://en.wikipedia.org/wiki/Cytochrome_P450"
        assert result["pageid"] == 709137
        assert cache.get(wikipedia.NAMESPACE, "summaries", "Cytochrome_P450") is not None

    @pytest.mark.asyncio
    async def test_404_is_a_definitive_miss_and_is_negative_cached(self, monkeypatch):
        """`not_found` is what tells "doesn't exist" from "couldn't check"."""
        requests = _stub(monkeypatch, (404, {"title": "Not found."}))

        result = await wikipedia.get_summary("This_Does_Not_Exist_xyzzy123")

        assert result["not_found"] is True
        assert "error" in result
        assert (
            cache.get_negative(wikipedia.NAMESPACE, "summaries", "This_Does_Not_Exist_xyzzy123")
            is not None
        )

        # The negative entry serves the retry: no second request upstream.
        await wikipedia.get_summary("This_Does_Not_Exist_xyzzy123")
        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_transport_failure_returns_an_uncached_error(self, monkeypatch):
        _stub(monkeypatch, httpx.ConnectTimeout("slow"))

        result = await wikipedia.get_summary("Photosynthesis")

        assert "error" in result
        assert result.get("not_found") is not True
        assert cache.get(wikipedia.NAMESPACE, "summaries", "Photosynthesis") is None
        assert cache.get_negative(wikipedia.NAMESPACE, "summaries", "Photosynthesis") is None

    @pytest.mark.asyncio
    async def test_one_spelling_per_article_is_one_fetch(self, monkeypatch):
        """Every spacing of a title shares one cache key, hence one request."""
        requests = _stub(monkeypatch, _SUMMARY_PAYLOAD)

        first = await wikipedia.get_summary("Cytochrome P450")
        second = await wikipedia.get_summary("Cytochrome_P450")
        third = await wikipedia.get_summary("  cytochrome  P450 ")

        assert len(requests) == 1
        assert first == second == third

    @pytest.mark.asyncio
    async def test_force_refresh_rebypasses_cache(self, monkeypatch):
        """A cache hit serves without a network call; force_refresh re-fetches."""
        requests = _stub(monkeypatch, _SUMMARY_PAYLOAD)

        await wikipedia.get_summary("Photosynthesis")
        assert len(requests) == 1

        await wikipedia.get_summary("Photosynthesis")
        assert len(requests) == 1

        await wikipedia.get_summary("Photosynthesis", force_refresh=True)
        assert len(requests) == 2


class TestSummaryHardening:
    @pytest.mark.asyncio
    async def test_garbled_body_returns_retryable_error(self, monkeypatch):
        """A 200 with an unparseable body must not crash — nor cache."""
        _stub(monkeypatch, _BAD_JSON)

        result = await wikipedia.get_summary("Some Title")

        assert result["retryable"] is True
        assert cache.get(wikipedia.NAMESPACE, "summaries", "Some_Title") is None

    @pytest.mark.asyncio
    async def test_non_dict_body_returns_error(self, monkeypatch):
        """A 200 whose JSON is a list (not a dict) must not raise AttributeError."""
        _stub(monkeypatch, ["unexpected", "shape"])

        result = await wikipedia.get_summary("Some Title")

        assert result["retryable"] is True
        assert cache.get(wikipedia.NAMESPACE, "summaries", "Some_Title") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content_urls",
        [
            None,
            [],
            "https://en.wikipedia.org/wiki/X",
            {},
            {"desktop": None},
            {"desktop": "https://en.wikipedia.org/wiki/X"},
            {"desktop": {}},
            {"desktop": {"page": 42}},
        ],
    )
    async def test_wrong_shape_content_urls_degrades_to_an_empty_url(
        self, monkeypatch, content_urls
    ):
        """Every level here is untyped JSON, and the AttributeError a non-dict
        raises is caught by neither _PARSE_ERRORS nor HTTPX_ERRORS — it escapes
        get_summary as a traceback."""
        _stub(monkeypatch, {"type": "standard", "title": "X", "content_urls": content_urls})

        result = await wikipedia.get_summary("X")

        assert result["url"] == ""
        assert result["title"] == "X"


class TestTitleAddressesARecord:
    @pytest.mark.asyncio
    async def test_slash_in_title_is_percent_encoded(self, monkeypatch):
        """A title like 'AC/DC' must encode the slash, not split the path."""
        requests = _stub(monkeypatch, {"type": "standard", "title": "AC/DC"})

        await wikipedia.get_summary("AC/DC")

        # raw_path, not path: httpx decodes the latter back to a slash.
        assert requests[0].url.raw_path == b"/api/rest_v1/page/summary/AC%2FDC"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("title", ["..", ".", " .. ", "", "   ", "___"])
    async def test_a_title_that_does_not_address_a_record_is_refused(self, monkeypatch, title):
        """A title that shortens the path is refused, unspent and uncached.

        `quote` can't escape a `.`/`..` segment, and /page answers 200 with a
        dict — which would cache as this title's summary for the full TTL.
        """
        _stub_no_network(monkeypatch)

        result = await wikipedia.get_summary(title)

        assert result["not_found"] is True
        canonical = wikipedia.canonical_title(title)
        assert cache.get(wikipedia.NAMESPACE, "summaries", canonical) is None
        assert cache.get_negative(wikipedia.NAMESPACE, "summaries", canonical) is None

    @pytest.mark.asyncio
    async def test_a_dot_inside_a_title_is_fine(self, monkeypatch):
        """Only a whole segment of `.`/`..` is removed — 'R.E.M.' is a record."""
        requests = _stub(monkeypatch, {"type": "standard", "title": "R.E.M."})

        result = await wikipedia.get_summary("R.E.M.")

        assert len(requests) == 1
        assert result["title"] == "R.E.M."


# ---------------------------------------------------------------------------
# The MCP tool layer
#
# Everything above covers the provider. These cover the two @mcp.tool wrappers,
# which own a contract the provider does not: the response shape agents branch
# on, and the `suggestion` attached to an error.
# ---------------------------------------------------------------------------


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
    async def test_search_threads_the_limit_at_both_bounds(self, monkeypatch):
        from academic_tools_mcp import server
        from academic_tools_mcp.providers import wikipedia as wp

        seen = []

        async def fake_search(query, limit=5):
            seen.append(limit)
            return {"results": []}

        monkeypatch.setattr(wp, "search", fake_search)

        await server.search_wikipedia("x", limit=1)
        await server.search_wikipedia("x", limit=wp.MAX_SEARCH_LIMIT)
        assert seen == [1, wp.MAX_SEARCH_LIMIT]

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
