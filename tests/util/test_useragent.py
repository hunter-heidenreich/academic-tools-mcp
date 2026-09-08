"""The shared outbound User-Agent: shape, version source, contact scrubbing.

The contact address is the one operator-supplied string this codebase
interpolates into a header, so most of what is interesting here is what
happens to a malformed one.
"""

from importlib.metadata import PackageNotFoundError, version

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp.util import useragent


@pytest.fixture(autouse=True)
def _clear_version_cache():
    useragent.package_version.cache_clear()
    yield
    useragent.package_version.cache_clear()


class TestPackageVersion:
    def test_reports_the_installed_distribution_version(self):
        assert useragent.package_version() == version("academic-tools-mcp")

    def test_falls_back_to_a_clear_placeholder_when_not_installed(self, monkeypatch):
        def raise_not_found(_name):
            raise PackageNotFoundError(_name)

        monkeypatch.setattr(useragent, "version", raise_not_found)
        assert useragent.package_version() == "0+unknown"

    def test_is_cached(self, monkeypatch):
        # Every provider's _get_client rebuilds its headers per request, so an
        # uncached lookup puts an importlib.metadata sys.path scan on the hot
        # path -- ~90% of the cost of a pooled-client lookup.
        calls = 0

        def counting_version(name):
            nonlocal calls
            calls += 1
            return "1.2.3"

        monkeypatch.setattr(useragent, "version", counting_version)
        for _ in range(5):
            useragent.package_version()
        assert calls == 1


class TestAgentShape:
    def test_carries_the_real_version_not_a_literal(self):
        assert useragent.build().startswith(f"academic-tools-mcp/{version('academic-tools-mcp')} (")

    def test_advertises_the_project_url(self):
        assert "+https://github.com/hunter-heidenreich/academic-tools-mcp" in useragent.build()

    def test_is_descriptive_without_a_contact(self):
        ua = useragent.build(None)
        assert ua.startswith("academic-tools-mcp/")
        assert "mailto:" not in ua
        assert ua.endswith(")")

    def test_appends_a_configured_contact(self):
        assert useragent.build("me@example.org").endswith("; mailto:me@example.org)")


class TestHeaders:
    def test_returns_exactly_the_user_agent(self):
        assert useragent.headers("me@example.org") == {
            "User-Agent": useragent.build("me@example.org")
        }

    def test_no_contact_still_yields_one_header(self):
        assert list(useragent.headers()) == ["User-Agent"]


class TestContactNormalization:
    @pytest.mark.parametrize("value", [None, "", "   ", "\t\n", "()"])
    def test_unusable_contacts_are_dropped_entirely(self, value):
        # A whitespace-only WIKIPEDIA_MAILTO must not emit a bare "mailto:".
        assert useragent.normalize_mailto(value) is None
        assert "mailto:" not in useragent.build(value)

    def test_surrounding_whitespace_is_stripped(self):
        # Belt and braces: config.get already strips, but normalize_mailto
        # takes contacts from callers too, and a padded one is a typo rather
        # than a request for a padded header.
        assert useragent.build("  me@x.org  ").endswith("; mailto:me@x.org)")

    @pytest.mark.parametrize(
        "value",
        ["mailto:me@x.org", "MAILTO:me@x.org", "mailto: mailto:me@x.org"],
    )
    def test_a_redundant_scheme_prefix_is_stripped(self, value):
        # CROSSREF_MAILTO=mailto:me@x.org is a plausible .env entry; doubling
        # the scheme is the same class of bug doinorm.normalize strips for "doi:".
        assert useragent.build(value).endswith("; mailto:me@x.org)")

    def test_scrubbing_runs_before_prefix_stripping(self):
        # Scrubbing can reveal a prefix, so the other order is not idempotent.
        assert useragent.normalize_mailto("mail(to:me@x.org") == "me@x.org"

    def test_crlf_cannot_inject_a_header(self):
        # httpx accepts this at construction and only fails at send time, as a
        # RequestError -- so every request would degrade to a misleading
        # "network error" dict for the life of the process.
        ua = useragent.build("me@x.org\r\nX-Evil: 1")
        assert "\r" not in ua and "\n" not in ua
        assert ua.endswith("; mailto:me@x.orgX-Evil: 1)")

    def test_non_ascii_cannot_crash_client_construction(self):
        # An unscrubbed non-ASCII contact raises UnicodeEncodeError *inside
        # httpx*, which is not in HTTPX_ERRORS -- an uncaught crash on the
        # first request, not an {error} dict.
        client = httpx.AsyncClient(headers=useragent.headers("mé@x.org"))
        assert client.headers["user-agent"].endswith("; mailto:m@x.org)")


_CONTACTS = st.one_of(st.none(), st.text(max_size=60))


class TestProperties:
    @given(_CONTACTS)
    def test_agent_is_always_a_safe_header_value(self, mailto):
        ua = useragent.build(mailto)
        assert all(0x20 <= ord(c) <= 0x7E for c in ua)
        httpx.Headers({"User-Agent": ua})  # must not raise

    @given(_CONTACTS)
    def test_agent_is_always_one_balanced_comment(self, mailto):
        ua = useragent.build(mailto)
        assert ua.startswith(f"academic-tools-mcp/{useragent.package_version()} (+")
        assert ua.endswith(")")
        assert ua.count("(") == 1
        assert ua.count(")") == 1

    @given(_CONTACTS)
    def test_normalization_is_idempotent(self, mailto):
        once = useragent.normalize_mailto(mailto)
        assert useragent.normalize_mailto(once) == once
        assert useragent.build(once) == useragent.build(mailto)
