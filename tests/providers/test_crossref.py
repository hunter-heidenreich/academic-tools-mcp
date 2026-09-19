import asyncio
import json
from urllib.parse import unquote

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp.net import clients, stats
from academic_tools_mcp.providers import crossref
from academic_tools_mcp.store import cache, singleflight
from academic_tools_mcp.util import doinorm

# ---------------------------------------------------------------------------
# Shared transport fakes
# ---------------------------------------------------------------------------


def _reset_crossref(monkeypatch, tmp_path=None):
    """Zero both pacing gaps (the singles one is on the throttle, the search one
    is a module constant) so a two-request test doesn't sleep for real."""
    if tmp_path is not None:
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(crossref._throttle, "min_gap_seconds", 0.0)
    monkeypatch.setattr(crossref._search_gap, "min_gap_seconds", 0.0)
    monkeypatch.setattr(crossref, "_single_flight", singleflight.SingleFlight())


# Sentinel: a payload whose .json() raises, simulating a malformed/truncated body.
_BAD_JSON = object()


def _stub_json_responses(
    monkeypatch, *payloads, status_code=200, status_codes=None, slow=False, headers=None
):
    """Stub client returning ``payloads`` from successive GETs.

    ``status_codes`` gives a per-response sequence (404 then 200); ``headers`` the
    response headers every stubbed response carries, for the ``x-api-pool`` read;
    ``slow`` adds a yield so concurrent callers interleave. The recorder exposes
    ``.urls`` / ``.kwargs`` / ``.count``.
    """
    seq = list(payloads)

    class _Recorder:
        def __init__(self):
            self.urls: list[str] = []
            self.kwargs: list[dict] = []

        @property
        def count(self) -> int:
            return len(self.urls)

    recorder = _Recorder()

    class StubResponse:
        def __init__(self, payload, code):
            self._payload = payload
            self.status_code = code
            self.headers: dict[str, str] = dict(headers or {})

        def raise_for_status(self):
            pass

        def json(self):
            if self._payload is _BAD_JSON:
                raise json.JSONDecodeError("Expecting value", "", 0)
            return self._payload

    class StubClient:
        async def get(self, url, **kwargs):
            recorder.urls.append(url)
            recorder.kwargs.append(kwargs)
            idx = len(recorder.urls) - 1
            if slow:
                await asyncio.sleep(0)
            payload = seq[idx] if idx < len(seq) else seq[-1]
            if status_codes is None:
                code = status_code
            else:
                code = status_codes[idx] if idx < len(status_codes) else status_codes[-1]
            return StubResponse(payload, code)

    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())
    return recorder


def _stub_transport_error(monkeypatch, exc):
    """Stub client whose GET raises ``exc``; one attempt, so no backoff sleep."""
    monkeypatch.setattr(crossref._throttle, "retry_attempts", 1)

    class StubClient:
        async def get(self, url, **kwargs):
            raise exc

    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: StubClient())


def _work_response(doi="10.1234/x"):
    """A minimal valid Crossref /works response wrapping one work."""
    return {"message": {"DOI": doi, "title": ["A Great Work"], "type": "journal-article"}}


def _search_response(items, total=None):
    """A minimal valid Crossref /works search response."""
    message: dict = {"items": items}
    if total is not None:
        message["total-results"] = total
    return {"message": message}


# ---------------------------------------------------------------------------
# search_works parameter building
# ---------------------------------------------------------------------------


class TestSearchWorksParams:
    @pytest.mark.asyncio
    async def test_builds_params_with_year(self, monkeypatch, tmp_path):
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _search_response([], total=42))

        result = await crossref.search_works("some title", year=2022, rows=3)

        # total_results is the upstream match count, not len(items).
        assert result == {"items": [], "total_results": 42}
        params = recorder.kwargs[0]["params"]
        assert params["query.bibliographic"] == "some title"
        assert params["rows"] == "3"
        assert "from-pub-date:2022" in params["filter"]
        assert "until-pub-date:2022" in params["filter"]

    @pytest.mark.asyncio
    async def test_builds_params_without_year(self, monkeypatch, tmp_path):
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _search_response([]))

        result = await crossref.search_works("some title", rows=5)

        params = recorder.kwargs[0]["params"]
        assert "filter" not in params
        assert params["rows"] == "5"
        # No `total-results` upstream reports None, not 0 — "unknown" and "none".
        assert result == {"items": [], "total_results": None}

    @pytest.mark.parametrize(
        ("requested", "sent"),
        [
            (-5, "1"),
            (0, "1"),
            (1, "1"),  # at the floor
            (crossref.MAX_SEARCH_ROWS - 1, str(crossref.MAX_SEARCH_ROWS - 1)),
            (crossref.MAX_SEARCH_ROWS, str(crossref.MAX_SEARCH_ROWS)),  # at the cap
            (crossref.MAX_SEARCH_ROWS + 1, str(crossref.MAX_SEARCH_ROWS)),  # one past
            (10_000, str(crossref.MAX_SEARCH_ROWS)),
        ],
    )
    @pytest.mark.asyncio
    async def test_rows_is_clamped_to_the_published_bound(
        self, monkeypatch, tmp_path, requested, sent
    ):
        """Asserted on the params sent: this was two tests asserting
        ``min(max(100, 1), 20) == 20``, which passed with the clamp deleted."""
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _search_response([]))

        await crossref.search_works("some title", rows=requested)

        assert recorder.kwargs[0]["params"]["rows"] == sent


# ---------------------------------------------------------------------------
# search_works opportunistic cache-warming
# ---------------------------------------------------------------------------


