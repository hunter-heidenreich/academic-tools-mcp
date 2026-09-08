"""Politeness: do we actually honour what each upstream documents?

These assert the *policy*, not just the plumbing — that the rate we request
at matches the tier we're entitled to, that every provider identifies itself,
and that an explicit server-side back-off instruction is obeyed in either
form RFC 9110 permits.
"""

import ast
import importlib
import inspect
import pathlib
import pkgutil
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

import academic_tools_mcp
from academic_tools_mcp.download import openaccess
from academic_tools_mcp.net import http
from academic_tools_mcp.providers import crossref, opencitations


def _discover_clients():
    """Every module holding a pooled outbound client, found by import scan.

    Deliberately not a hand-maintained list, for the reason ``stats.throttles``
    is not one: a new provider is covered the moment it exists, with no second
    roster to keep in sync. A module qualifies by holding both a ``_get_client``
    and a ``throttle`` -- the pair every outbound client has.
    """
    found = []
    for info in pkgutil.walk_packages(
        academic_tools_mcp.__path__, f"{academic_tools_mcp.__name__}."
    ):
        module = importlib.import_module(info.name)
        if hasattr(module, "_get_client") and hasattr(module, "_throttle"):
            found.append((info.name.rsplit(".", 1)[-1], module))
    return sorted(found, key=lambda entry: entry[0])


_ALL_CLIENTS = _discover_clients()


def test_every_client_module_was_discovered():
    # Guards the scan itself: if it silently found nothing, every
    # parametrized politeness check below would vacuously pass.
    names = [name for name, _ in _ALL_CLIENTS]
    assert "openaccess" in names
    assert {
        "acl",
        "arxiv",
        "biorxiv",
        "crossref",
        "openalex",
        "opencitations",
        "wikipedia",
    } <= set(names)
    assert len(names) == len(set(names))


