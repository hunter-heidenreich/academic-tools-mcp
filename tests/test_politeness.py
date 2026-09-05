"""Politeness: do we actually honour what each upstream documents?

These assert the *policy*, not just the plumbing — that the rate we request
at matches the tier we're entitled to, that every provider identifies itself,
and that an explicit server-side back-off instruction is obeyed in either
form RFC 9110 permits.
"""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from academic_tools_mcp import _http, _useragent, oa_download
from academic_tools_mcp.providers import (
    acl_anthology,
    arxiv,
    biorxiv,
    crossref,
    openalex,
    opencitations,
    wikipedia,
)

_ALL_CLIENTS = [
    ("arxiv", arxiv),
    ("openalex", openalex),
    ("crossref", crossref),
    ("wikipedia", wikipedia),
    ("biorxiv", biorxiv),
    ("opencitations", opencitations),
    ("acl_anthology", acl_anthology),
    ("oa_download", oa_download),
]


class TestEveryProviderIdentifiesItself:
    """biorxiv, opencitations, acl_anthology and the open-access download path
    passed no headers at all, so they went out as ``python-httpx/x.y`` — the
    generic agent several upstreams throttle hardest.
    """

    @pytest.mark.parametrize(("name", "module"), _ALL_CLIENTS)
    def test_sends_descriptive_user_agent(self, name, module):
        ua = module._get_client().headers.get("user-agent", "")
        assert ua.startswith("academic-tools-mcp/"), f"{name} sends {ua!r}"

    @pytest.mark.parametrize(("name", "module"), _ALL_CLIENTS)
    def test_advertises_a_reachable_project_url(self, name, module):
        # The hand-rolled agents pointed at https://github.com/academic-tools-mcp,
        # which does not exist — defeating the purpose of a contact URL.
        ua = module._get_client().headers.get("user-agent", "")
        assert "github.com/hunter-heidenreich/academic-tools-mcp" in ua

    def test_version_is_the_real_package_version(self):
        # Was hardcoded "1.0" against a calendar-versioned package.
        assert "/1.0 (" not in _useragent.build()
        assert _useragent.package_version() in _useragent.build()

    def test_mailto_is_appended_when_configured(self):
        assert "mailto:me@example.org" in _useragent.build("me@example.org")

    def test_agent_is_descriptive_even_without_mailto(self):
        ua = _useragent.build(None)
        assert ua.startswith("academic-tools-mcp/")
        assert "mailto:" not in ua


class TestCrossrefPoolSelection:
    """Crossref runs two tiers and the rate we may use depends on whether we
    identify ourselves. The constants were hardcoded to the *polite* tier
    unconditionally while the mailto that earns it was optional, so an empty
    .env requested at 2x the public rate, 3x its concurrency, 3x its search
    rate — anonymously.

    Documented limits (.claude/rules/providers.md):
                 singles      search      concurrent
        polite   10 req/sec   3 req/sec   3
        public    5 req/sec   1 req/sec   1
    """

    def test_public_pool_policy_without_mailto(self, monkeypatch):
        monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
        concurrent, gap, search_gap = crossref._resolve_policy()
        assert concurrent == 1
        assert gap == pytest.approx(0.2)  # 5 req/sec
        assert search_gap == pytest.approx(1.0)  # 1 req/sec

    def test_polite_pool_policy_with_mailto(self, monkeypatch):
        monkeypatch.setenv("CROSSREF_MAILTO", "me@example.org")
        concurrent, gap, search_gap = crossref._resolve_policy()
        assert concurrent == 3
        assert gap == pytest.approx(0.1)  # 10 req/sec
        assert search_gap == pytest.approx(0.334)  # ~3 req/sec

    def test_public_pool_is_strictly_more_conservative(self, monkeypatch):
        monkeypatch.setenv("CROSSREF_MAILTO", "me@example.org")
        polite = crossref._resolve_policy()
        monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
        public = crossref._resolve_policy()
        assert public[0] < polite[0]
        assert public[1] > polite[1]
        assert public[2] > polite[2]

    def test_in_polite_pool_reflects_config(self, monkeypatch):
        monkeypatch.delenv("CROSSREF_MAILTO", raising=False)
        assert crossref.in_polite_pool() is False
        monkeypatch.setenv("CROSSREF_MAILTO", "me@example.org")
        assert crossref.in_polite_pool() is True

    def test_search_is_paced_separately_from_singles(self):
        # Search used to share the singles throttle entirely, so its tighter
        # limit was never enforced in either tier.
        assert crossref._SEARCH_REQUEST_GAP > crossref._MIN_REQUEST_GAP