class TestSearchWorksCacheWarming:
    """A hit has the same shape as /works/{doi}, so caching it makes the
    follow-up get_work free. Mirrors arxiv.search_papers."""

    @pytest.mark.asyncio
    async def test_search_hits_warm_works_cache(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)

        # Crossref occasionally returns items with no DOI: not fatal, not cached.
        items = [
            {"DOI": "10.1234/A", "title": ["A"], "type": "journal-article"},
            {"DOI": "10.5678/B", "title": ["B"], "type": "journal-article"},
            {"title": ["C — no DOI"], "type": "journal-article"},
        ]
        _stub_json_responses(monkeypatch, _search_response(items))

        result = await crossref.search_works("anything")

        assert cache.get(crossref.NAMESPACE, "works", "10.1234/a") == items[0]
        assert cache.get(crossref.NAMESPACE, "works", "10.5678/b") == items[1]
        assert result["items"] == items

    @pytest.mark.asyncio
    async def test_warmed_hit_makes_get_work_free(self, tmp_path, monkeypatch):
        """The headline claim: search then get_work costs one HTTP call."""
        _reset_crossref(monkeypatch, tmp_path)
        item = {"DOI": "10.1234/A", "title": ["A"], "type": "journal-article"}
        recorder = _stub_json_responses(monkeypatch, _search_response([item]))

        await crossref.search_works("anything")
        assert recorder.count == 1

        # Any spelling of the DOI resolves to the warmed key.
        work = await crossref.get_work("https://doi.org/10.1234/a")

        assert work == item
        assert recorder.count == 1

    @pytest.mark.asyncio
    async def test_existing_cached_entry_not_clobbered(self, tmp_path, monkeypatch):
        """A sparse search hit must not overwrite a deliberately fetched record."""
        _reset_crossref(monkeypatch, tmp_path)
        rich = {"DOI": "10.1234/A", "title": ["A"], "abstract": "<p>full</p>"}
        cache.put(crossref.NAMESPACE, "works", "10.1234/a", rich)

        sparse = {"DOI": "10.1234/A", "title": ["A"]}
        _stub_json_responses(monkeypatch, _search_response([sparse]))

        await crossref.search_works("anything")

        assert cache.get(crossref.NAMESPACE, "works", "10.1234/a") == rich


# ---------------------------------------------------------------------------
# search_works response-shape guards
# ---------------------------------------------------------------------------


class TestSearchWorksShapeGuards:
    """A wrong-shape 200 is an error, never an empty result set.

    A non-dict `message` fell through to `{"items": []}` — read as "no papers
    match", which ends the search. A non-dict *item* raised AttributeError out
    of the provider, in neither `_PARSE_ERRORS` nor `HTTPX_ERRORS`.
    """

    @pytest.mark.parametrize("message", ["not-an-object", 5, None, ["a", "list"], True])
    @pytest.mark.asyncio
    async def test_non_dict_message_is_a_parse_error(self, monkeypatch, tmp_path, message):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, {"message": message})

        result = await crossref.search_works("some title")

        assert "error" in result
        assert result["retryable"] is True

    @pytest.mark.parametrize("items", ["abc", 5, {"DOI": "10.1/a"}, True])
    @pytest.mark.asyncio
    async def test_non_list_items_is_a_parse_error(self, monkeypatch, tmp_path, items):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _search_response(items))

        result = await crossref.search_works("some title")

        assert "error" in result
        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_missing_message_key_is_a_parse_error(self, monkeypatch, tmp_path):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, {"status": "ok"})

        result = await crossref.search_works("some title")

        assert "error" in result
        assert result["retryable"] is True

    @pytest.mark.parametrize("doi", [True, 5, ["10.1/a"], {"v": "10.1/a"}])
    @pytest.mark.asyncio
    async def test_a_non_string_doi_is_skipped_not_fatal(self, monkeypatch, tmp_path, doi):
        """Truthiness isn't enough: a non-string DOI reaches doinorm.normalize and
        raises AttributeError, caught by neither except clause."""
        _reset_crossref(monkeypatch, tmp_path)
        good = {"DOI": "10.1234/A", "title": ["A"]}
        _stub_json_responses(monkeypatch, _search_response([{"DOI": doi}, good]))

        result = await crossref.search_works("some title")

        assert result["items"] == [{"DOI": doi}, good]
        assert cache.get(crossref.NAMESPACE, "works", "10.1234/a") == good

    @pytest.mark.asyncio
    async def test_non_dict_items_are_skipped_not_fatal(self, monkeypatch, tmp_path):
        """A partly-garbled list still yields its good hits, and warms only those."""
        _reset_crossref(monkeypatch, tmp_path)
        good_a = {"DOI": "10.1234/A", "title": ["A"]}
        good_b = {"DOI": "10.5678/B", "title": ["B"]}
        _stub_json_responses(
            monkeypatch, _search_response([good_a, None, "junk", 7, good_b], total=2)
        )

        result = await crossref.search_works("some title")

        assert result == {"items": [good_a, good_b], "total_results": 2}
        assert cache.get(crossref.NAMESPACE, "works", "10.1234/a") == good_a
        assert cache.get(crossref.NAMESPACE, "works", "10.5678/b") == good_b


# ---------------------------------------------------------------------------
# Headers and client wiring
# ---------------------------------------------------------------------------


class TestBuildHeaders:
    def test_includes_mailto_when_configured(self, monkeypatch):
        monkeypatch.setenv("CROSSREF_MAILTO", "test@example.com")
        headers = crossref._build_headers()
        assert "mailto:test@example.com" in headers.get("User-Agent", "")

    def test_descriptive_ua_sent_without_mailto(self, monkeypatch):
        """This returned {}, so the default config went out as python-httpx/x.y
        while still requesting at the polite-pool rate."""
        monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
        headers = crossref._build_headers()
        assert headers["User-Agent"].startswith("academic-tools-mcp/")
        assert "mailto:" not in headers["User-Agent"]

    def test_ua_advertises_a_real_project_url(self, monkeypatch):
        """The hand-rolled agent pointed at a URL that does not exist."""
        monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
        assert (
            "github.com/hunter-heidenreich/academic-tools-mcp"
            in crossref._build_headers()["User-Agent"]
        )


class TestGetClientWiring:
    def test_bakes_in_namespace_headers_and_timeout(self, monkeypatch):
        """``get_client`` ignores kwargs after the first call per namespace, so
        these are configured here or nowhere."""
        captured = {}

        def fake_get_client(name, **kwargs):
            captured["name"] = name
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(clients, "get_client", fake_get_client)

        crossref._get_client()

        assert captured["name"] == crossref.NAMESPACE
        assert captured["headers"]["User-Agent"].startswith("academic-tools-mcp/")
        assert captured["timeout"] == 30.0