class TestEveryProviderIdentifiesItself:
    """biorxiv, opencitations, acl and the open-access download path
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


# Where a provider's own name is passed. A literal at any of these is a
# second spelling of LABEL.
_NAME_POSITIONAL = {"error_dict": 0, "parse_error_dict": 0}
_NAME_KEYWORDS = ("provider_label", "label")


def _hardcoded_name_sites(module):
    """Every site in *module* passing a string literal where ``LABEL`` belongs."""
    tree = ast.parse(pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attr = node.func.attr if isinstance(node.func, ast.Attribute) else None
        index = _NAME_POSITIONAL.get(attr)
        if (
            index is not None
            and len(node.args) > index
            and isinstance(node.args[index], ast.Constant)
        ):
            bad.append(f"{attr}(...) line {node.lineno}")
        for kw in node.keywords:
            if kw.arg in _NAME_KEYWORDS and isinstance(kw.value, ast.Constant):
                bad.append(f"{kw.arg}= line {kw.value.lineno}")
    return bad


class TestTheProviderNameHasOneHome:
    """Four sites name the provider and must agree. Three take the name as a
    plain argument, so only a source scan can compare them; ``net/test_stats.py``
    pins the fourth (``Throttle(label=)``) at runtime."""

    @pytest.mark.parametrize(("name", "module"), _ALL_CLIENTS)
    def test_module_defines_a_label(self, name, module):
        assert isinstance(getattr(module, "LABEL", None), str) and module.LABEL, (
            f"{name} holds a client but no LABEL"
        )

    @pytest.mark.parametrize(("name", "module"), _ALL_CLIENTS)
    def test_no_site_hardcodes_the_provider_name(self, name, module):
        sites = _hardcoded_name_sites(module)
        assert not sites, f"{name} spells its own name instead of LABEL at: {sites}"


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

    @pytest.mark.parametrize("blank", ["   ", "\t", "\n"])
    def test_a_blank_mailto_does_not_buy_the_polite_tier(self, monkeypatch, blank):
        """The pool and the header must agree on what "configured" means.

        ``normalize_mailto`` strips, so a whitespace-only address never reaches
        the User-Agent. When ``config.get`` did not strip, the same value was
        still truthy here — so we claimed the polite tier's rates while sending
        no contact at all, the exact failure this class exists to prevent.
        """
        monkeypatch.setenv("CROSSREF_MAILTO", blank)
        assert crossref.in_polite_pool() is False
        assert crossref._resolve_policy() == (1, pytest.approx(0.2), pytest.approx(1.0))
        assert "mailto:" not in crossref._build_headers()["User-Agent"]

    def test_search_is_paced_separately_from_singles(self):
        # Search used to share the singles throttle entirely, so its tighter
        # limit was never enforced in either tier. This pins the *policy* — that
        # the two gaps are ordered. That `search_works` actually goes through
        # the search gate, and that the gate sleeps, is behaviour, and lives
        # with the rest of the module's behaviour in providers/test_crossref.py.
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
        assert http._retry_after_seconds(_response_with_retry_after("120")) == 120.0

    def test_http_date_form(self):
        when = datetime.now(UTC) + timedelta(seconds=120)
        got = http._retry_after_seconds(
            _response_with_retry_after(format_datetime(when, usegmt=True))
        )
        assert got is not None
        assert 110 < got <= 121

    def test_http_date_in_the_past_is_ignored(self):
        when = datetime.now(UTC) - timedelta(seconds=60)
        assert (
            http._retry_after_seconds(
                _response_with_retry_after(format_datetime(when, usegmt=True))
            )
            is None
        )

    def test_naive_date_is_read_as_utc_not_local_time(self):
        # A naive value read as local time would shift the wait by the host's
        # UTC offset — hours, in either direction.
        when = datetime.now(UTC) + timedelta(seconds=300)
        raw = when.strftime("%a, %d %b %Y %H:%M:%S")
        got = http._retry_after_seconds(_response_with_retry_after(raw))
        assert got is not None
        assert 280 < got <= 301

    @pytest.mark.parametrize("value", ["inf", "nan", "-inf"])
    def test_non_finite_values_are_rejected(self, value):
        assert http._retry_after_seconds(_response_with_retry_after(value)) is None

    @pytest.mark.parametrize("value", ["0", "-5", "garbage", "", None])
    def test_unusable_values_fall_back_to_our_own_backoff(self, value):
        assert http._retry_after_seconds(_response_with_retry_after(value)) is None

    def test_whitespace_is_tolerated(self):
        assert http._retry_after_seconds(_response_with_retry_after("  90  ")) == 90.0


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
        result = http.error_dict("Crossref", exc)
        assert result["retry_after_seconds"] == http._MAX_RETRY_AFTER_SECONDS

    def test_reasonable_value_passes_through(self):
        exc = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://x"),
            response=_response_with_retry_after("30"),
        )
        assert http.error_dict("Crossref", exc)["retry_after_seconds"] == 30.0

    def test_http_date_now_reaches_the_agent(self):
        when = datetime.now(UTC) + timedelta(seconds=45)
        exc = httpx.HTTPStatusError(
            "429",
            request=httpx.Request("GET", "https://x"),
            response=_response_with_retry_after(format_datetime(when, usegmt=True)),
        )
        # Previously omitted entirely: the agent got no hint at all.
        assert "retry_after_seconds" in http.error_dict("Crossref", exc)


class TestStatsAccuracy:
    """``stats`` is what an operator reads to audit outbound volume and cache
    effectiveness, so both counters being wrong mattered.
    """

    @pytest.mark.asyncio
    async def test_http_calls_counts_every_attempt_not_every_slot(self, monkeypatch):
        # http_calls was incremented once per throttle slot, but a slot issues
        # up to retry_attempts real requests (3 for arXiv) — under-reporting
        # actual outbound volume by up to 3x.
        from academic_tools_mcp.net import stats
        from academic_tools_mcp.net.throttle import Throttle

        stats.reset()
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

        monkeypatch.setattr(http.asyncio, "sleep", _noop_sleep)
        await throttle.get(StubClient(), "https://example.org/x")

        assert attempts == 3
        assert stats.snapshot()["providers"]["probe"]["http_calls"] == 3

    @pytest.mark.asyncio
    async def test_streaming_download_still_counts_one_call(self, monkeypatch):
        # PDF downloads hold the slot directly and never reach get_with_retry,
        # so moving the counter must not drop them entirely.
        from academic_tools_mcp.net import stats
        from academic_tools_mcp.net.throttle import Throttle

        stats.reset()
        throttle = Throttle(namespace="probe", label="Probe", max_concurrent=1, min_gap_seconds=0.0)
        async with throttle.slot("https://example.org/x.pdf"):
            pass

        assert stats.snapshot()["providers"]["probe"]["http_calls"] == 1

    def test_a_single_miss_is_counted_once(self, tmp_path):
        # cached_lookup checks the cache twice (outer, then again inside the
        # single-flight slot), so one genuine miss registered two misses while
        # a hit registered one — making the reported hit rate wrong.
        import asyncio

        from academic_tools_mcp.net import stats
        from academic_tools_mcp.store import cache, singleflight

        stats.reset()

        async def fetch():
            return {"ok": True}

        asyncio.run(
            cache.cached_lookup(
                single_flight=singleflight.SingleFlight(),
                namespace="probe",
                entity="things",
                canonical="k",
                fetch=fetch,
                positive_ttl=999.0,
            )
        )

        counters = stats.snapshot()["providers"]["probe"]
        assert counters["cache_misses"] == 1, counters
        assert counters.get("cache_hits", 0) == 0

    def test_count_false_suppresses_the_hit_counter(self, tmp_path):
        """A warming probe reads to decide whether to overwrite; it is not a
        lookup being served, so it must not show up as one."""
        from academic_tools_mcp.net import stats
        from academic_tools_mcp.store import cache

        stats.reset()
        cache.put("probe", "things", "present", {"a": 1})
        cache.get("probe", "things", "present", count=False)
        cache.get("probe", "things", "absent", count=False)

        assert stats.snapshot()["providers"].get("probe", {}) == {}


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
        assert openaccess._throttle.per_host is True
        assert openaccess._MIN_REQUEST_GAP >= 1.0

    def test_concurrency_stays_global(self):
        # max_concurrent bounds *our* egress — sockets, fds, and simultaneous
        # in-flight streams (stream_to_file holds the slot for the whole
        # download). Making it per-host would let a 20-publisher walk open 40
        # parallel streams, however polite that is to each publisher.
        assert openaccess._MAX_CONCURRENT <= 4

    def test_opencitations_honours_its_documented_rate(self):
        # OpenCitations documents 180 requests/minute = 3/sec. The gap is the
        # only thing enforcing it, and the number in the module is a comment
        # rather than a derivation, so a hand-edit that widens it is silent.
        assert opencitations._MIN_REQUEST_GAP >= 60.0 / 180.0

    def test_opencitations_can_fetch_both_directions_at_once(self):
        # The concurrency cap exists so a graph traversal's references and
        # citations fetches overlap rather than serialise; below 2 they can't,
        # and above it we exceed what the comment claims to be conservative.
        assert opencitations._MAX_CONCURRENT == 2

    @pytest.mark.parametrize(
        ("name", "module"),
        [(n, m) for n, m in _ALL_CLIENTS if n != "openaccess"],
    )
    def test_api_providers_stay_globally_paced(self, name, module):
        # per_host is for a client whose URLs are not one API. Each of these
        # talks to exactly one host, where the map would be a dict of size one
        # — and opting one in would silently widen its documented rate the day
        # it gained a second hostname.
        assert module._throttle.per_host is False, f"{name} should not be per-host paced"