def _response_with_retry_after(value):
    headers = {"retry-after": value} if value is not None else {}
    return httpx.Response(429, headers=headers)


class TestRetryAfterHttpDate:
    """RFC 9110 permits both a delay-seconds and an HTTP-date ``Retry-After``,
    and Wikimedia/Cloudflare-fronted endpoints emit dates. Only the numeric
    form was parsed, so a date was discarded and we fell back to a 1.0s
    backoff against a server that had asked for minutes.
    """

    def test_numeric_form(self):
        assert _http._retry_after_seconds(_response_with_retry_after("120")) == 120.0

    def test_http_date_form(self):
        when = datetime.now(UTC) + timedelta(seconds=120)
        got = _http._retry_after_seconds(
            _response_with_retry_after(format_datetime(when, usegmt=True))
        )
        assert got is not None
        assert 110 < got <= 121

    def test_http_date_in_the_past_is_ignored(self):
        when = datetime.now(UTC) - timedelta(seconds=60)
        assert (
            _http._retry_after_seconds(
                _response_with_retry_after(format_datetime(when, usegmt=True))
            )
            is None
        )

    def test_naive_date_is_read_as_utc_not_local_time(self):
        # A naive value read as local time would shift the wait by the host's
        # UTC offset — hours, in either direction.
        when = datetime.now(UTC) + timedelta(seconds=300)
        raw = when.strftime("%a, %d %b %Y %H:%M:%S")
        got = _http._retry_after_seconds(_response_with_retry_after(raw))
        assert got is not None
        assert 280 < got <= 301

    @pytest.mark.parametrize("value", ["inf", "nan", "-inf"])
    def test_non_finite_values_are_rejected(self, value):
        assert _http._retry_after_seconds(_response_with_retry_after(value)) is None

    @pytest.mark.parametrize("value", ["0", "-5", "garbage", "", None])
    def test_unusable_values_fall_back_to_our_own_backoff(self, value):
        assert _http._retry_after_seconds(_response_with_retry_after(value)) is None

    def test_whitespace_is_tolerated(self):
        assert _http._retry_after_seconds(_response_with_retry_after("  90  ")) == 90.0


class TestRetryAfterSurfacedToAgent:
    def test_value_is_clamped_before_reaching_the_agent(self):
        # The internal retry path always honoured a 600s ceiling, but
        # error_dict surfaced the raw header — so a misconfigured
        # "Retry-After: 86400" told the agent to wait a day.
        exc = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://x"),
            response=_response_with_retry_after("86400"),
        )
        result = _http.error_dict("Crossref", exc)
        assert result["retry_after_seconds"] == _http._MAX_RETRY_AFTER_SECONDS

    def test_reasonable_value_passes_through(self):
        exc = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://x"),
            response=_response_with_retry_after("30"),
        )
        assert _http.error_dict("Crossref", exc)["retry_after_seconds"] == 30.0

    def test_http_date_now_reaches_the_agent(self):
        when = datetime.now(UTC) + timedelta(seconds=45)
        exc = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://x"),
            response=_response_with_retry_after(format_datetime(when, usegmt=True)),
        )
        # Previously omitted entirely: the agent got no hint at all.
        assert "retry_after_seconds" in _http.error_dict("Crossref", exc)