class TestBuildParams:
    """Crossref meters the polite pool by address, so the User-Agent alone left us
    accounted anonymously even when a contact was configured."""

    def test_sends_the_configured_contact(self, monkeypatch):
        monkeypatch.setenv("CROSSREF_MAILTO", "test@example.com")
        assert crossref._build_params() == {"mailto": "test@example.com"}

    def test_empty_without_a_contact(self, monkeypatch):
        monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
        assert crossref._build_params() == {}

    @pytest.mark.parametrize("junk", ["   ", "()", "mailto:"])
    def test_a_contact_that_scrubs_to_nothing_is_not_sent(self, monkeypatch, junk):
        monkeypatch.setenv("CROSSREF_MAILTO", junk)
        assert crossref._build_params() == {}

    def test_the_mailto_scheme_prefix_is_stripped(self, monkeypatch):
        """The header path strips it; the two must not disagree about the address."""
        monkeypatch.setenv("CROSSREF_MAILTO", "mailto:test@example.com")
        assert crossref._build_params() == {"mailto": "test@example.com"}

    @pytest.mark.asyncio
    async def test_get_work_sends_it(self, monkeypatch, tmp_path):
        _reset_crossref(monkeypatch, tmp_path)
        monkeypatch.setenv("CROSSREF_MAILTO", "test@example.com")
        recorder = _stub_json_responses(monkeypatch, _work_response())

        await crossref.get_work("10.1234/x")

        assert recorder.kwargs[0]["params"] == {"mailto": "test@example.com"}

    @pytest.mark.asyncio
    async def test_search_sends_it_alongside_the_query(self, monkeypatch, tmp_path):
        _reset_crossref(monkeypatch, tmp_path)
        monkeypatch.setenv("CROSSREF_MAILTO", "test@example.com")
        recorder = _stub_json_responses(monkeypatch, _search_response([]))

        await crossref.search_works("attention is all you need")

        params = recorder.kwargs[0]["params"]
        assert params["mailto"] == "test@example.com"
        assert params["query.bibliographic"] == "attention is all you need"


# ---------------------------------------------------------------------------
# Pool confirmation
# ---------------------------------------------------------------------------


def _tier(module=crossref):
    """The tier actually in force: ``(max_concurrent, singles gap, search gap)``."""
    return (
        module._throttle.max_concurrent,
        module._throttle.min_gap_seconds,
        module._search_gap.min_gap_seconds,
    )


class TestPoolConfirmation:
    """The rate we take must follow the identity Crossref *confirms* it received.

    The constants were resolved from config alone, so a mailto that never arrived —
    or one Crossref declined — still bought the polite tier's 10 req/sec and 3
    concurrent while the public pool served us at 5 and 1, earning a sustained 429.
    Crossref stamps every response with ``x-api-pool``; these pin that we start
    conservative and widen only on that stamp.
    """

    @pytest.fixture(autouse=True)
    def _public_tier(self, monkeypatch):
        """Restore the starting tier; a promotion otherwise leaks into later tests."""
        crossref.reset_pool_tier()
        yield
        crossref.reset_pool_tier()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stamp", ["polite-single", "polite-multi", "polite", "Polite-Single"])
    async def test_a_confirmed_polite_pool_widens_every_gate(self, monkeypatch, tmp_path, stamp):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        monkeypatch.setattr(crossref, "_single_flight", singleflight.SingleFlight())
        monkeypatch.setenv("CROSSREF_MAILTO", "test@example.com")
        _stub_json_responses(monkeypatch, _work_response(), headers={"x-api-pool": stamp})

        assert _tier() == (1, pytest.approx(0.2), pytest.approx(1.0))
        await crossref.get_work("10.1234/x")

        assert _tier() == (3, pytest.approx(0.1), pytest.approx(0.334))

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response_headers",
        [{"x-api-pool": "public"}, {"x-api-pool": "public-single"}, {}, None],
    )
    async def test_anything_but_a_polite_stamp_leaves_the_public_tier(
        self, monkeypatch, tmp_path, response_headers
    ):
        """A missing header is not a grant — ``net/stats`` leaves an unadvertised
        quota alone for the same reason."""
        _reset_crossref(monkeypatch, tmp_path)
        monkeypatch.setenv("CROSSREF_MAILTO", "test@example.com")
        _stub_json_responses(monkeypatch, _work_response(), headers=response_headers)

        await crossref.get_work("10.1234/x")

        assert crossref._throttle.max_concurrent == 1
        assert crossref.in_confirmed_polite_pool() is False

    @pytest.mark.asyncio
    async def test_no_contact_means_no_promotion_whatever_crossref_says(
        self, monkeypatch, tmp_path
    ):
        """Belt and braces: we cannot be in a pool we sent no address for, so a
        stray header must not widen an anonymous client."""
        _reset_crossref(monkeypatch, tmp_path)
        monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
        _stub_json_responses(monkeypatch, _work_response(), headers={"x-api-pool": "polite"})

        await crossref.get_work("10.1234/x")

        assert crossref._throttle.max_concurrent == 1

    @pytest.mark.asyncio
    async def test_promotion_happens_once(self, monkeypatch, tmp_path):
        """``Semaphore.release`` adds a permit per call, so a re-run would widen
        concurrency without bound."""
        _reset_crossref(monkeypatch, tmp_path)
        monkeypatch.setenv("CROSSREF_MAILTO", "test@example.com")
        _stub_json_responses(
            monkeypatch, _work_response("10.1234/a"), headers={"x-api-pool": "polite"}
        )

        await crossref.get_work("10.1234/a")
        await crossref.get_work("10.1234/b")
        await crossref.get_work("10.1234/c")

        assert crossref._throttle.max_concurrent == 3

    @pytest.mark.asyncio
    async def test_a_search_response_confirms_it_too(self, monkeypatch, tmp_path):
        _reset_crossref(monkeypatch, tmp_path)
        monkeypatch.setenv("CROSSREF_MAILTO", "test@example.com")
        _stub_json_responses(monkeypatch, _search_response([]), headers={"x-api-pool": "polite"})

        await crossref.search_works("anything")

        assert crossref.in_confirmed_polite_pool() is True

    @pytest.mark.asyncio
    async def test_reset_pool_tier_restores_the_public_policy(self, monkeypatch, tmp_path):
        """The conftest seam. Without it a promotion is the starting tier of every
        later test, and the suite goes order-dependent."""
        _reset_crossref(monkeypatch, tmp_path)
        monkeypatch.setenv("CROSSREF_MAILTO", "test@example.com")
        _stub_json_responses(monkeypatch, _work_response(), headers={"x-api-pool": "polite"})
        await crossref.get_work("10.1234/x")

        crossref.reset_pool_tier()

        assert _tier() == (1, pytest.approx(0.2), pytest.approx(1.0))
        assert crossref.in_confirmed_polite_pool() is False
        assert crossref._throttle._sem._value == 1


# ---------------------------------------------------------------------------
# Malformed-body / parse-error handling and DOI path encoding
# ---------------------------------------------------------------------------


class TestGetWorkParseErrors:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, tmp_path, monkeypatch):
        """A 200 with a garbled body surfaces the uniform contract, not a crash."""
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await crossref.get_work("10.1234/x")

        assert "error" in result
        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_malformed_json_not_negative_cached(self, tmp_path, monkeypatch):
        """Transient, so a retry must re-fetch rather than be served the failure."""
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _BAD_JSON, _work_response())

        first = await crossref.get_work("10.1234/x")
        assert "error" in first

        second = await crossref.get_work("10.1234/x")
        assert second.get("DOI") == "10.1234/x"
        assert recorder.count == 2

    @pytest.mark.asyncio
    async def test_missing_message_not_cached(self, tmp_path, monkeypatch):
        """A 200 with no `message` must not positive-cache an empty dict."""
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, {"status": "ok"}, _work_response())

        first = await crossref.get_work("10.1234/x")
        assert "error" in first

        second = await crossref.get_work("10.1234/x")
        assert second.get("DOI") == "10.1234/x"
        assert recorder.count == 2


class TestSearchWorksParseErrors:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await crossref.search_works("some title")

        assert "error" in result
        assert result.get("retryable") is True


class TestGetWorkDoiEncoding:
    @pytest.mark.asyncio
    async def test_encodes_special_chars_in_doi(self, tmp_path, monkeypatch):
        """Unescaped, httpx reads '#bar' as a fragment and fetches the wrong record."""
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _work_response("10.1234/foo#bar"))

        await crossref.get_work("10.1234/foo#bar")

        assert "%23" in recorder.urls[0]
        assert "#" not in recorder.urls[0]

    @pytest.mark.asyncio
    async def test_preserves_slash_in_doi(self, tmp_path, monkeypatch):
        """The DOI's own slash stays literal — Crossref's proven-working form."""
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _work_response("10.1038/nature12373"))

        await crossref.get_work("10.1038/nature12373")

        assert recorder.urls[0].endswith("/works/10.1038/nature12373")


class TestGetWorkForceRefresh:
    @pytest.mark.asyncio
    async def test_force_refresh_rebypasses_cache(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _work_response())

        await crossref.get_work("10.1234/x")
        assert recorder.count == 1

        await crossref.get_work("10.1234/x")
        assert recorder.count == 1  # cache hit

        await crossref.get_work("10.1234/x", force_refresh=True)
        assert recorder.count == 2


# ---------------------------------------------------------------------------
# 404: the definitive miss and its negative-cache entry
# ---------------------------------------------------------------------------


class TestPathShorteningIsRefused:
    """A `.`/`..` segment is *removed* by RFC 3986 resolution after encoding —
    both characters are unreserved, so no `quote` escapes them. The shortened
    path is the /works *collection*, whose 200 carries a work-list under a dict
    `message`: it clears the shape ladder and caches as this DOI's work."""

    @pytest.mark.parametrize("doi", ["10.1234/..", "10.1038/a/..", "10.1234/.", "10.1234/a/../b"])
    @pytest.mark.asyncio
    async def test_it_is_a_definitive_miss_costing_no_request(self, tmp_path, monkeypatch, doi):
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _work_response())

        result = await crossref.get_work(doi)

        assert recorder.count == 0
        assert result["not_found"] is True
        assert "retryable" not in result

    @pytest.mark.asyncio
    async def test_nothing_is_cached_for_it(self, tmp_path, monkeypatch):
        # Uncached on purpose: no request was spent, so nothing was learned
        # about the identifier worth holding for the negative TTL.
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _work_response())

        await crossref.get_work("10.1234/..")

        assert cache.get(crossref.NAMESPACE, "works", "10.1234/..") is None
        assert cache.get_negative(crossref.NAMESPACE, "works", "10.1234/..") is None

    @pytest.mark.asyncio
    async def test_an_escaped_dot_segment_is_a_normal_identifier(self, tmp_path, monkeypatch):
        # The guard reads the *encoded* path, so a DOI whose suffix is a
        # literal '%2E%2E' addresses a real record and must not be refused.
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _work_response())

        result = await crossref.get_work("10.1234/%2E%2E")

        assert "error" not in result
        assert recorder.count == 1