class TestStatsAccuracy:
    """``_stats`` is what an operator reads to audit outbound volume and cache
    effectiveness, so both counters being wrong mattered.
    """

    @pytest.mark.asyncio
    async def test_http_calls_counts_every_attempt_not_every_slot(self, monkeypatch):
        # http_calls was incremented once per throttle slot, but a slot issues
        # up to retry_attempts real requests (3 for arXiv) — under-reporting
        # actual outbound volume by up to 3x.
        from academic_tools_mcp import _stats
        from academic_tools_mcp._throttle import Throttle

        _stats.reset()
        throttle = Throttle(
            namespace="probe",
            label="Probe",
            max_concurrent=1,
            min_gap_seconds=0.0,
            retry_attempts=3,
        )

        attempts = 0

        class StubClient:
            async def get(self, url, **kwargs):
                nonlocal attempts
                attempts += 1
                return httpx.Response(503, request=httpx.Request("GET", url))

        monkeypatch.setattr(_http.asyncio, "sleep", _noop_sleep)
        await throttle.get(StubClient(), "https://example.org/x")

        assert attempts == 3
        assert _stats.snapshot()["providers"]["probe"]["http_calls"] == 3

    @pytest.mark.asyncio
    async def test_streaming_download_still_counts_one_call(self, monkeypatch):
        # PDF downloads hold the slot directly and never reach get_with_retry,
        # so moving the counter must not drop them entirely.
        from academic_tools_mcp import _stats
        from academic_tools_mcp._throttle import Throttle

        _stats.reset()
        throttle = Throttle(namespace="probe", label="Probe", max_concurrent=1, min_gap_seconds=0.0)
        async with throttle.slot("https://example.org/x.pdf"):
            pass

        assert _stats.snapshot()["providers"]["probe"]["http_calls"] == 1

    def test_a_single_miss_is_counted_once(self, tmp_path):
        # cached_lookup checks the cache twice (outer, then again inside the
        # single-flight slot), so one genuine miss registered two misses while
        # a hit registered one — making the reported hit rate wrong.
        import asyncio

        from academic_tools_mcp import _singleflight, _stats, cache

        _stats.reset()

        async def fetch():
            return {"ok": True}

        asyncio.run(
            cache.cached_lookup(
                single_flight=_singleflight.SingleFlight(),
                namespace="probe",
                entity="things",
                canonical="k",
                fetch=fetch,
                positive_ttl=None,
            )
        )

        counters = _stats.snapshot()["providers"]["probe"]
        assert counters["cache_misses"] == 1, counters
        assert counters.get("cache_hits", 0) == 0

    def test_count_false_suppresses_both_counters(self, tmp_path):
        from academic_tools_mcp import _stats, cache

        _stats.reset()
        cache.get("probe", "things", "absent", count=False)
        cache.put("probe", "things", "present", {"a": 1})
        cache.get("probe", "things", "present", count=False)

        counters = _stats.snapshot()["providers"].get("probe", {})
        assert counters.get("cache_misses", 0) == 0
        assert counters.get("cache_hits", 0) == 0


async def _noop_sleep(_seconds):
    return None


class TestOaDownloadPacesPerPublisher:
    """OA URLs are resolved from OpenAlex and point at arbitrary publisher
    domains. The gap was 0.0, justified as "every URL is a different host" —
    an assumption, not a fact: a reference walk through one journal resolves
    many DOIs to the same domain, which then got fetched back-to-back with no
    pacing at all, at the one provider with no documented budget and no
    relationship to trade on.
    """

    def test_paces_per_host_at_no_worse_than_one_per_second(self):
        assert oa_download._throttle.per_host is True
        assert oa_download._MIN_REQUEST_GAP >= 1.0

    def test_concurrency_stays_global(self):
        # max_concurrent bounds *our* egress — sockets, fds, and simultaneous
        # in-flight streams (stream_to_file holds the slot for the whole
        # download). Making it per-host would let a 20-publisher walk open 40
        # parallel streams, however polite that is to each publisher.
        assert oa_download._MAX_CONCURRENT <= 4

    @pytest.mark.parametrize(
        ("name", "module"),
        [(n, m) for n, m in _ALL_CLIENTS if n != "oa_download"],
    )
    def test_api_providers_stay_globally_paced(self, name, module):
        # per_host is for a client whose URLs are not one API. Each of these
        # talks to exactly one host, where the map would be a dict of size one
        # — and opting one in would silently widen its documented rate the day
        # it gained a second hostname.
        assert module._throttle.per_host is False, f"{name} should not be per-host paced"