class TestGetWorkNotFound:
    """The one branch writing a durable negative entry, and the only one that
    can carry `not_found` — the flag tools/graph.py forwards so an agent can
    tell "Crossref lacks this DOI" from "Crossref was briefly unreachable"."""

    @pytest.mark.asyncio
    async def test_404_is_a_definitive_not_found(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, {}, status_code=404)

        result = await crossref.get_work("10.1234/missing")

        assert "error" in result
        assert result["not_found"] is True
        assert "retryable" not in result  # definitive

    @pytest.mark.asyncio
    async def test_404_is_negative_cached(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, {}, status_code=404)

        first = await crossref.get_work("10.1234/missing")
        second = await crossref.get_work("10.1234/missing")

        assert first == second
        assert second["not_found"] is True
        # The miss costs a second request to name the registering agency; the
        # negative entry carries the answer, so the repeat costs nothing.
        assert recorder.count == 2

    @pytest.mark.asyncio
    async def test_the_negative_entry_is_keyed_canonically(self, tmp_path, monkeypatch):
        """Any spelling of the missed DOI hits the same negative entry."""
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, {}, status_code=404)

        await crossref.get_work("10.1234/MISSING")
        again = await crossref.get_work("https://doi.org/10.1234/missing")

        assert again["not_found"] is True
        assert recorder.count == 2  # the work and its agency, once between them

    @pytest.mark.asyncio
    async def test_force_refresh_drops_the_negative_entry(self, tmp_path, monkeypatch):
        """A DOI that 404'd is retryable once the publisher deposits it."""
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(
            monkeypatch, {}, _work_response("10.1234/missing"), status_codes=[404, 200]
        )

        missed = await crossref.get_work("10.1234/missing")
        assert missed["not_found"] is True

        found = await crossref.get_work("10.1234/missing", force_refresh=True)

        assert found["DOI"] == "10.1234/missing"
        # The 404, the agency probe behind it, then the refreshed hit.
        assert recorder.count == 3


def _agency_response(agency_id, label=None):
    """A minimal valid Crossref /works/{doi}/agency response."""
    return {
        "message": {"DOI": "10.1234/x", "agency": {"id": agency_id, "label": label or agency_id}}
    }


class TestGetAgency:
    """Which registrar minted a DOI — the difference between "Crossref hasn't
    indexed this yet" and "this is a dataset nothing here will ever index"."""

    @pytest.mark.asyncio
    async def test_returns_the_agency_object(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _agency_response("datacite", "DataCite"))

        assert await crossref.get_agency("10.5281/zenodo.1") == {
            "id": "datacite",
            "label": "DataCite",
        }

    @pytest.mark.asyncio
    async def test_is_cached(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _agency_response("crossref", "Crossref"))

        await crossref.get_agency("10.1234/x")
        await crossref.get_agency("10.1234/X")

        assert recorder.count == 1  # and keyed canonically

    @pytest.mark.asyncio
    async def test_a_missing_agency_key_is_a_parse_error(self, tmp_path, monkeypatch):
        """A 200 whose message lacks `agency` is a wrong shape, not an empty answer."""
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, {"message": {"DOI": "10.1234/x"}})

        result = await crossref.get_agency("10.1234/x")

        assert result["retryable"] is True
        assert cache.get(crossref.NAMESPACE, "agency", "10.1234/x") is None

    @pytest.mark.asyncio
    async def test_404_is_negative_cached(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, {}, status_code=404)

        first = await crossref.get_agency("10.1234/missing")
        await crossref.get_agency("10.1234/missing")

        assert first["not_found"] is True
        assert recorder.count == 1


class TestNotFoundNamesTheRegistrar:
    """A DataCite DOI — a dataset, a Zenodo record, software — used to read as an
    ordinary Crossref miss, sending an agent back to retry a DOI no client here
    will ever answer."""

    @pytest.mark.asyncio
    async def test_a_datacite_doi_says_so(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(
            monkeypatch,
            {},
            _agency_response("datacite", "DataCite"),
            status_codes=[404, 200],
        )

        result = await crossref.get_work("10.5281/zenodo.1")

        assert result["not_found"] is True
        assert "DataCite" in result["error"]

    @pytest.mark.asyncio
    async def test_a_crossref_doi_adds_nothing(self, tmp_path, monkeypatch):
        """Naming Crossref in a Crossref refusal is noise: the DOI is registered
        here and simply not deposited yet."""
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(
            monkeypatch,
            {},
            _agency_response("crossref", "Crossref"),
            status_codes=[404, 200],
        )

        result = await crossref.get_work("10.1234/notyet")

        assert result["error"] == "No work found on Crossref for DOI: 10.1234/notyet"

    @pytest.mark.asyncio
    async def test_a_failed_agency_probe_degrades_to_the_plain_refusal(self, tmp_path, monkeypatch):
        """The miss is definitive whatever the probe does; a transient failure
        there must not turn it into something an agent reads as retryable."""
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, {}, status_codes=[404, 500])

        result = await crossref.get_work("10.1234/missing")

        assert result["not_found"] is True
        assert "retryable" not in result
        assert result["error"] == "No work found on Crossref for DOI: 10.1234/missing"


# ---------------------------------------------------------------------------
# updated-by / relation readers
# ---------------------------------------------------------------------------


_RETRACTED = {
    "DOI": "10.1016/s0140-6736(97)11096-0",
    "updated-by": [
        {
            "DOI": "10.1016/s0140-6736(04)15715-2",
            "type": "correction",
            "label": "Correction",
            "source": "retraction-watch",
            "updated": {"date-parts": [[2004, 3, 6]]},
        },
        {
            "DOI": "10.1016/S0140-6736(10)60175-4",
            "type": "retraction",
            "label": "Retraction",
            "source": "retraction-watch",
            "updated": {"date-parts": [[2010, 2, 6]]},
        },
    ],
}


class TestRetracts:
    """Which Crossmark update types leave a paper uncitable. Sampled live, 200
    ``is-update:true`` works carry correction, retraction, erratum, new_version,
    new_edition, corrigendum, clarification, expression_of_concern and withdrawal —
    so the withdrawing class is not hypothetical, and reading only ``retraction``
    clears a withdrawn paper.
    """

    @pytest.mark.parametrize(
        "update_type", ["retraction", "partial_retraction", "withdrawal", "removal"]
    )
    def test_the_withdrawing_class(self, update_type):
        assert crossref.retracts(update_type) is True

    @pytest.mark.parametrize(
        "update_type",
        [
            "correction",
            "corrigendum",
            "erratum",
            "expression_of_concern",
            "clarification",
            "addendum",
            "new_edition",
            "new_version",
        ],
    )
    def test_the_amending_class(self, update_type):
        assert crossref.retracts(update_type) is False

    @pytest.mark.parametrize("spelling", ["Retraction", " retraction ", "partial-retraction"])
    def test_deposit_spelling_does_not_decide_it(self, spelling):
        """Deposited by hand, so case and a hyphen for the underscore both occur."""
        assert crossref.retracts(spelling) is True

    def test_an_unknown_type_does_not_set_the_flag(self):
        """A vocabulary Crossref grows later reaches the agent through `updates`;
        inventing a verdict for it would be the wrong direction to guess in."""
        assert crossref.retracts("something_new") is False


class TestUpdates:
    def test_reads_every_notice(self):
        found = crossref.updates(_RETRACTED)
        assert [u["type"] for u in found] == ["correction", "retraction"]
        assert [u["source"] for u in found] == ["retraction-watch", "retraction-watch"]

    def test_the_notice_doi_is_canonical(self):
        """So it correlates with every other DOI this server hands back."""
        assert crossref.updates(_RETRACTED)[1]["doi"] == "10.1016/s0140-6736(10)60175-4"

    def test_the_raw_date_parts_ride_along(self):
        """The year walk is `app.crossref_date`'s, a layer above this module."""
        assert crossref.updates(_RETRACTED)[0]["updated"] == {"date-parts": [[2004, 3, 6]]}

    def test_a_work_with_no_notices_is_empty(self):
        assert crossref.updates({"DOI": "10.1234/x"}) == []

    @pytest.mark.parametrize("raw", [None, 7, "retraction", {}, [], [None], [7], ["x"]])
    def test_a_wrong_shape_degrades(self, raw):
        assert crossref.updates({"updated-by": raw}) == []

    @pytest.mark.parametrize(
        "entry",
        [
            {"type": "retraction"},  # no DOI
            {"DOI": "10.1/x"},  # no type
            {"DOI": "", "type": "retraction"},  # empty DOI
            {"DOI": 7, "type": "retraction"},
            {"DOI": "10.1/x", "type": 7},
        ],
    )
    def test_an_entry_missing_either_required_field_is_dropped(self, entry):
        assert crossref.updates({"updated-by": [entry]}) == []

    def test_optional_fields_degrade_to_none(self):
        found = crossref.updates({"updated-by": [{"DOI": "10.1/x", "type": "retraction"}]})
        assert found[0]["label"] is None
        assert found[0]["source"] is None
        assert found[0]["updated"] == {}


class TestRelations:
    def test_reads_the_hashmap(self):
        """Crossref deposits `relation` as a map of name to list, not the flat
        list `author` and `title` carry."""
        work = {
            "relation": {
                "is-preprint-of": [
                    {"id-type": "doi", "id": "10.1364/OE.572415", "asserted-by": "subject"}
                ]
            }
        }
        assert crossref.relations(work) == {"is-preprint-of": ["10.1364/oe.572415"]}

    def test_keeps_every_named_relation(self):
        work = {
            "relation": {
                "references": [{"id-type": "doi", "id": "10.2337/ds24-0025"}],
                "is-preprint-of": [{"id-type": "doi", "id": "10.2337/ds24-0025"}],
            }
        }
        assert set(crossref.relations(work)) == {"references", "is-preprint-of"}

    def test_non_doi_ids_are_dropped(self):
        """Every consumer downstream takes a DOI."""
        work = {"relation": {"is-same-as": [{"id-type": "issn", "id": "1234-5678"}]}}
        assert crossref.relations(work) == {}

    def test_an_empty_relation_map_is_empty(self):
        """The common case: publisher deposit is thin."""
        assert crossref.relations({"relation": {}}) == {}

    @pytest.mark.parametrize("raw", [None, 7, [], "is-preprint-of"])
    def test_a_wrong_shape_degrades(self, raw):
        assert crossref.relations({"relation": raw}) == {}

    @pytest.mark.parametrize("entries", [None, 7, {}, [None], [7], [{}], [{"id": "10.1/x"}]])
    def test_wrong_shaped_entries_degrade(self, entries):
        assert crossref.relations({"relation": {"is-preprint-of": entries}}) == {}


# ---------------------------------------------------------------------------
# Single-flight coalescing
# ---------------------------------------------------------------------------


class TestGetWorkSingleFlight:
    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_fetch(self, tmp_path, monkeypatch):
        """``slow`` is load-bearing: a stub that never awaits runs inline, so the
        second caller would find a warm cache and pass for the wrong reason."""
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _work_response(), slow=True)

        results = await asyncio.gather(*(crossref.get_work("10.1234/x") for _ in range(5)))

        assert recorder.count == 1
        assert all(r["DOI"] == "10.1234/x" for r in results)

    @pytest.mark.asyncio
    async def test_every_caller_gets_an_independent_copy(self, tmp_path, monkeypatch):
        """``cached_lookup`` deep-copies, so one caller's mutation is its own."""
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _work_response(), slow=True)

        first, second = await asyncio.gather(
            crossref.get_work("10.1234/x"), crossref.get_work("10.1234/x")
        )
        first["title"] = ["mutated"]

        assert second["title"] == ["A Great Work"]

    @pytest.mark.asyncio
    async def test_different_dois_do_not_coalesce(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        recorder = _stub_json_responses(
            monkeypatch, _work_response("10.1234/a"), _work_response("10.1234/b"), slow=True
        )

        await asyncio.gather(crossref.get_work("10.1234/a"), crossref.get_work("10.1234/b"))

        assert recorder.count == 2


# ---------------------------------------------------------------------------
# Search pacing — the second-tier gate
# ---------------------------------------------------------------------------


class TestSearchPacing:
    """Crossref limits search far more tightly than singles. The only prior
    assertion compared the two constants, so swapping the call for
    ``_throttled_get`` left the suite green and the limit unenforced."""

    @pytest.mark.asyncio
    async def test_search_uses_the_search_gate_and_get_work_does_not(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _search_response([]), _work_response())

        calls: list[str] = []
        real_search = crossref._throttled_search_get
        real_get = crossref._throttled_get

        async def spy_search(url, **kwargs):
            calls.append("search")
            return await real_search(url, **kwargs)

        async def spy_get(url, **kwargs):
            calls.append("single")
            return await real_get(url, **kwargs)

        monkeypatch.setattr(crossref, "_throttled_search_get", spy_search)
        monkeypatch.setattr(crossref, "_throttled_get", spy_get)

        await crossref.search_works("some title")
        await crossref.get_work("10.1234/x")

        # A search rides both gates; a singleton lookup must never record "search".
        assert calls[0] == "search"
        assert "search" not in calls[1:]

    @pytest.mark.asyncio
    async def test_the_first_search_does_not_wait(self, tmp_path, monkeypatch):
        """A fresh gap has no last start, so nothing to wait out."""
        _reset_crossref(monkeypatch, tmp_path)
        monkeypatch.setattr(crossref._search_gap, "min_gap_seconds", 30.0)
        crossref._search_gap.reset()
        _stub_json_responses(monkeypatch, _search_response([]))

        started = asyncio.get_running_loop().time()
        await crossref.search_works("some title")

        assert asyncio.get_running_loop().time() - started < 1.0

    @pytest.mark.asyncio
    async def test_a_second_search_waits_out_the_gap(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        gap = 0.05
        monkeypatch.setattr(crossref._search_gap, "min_gap_seconds", gap)
        crossref._search_gap.reset()
        _stub_json_responses(monkeypatch, _search_response([]))

        started = asyncio.get_running_loop().time()
        await crossref.search_works("first")
        await crossref.search_works("second")

        assert asyncio.get_running_loop().time() - started >= gap

    def test_the_search_gap_is_reachable_from_the_reset_seam(self):
        """conftest rebuilds the loop-bound lock through the package-wide scan.

        The wrapper this gap once exposed for that is gone; what has to hold now is
        that the scan finds the gap at all. `SubGap.reset` itself is pinned in
        tests/net/test_throttle.py.
        """
        assert any(gate is crossref._search_gap for gate in stats.pacers())


# ---------------------------------------------------------------------------
# Whole-body shape guards
# ---------------------------------------------------------------------------


class TestMalformedPayloadShape:
    """A JSON body that isn't an object must not raise: ``"message" not in data``
    raises TypeError on a null or scalar, caught by neither except clause."""

    @pytest.mark.parametrize("payload", [None, 5, "a string", [1, 2, 3], True])
    @pytest.mark.asyncio
    async def test_get_work_does_not_raise(self, monkeypatch, payload):
        _reset_crossref(monkeypatch)
        _stub_json_responses(monkeypatch, payload)

        result = await crossref.get_work("10.1234/x")

        assert "error" in result
        assert result["retryable"] is True

    @pytest.mark.parametrize("payload", [None, 5, "a string", [1, 2, 3]])
    @pytest.mark.asyncio
    async def test_search_works_does_not_raise(self, monkeypatch, payload):
        _reset_crossref(monkeypatch)
        _stub_json_responses(monkeypatch, payload)

        result = await crossref.search_works("some title")

        assert "error" in result
        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_non_dict_message_is_a_parse_error(self, monkeypatch):
        _reset_crossref(monkeypatch)
        _stub_json_responses(monkeypatch, {"message": "not-an-object"})

        result = await crossref.get_work("10.1234/x")

        assert "error" in result
        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_malformed_payload_is_not_negative_cached(self, monkeypatch):
        """A parse failure says nothing about whether the DOI exists."""
        _reset_crossref(monkeypatch)
        recorder = _stub_json_responses(monkeypatch, None, {"message": {"DOI": "10.1234/x"}})

        first = await crossref.get_work("10.1234/x")
        second = await crossref.get_work("10.1234/x")

        assert "error" in first
        assert second.get("DOI") == "10.1234/x"
        assert recorder.count == 2


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------


class TestTransportErrors:
    """A network failure says nothing about the DOI: same retryable contract as
    an unparseable body, and never negative-cached."""

    @pytest.mark.parametrize(
        "exc", [httpx.ConnectError("no route"), httpx.ReadTimeout("timed out")]
    )
    @pytest.mark.asyncio
    async def test_get_work_surfaces_a_retryable_error(self, tmp_path, monkeypatch, exc):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_transport_error(monkeypatch, exc)

        result = await crossref.get_work("10.1234/x")

        assert "error" in result
        assert result["retryable"] is True
        assert "not_found" not in result

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_not_cached(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_transport_error(monkeypatch, httpx.ConnectError("no route"))
        assert "error" in await crossref.get_work("10.1234/x")

        recorder = _stub_json_responses(monkeypatch, _work_response())
        assert (await crossref.get_work("10.1234/x"))["DOI"] == "10.1234/x"
        assert recorder.count == 1

    @pytest.mark.asyncio
    async def test_search_surfaces_a_retryable_error(self, tmp_path, monkeypatch):
        _reset_crossref(monkeypatch, tmp_path)
        _stub_transport_error(monkeypatch, httpx.ReadTimeout("timed out"))

        result = await crossref.search_works("some title")

        assert "error" in result
        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_local_backpressure_reaches_the_agent_as_an_error(self, tmp_path, monkeypatch):
        """A refusal we raised ourselves uses the same contract as an upstream one."""
        _reset_crossref(monkeypatch, tmp_path)
        monkeypatch.setattr(crossref._throttle, "max_pending", 1)
        monkeypatch.setattr(crossref._throttle, "pending", 1)
        _stub_json_responses(monkeypatch, _work_response())

        result = await crossref.get_work("10.1234/x")

        assert result["retryable"] is True
        assert result["backpressure"] is True


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

# Nested, so a wrong shape can hide under `message` or inside `items`.
_json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)

_search_bodies = st.one_of(
    _json_values,
    st.builds(lambda v: {"message": v}, _json_values),
    st.builds(lambda v: {"message": {"items": v}}, _json_values),
    st.builds(
        lambda i, t: {"message": {"items": i, "total-results": t}},
        st.lists(_json_values, max_size=4),
        _json_values,
    ),
)

# `#` and `?` are included, unlike `util/test_doinorm_properties.dois`: they are exactly
# what `quote(..., safe="/")` exists to neutralise.
_doi_suffix_chars = st.characters(
    min_codepoint=33,
    max_codepoint=0x2FFF,
    blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"),
).filter(lambda c: not c.isspace())

_bare_dois = st.builds(
    lambda registrant, suffix: f"10.{registrant}/{suffix}",
    st.integers(min_value=1000, max_value=99999999).map(str),
    st.text(alphabet=_doi_suffix_chars, min_size=1, max_size=40),
)


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(body=_search_bodies)
def test_search_works_never_raises_and_always_returns_one_of_two_shapes(monkeypatch, body):
    """The tool layer indexes straight into these hits, so anything escaping
    here reaches the agent as a traceback rather than an ``{error}``."""
    _reset_crossref(monkeypatch)
    crossref._search_gap.reset()
    crossref._throttle.reset()
    _stub_json_responses(monkeypatch, body)

    result = asyncio.run(crossref.search_works("some title"))

    assert isinstance(result, dict)
    if "error" in result:
        assert result["retryable"] is True
    else:
        assert isinstance(result["items"], list)
        assert all(isinstance(item, dict) for item in result["items"])
        assert "total_results" in result


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(doi=_bare_dois)
def test_the_requested_path_round_trips_to_the_bare_doi(monkeypatch, doi):
    """A surviving reserved character doesn't fail loudly — httpx fetches a
    different, existing record, which then caches under this DOI's key."""
    _reset_crossref(monkeypatch)
    crossref._throttle.reset()
    recorder = _stub_json_responses(monkeypatch, _work_response(doi))

    # force_refresh so every example fetches: hypothesis reuses one cache root,
    # and two DOIs differing only in case share a canonical key.
    result = asyncio.run(crossref.get_work(doi, force_refresh=True))

    if not recorder.urls:
        # `http.addresses_a_record` refused before the request was spent — an
        # encoded path that no longer names a record (a trailing `/` leaves the
        # /works *collection*). Nothing round-trips, and that is the outcome
        # this guard exists to produce. Mirrors the OpenCitations twin.
        assert result["not_found"] is True
        return

    prefix = f"{crossref.CROSSREF_BASE_URL}/works/"
    url = recorder.urls[0]
    assert url.startswith(prefix)
    tail = url[len(prefix) :]
    assert "#" not in tail
    assert "?" not in tail
    assert unquote(tail) == doinorm.normalize(doi)


class TestAuthorName:
    """The Crossref name shape, single-homed here so `bibtex`'s surname rule and
    `get_paper_authors`' name field cannot disagree about one name."""

    @pytest.mark.parametrize(
        ("author", "expected"),
        [
            ({"given": "Ada", "family": "Lovelace"}, "Ada Lovelace"),
            ({"family": "Lovelace"}, "Lovelace"),
            ({"given": "Ada", "family": "van Beethoven"}, "Ada van Beethoven"),
            # An organisation deposits a single `name`.
            ({"name": "The Consortium"}, "The Consortium"),
            # A `family` wins over a stray `name`.
            ({"family": "Lovelace", "name": "Org"}, "Lovelace"),
            # Untyped JSON: every wrong shape degrades to "", never raises.
            ({}, ""),
            ({"given": "Ada"}, ""),
            ({"family": 42}, ""),
            ({"family": ""}, ""),
            ({"given": 42, "family": "Lovelace"}, "Lovelace"),
            ({"name": 42}, ""),
            (None, ""),
            ("not a dict", ""),
            ([], ""),
        ],
    )
    def test_name_shapes(self, author, expected):
        assert crossref.author_name(author) == expected


class TestAbstractText:
    """Crossref deposits JATS markup, so the stored value is not readable text.

    The counterpart of `openalex.reconstruct_abstract`, and here for the same
    reason: rendering the provider's storage format is the provider's business.
    """

    def test_strips_tags_unescapes_entities_and_collapses_whitespace(self):
        raw = "<jats:p>Since  the\n outbreak &amp; onwards, a &lt;large&gt; number.</jats:p>"
        assert crossref.abstract_text({"abstract": raw}) == (
            "Since the outbreak & onwards, a <large> number."
        )

    def test_drops_the_label_but_keeps_a_structured_abstracts_sections(self):
        """`<jats:title>Abstract</jats:title>` names the field; `Background`
        names a section and is part of the text."""
        labelled = "<jats:title>Abstract</jats:title><jats:p>Body.</jats:p>"
        structured = (
            "<jats:sec><jats:title>Background</jats:title><jats:p>Body.</jats:p></jats:sec>"
        )
        assert crossref.abstract_text({"abstract": labelled}) == "Body."
        assert crossref.abstract_text({"abstract": structured}) == "Background Body."

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            42,
            [],
            {"nested": "dict"},
            # Markup with no text is not an abstract.
            "<jats:p></jats:p>",
            "   ",
        ],
    )
    def test_absent_or_wrong_shaped_returns_none(self, raw):
        assert crossref.abstract_text({"abstract": raw}) is None

    def test_missing_key_returns_none(self):
        assert crossref.abstract_text({}) is None


class TestInstitutionName:
    """`institution` is a list of *objects*, unlike the string lists beside it.

    A reader that treats it like `title` or `container-title` silently drops
    every dissertation's awarding school, which is why the accessor lives here
    rather than being open-coded by `bibtex`.
    """

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ([{"name": "MIT", "place": ["Cambridge, MA"]}], "MIT"),
            ({"name": "MIT"}, "MIT"),
            ("MIT", "MIT"),
            # Some deposits carry a place-only entry ahead of the named one.
            ([{"place": ["Cambridge"]}, {"name": "MIT"}], "MIT"),
            ([], ""),
            ([{"name": 7}], ""),
            ({"place": ["Cambridge"]}, ""),
            (None, ""),
            (7, ""),
        ],
    )
    def test_pulls_the_name_out_of_every_shape_crossref_deposits(self, value, expected):
        assert crossref.institution_name(value) == expected
