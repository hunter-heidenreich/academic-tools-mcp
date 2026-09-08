"""Unit tests for ``providers/opencitations.py``.

The transport seam is a real ``httpx.MockTransport`` patched over
``clients.get_client`` (as ``test_openalex.py`` and ``test_biorxiv.py`` do)
rather than a stub over the client: ``status_code``, ``raise_for_status`` and
``.json()`` are then genuine, so ``http.HTTPX_ERRORS`` is reachable and the
throttle and retry the provider actually uses stay in the path.
"""

import asyncio
import json
from typing import Any, NamedTuple
from urllib.parse import unquote, urlsplit

import httpx
import pytest

from academic_tools_mcp.net import clients
from academic_tools_mcp.providers import opencitations
from academic_tools_mcp.store import cache

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

# Sentinel: a 200 whose body is not JSON, simulating a malformed/truncated body.
_BAD_JSON = object()

# The two directions, and the record field naming the other end of each link.
_DIRECTIONS = [
    (opencitations.get_references, "references", "cited"),
    (opencitations.get_citations, "citations", "citing"),
]


class _Resp(NamedTuple):
    """A payload with a non-200 status, for the HTTP-error branches."""

    status_code: int
    payload: Any = None
    headers: dict[str, str] | None = None


def _reset_opencitations(monkeypatch):
    """Zero the throttle gap and the retry so a multi-request test doesn't wait.

    Cache root, pooled clients and single-flight state are already reset per
    test by the conftest autouse fixtures; only pacing is this module's
    business. ``retry_attempts=1`` matters for the retryable-status tests,
    which would otherwise sleep out ``get_with_retry``'s one-second backoff.
    """
    monkeypatch.setattr(opencitations._throttle, "min_gap_seconds", 0.0)
    monkeypatch.setattr(opencitations._throttle, "retry_attempts", 1)


def _stub_json_responses(monkeypatch, *payloads, slow=False):
    """Serve ``payloads`` from successive GETs over a real MockTransport.

    A payload may be ``_BAD_JSON``, a ``_Resp`` carrying a status code, or an
    exception to raise; the last one repeats once the sequence is exhausted.
    ``slow=True`` yields to the event loop before answering, so concurrent
    callers actually overlap and single-flight has something to coalesce.
    Returns the list of captured ``httpx.Request``s.
    """
    _reset_opencitations(monkeypatch)
    requests: list[httpx.Request] = []
    seq = list(payloads)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = seq[len(requests)] if len(requests) < len(seq) else seq[-1]
        requests.append(request)
        if isinstance(payload, Exception):
            raise payload
        status, headers = 200, {"content-type": "application/json"}
        if isinstance(payload, _Resp):
            status = payload.status_code
            headers = {**headers, **(payload.headers or {})}
            payload = payload.payload
        body = b"{ truncated" if payload is _BAD_JSON else json.dumps(payload).encode()
        return httpx.Response(status, headers=headers, content=body)

    async def slow_respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return respond(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow_respond if slow else respond))
    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: client)
    return requests


def _stub_no_network(monkeypatch):
    """Install a transport that fails the test if anything reaches it."""
    _reset_opencitations(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: client)


def _record(id_field, other_doi, **overrides):
    """One OpenCitations link record, in the shape the live API returns."""
    return {
        "oci": "06120344846-06802139111",
        id_field: f"omid:br/01 doi:{other_doi} openalex:W1 pmid:1",
        "creation": "2020-01-01",
        "journal_sc": "no",
        "author_sc": "no",
        **overrides,
    }


def _references_response(cited_doi="10.5555/cited"):
    """A minimal valid OpenCitations /references response (a list of records)."""
    return [_record("cited", cited_doi)]


def _citations_response(citing_doi="10.5555/citing"):
    """A minimal valid OpenCitations /citations response (a list of records)."""
    return [_record("citing", citing_doi)]


# ---------------------------------------------------------------------------
# ID parsing
# ---------------------------------------------------------------------------


class TestParseIdsNonString:
    """The field comes from untyped JSON; a non-string reaches ``.split()``."""

    @pytest.mark.parametrize("raw", [True, 5, ["doi:10.1/a"], {"doi": "10.1/a"}, 1.5, None])
    def test_a_non_string_id_field_yields_no_ids(self, raw):
        assert opencitations._parse_ids(raw) == {}

    @pytest.mark.asyncio
    async def test_a_record_with_a_non_string_id_field_is_not_fatal(self, monkeypatch):
        _stub_json_responses(monkeypatch, [{"cited": 7, "creation": "2020"}])

        result = await opencitations.get_references("10.1234/x")

        assert result["count"] == 1
        assert result["references"][0]["creation"] == "2020"


class TestParseIds:
    def test_full_string(self):
        result = opencitations._parse_ids(
            "omid:br/062102024238 doi:10.1103/physrevx.2.031001 openalex:W3101024234 pmid:20079334"
        )
        assert result == {
            "omid": "br/062102024238",
            "doi": "10.1103/physrevx.2.031001",
            "openalex": "W3101024234",
            "pmid": "20079334",
        }

    def test_doi_only(self):
        result = opencitations._parse_ids("doi:10.1038/nature12373")
        assert result == {"doi": "10.1038/nature12373"}

    def test_empty_string(self):
        assert opencitations._parse_ids("") == {}

    def test_whitespace_only(self):
        assert opencitations._parse_ids("   \n ") == {}

    def test_a_token_without_a_colon_is_dropped(self):
        assert opencitations._parse_ids("omid br/01 doi:10.1/a") == {"doi": "10.1/a"}

    def test_a_repeated_prefix_takes_the_last_token(self):
        # Not a behaviour anyone chose — pinned so a change to it is visible.
        # No live record carries two tokens of one prefix.
        assert opencitations._parse_ids("doi:10.1/a doi:10.1/b") == {"doi": "10.1/b"}

    def test_a_value_keeps_everything_after_the_first_colon(self):
        assert opencitations._parse_ids("omid:br/06:1") == {"omid": "br/06:1"}

    def test_an_empty_value_is_dropped(self):
        # `_format_record` flattens these to the record's top level and
        # tools/graph.py forwards them verbatim, so a blank `doi` would read
        # to an agent as a real identifier to chain into another tool.
        assert opencitations._parse_ids("doi: omid:br/01") == {"omid": "br/01"}


# ---------------------------------------------------------------------------
# Record formatting
# ---------------------------------------------------------------------------


class TestFormatRecord:
    """The per-entry shape ``get_paper_references`` / ``get_paper_citations``
    promise the agent: flattened IDs, creation date, two self-citation flags."""

    def test_the_documented_keys_are_all_present(self):
        record = opencitations._format_record(_record("cited", "10.5555/c"), "cited")

        assert record == {
            "omid": "br/01",
            "doi": "10.5555/c",
            "openalex": "W1",
            "pmid": "1",
            "creation": "2020-01-01",
            "journal_self_citation": False,
            "author_self_citation": False,
        }

    @pytest.mark.parametrize(
        ("journal_sc", "author_sc", "journal_flag", "author_flag"),
        [
            ("yes", "no", True, False),
            ("no", "yes", False, True),
            ("yes", "yes", True, True),
        ],
    )
    def test_yes_is_the_only_true(self, journal_sc, author_sc, journal_flag, author_flag):
        raw = _record("cited", "10.5555/c", journal_sc=journal_sc, author_sc=author_sc)
        record = opencitations._format_record(raw, "cited")

        assert record["journal_self_citation"] is journal_flag
        assert record["author_self_citation"] is author_flag

    @pytest.mark.parametrize("value", ["YES", "Yes", "", None, True, 1])
    def test_anything_but_the_literal_yes_is_false(self, value):
        # Upstream emits exactly "yes"/"no"; a bool must come out of this, so
        # every other spelling degrades to False rather than leaking through.
        raw = _record("cited", "10.5555/c", journal_sc=value, author_sc=value)
        record = opencitations._format_record(raw, "cited")

        assert record["journal_self_citation"] is False
        assert record["author_self_citation"] is False

    def test_a_missing_creation_date_is_none_not_absent(self):
        raw = {"cited": "doi:10.5555/c"}
        assert opencitations._format_record(raw, "cited")["creation"] is None


# ---------------------------------------------------------------------------
# Malformed and anomalous bodies
# ---------------------------------------------------------------------------


class TestParseErrors:
    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, monkeypatch, fetch, kind, _field):
        # A 200 with a garbled body must NOT crash — it should surface the
        # uniform {error, retryable} contract like every other failure.
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await fetch("10.1234/x")

        assert isinstance(result, dict)
        assert "error" in result
        assert result["retryable"] is True
        assert "not_found" not in result

    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_malformed_json_is_not_cached(self, monkeypatch, fetch, kind, field):
        # A parse failure is transient — a retry must re-fetch and succeed,
        # not be served a poisoned negative-cache entry.
        good = [_record(field, "10.5555/other")]
        requests = _stub_json_responses(monkeypatch, _BAD_JSON, good)

        assert "error" in await fetch("10.1234/x")
        assert (await fetch("10.1234/x"))["count"] == 1
        assert len(requests) == 2  # re-fetched, not cached


class TestMalformedPayloadShape:
    """A 200 that isn't the expected list of records is a parse failure, not an
    empty result: reported as "no references" it ends the agent's search."""

    @pytest.mark.parametrize("payload", [None, 5, "a string", True, {"status": "ok"}])
    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_a_non_list_body_errors_and_is_not_cached(
        self, monkeypatch, fetch, kind, field, payload
    ):
        good = [_record(field, "10.5555/other")]
        requests = _stub_json_responses(monkeypatch, payload, good)

        first = await fetch("10.1234/x")
        assert first["retryable"] is True
        assert kind not in first

        assert (await fetch("10.1234/x"))["count"] == 1
        assert len(requests) == 2

    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_non_dict_entries_are_skipped_and_not_counted(
        self, monkeypatch, fetch, kind, field
    ):
        # `_format_record` calls `.get` on each entry: an AttributeError here
        # is in neither _PARSE_ERRORS nor HTTPX_ERRORS, so it would escape the
        # provider as a traceback. `count` must follow the kept records, not
        # the raw list.
        body = [_record(field, "10.5555/a"), "junk", None, 5, [], _record(field, "10.5555/b")]
        _stub_json_responses(monkeypatch, body)

        result = await fetch("10.1234/x")

        assert result["count"] == 2
        assert [entry["doi"] for entry in result[kind]] == ["10.5555/a", "10.5555/b"]
        assert all(isinstance(entry, dict) for entry in result[kind])


class TestEmptyResult:
    """An unknown-but-well-formed DOI answers 200 with an empty list, not 404 —
    OpenCitations cannot tell "never indexed" from "indexed with zero edges",
    so an empty list is a real answer and is cached like any other."""

    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_an_empty_list_is_a_zero_count_success(self, monkeypatch, fetch, kind, _field):
        _stub_json_responses(monkeypatch, [])

        result = await fetch("10.1234/x")

        assert result == {kind: [], "count": 0}
        assert "error" not in result
        assert "not_found" not in result

    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_an_empty_list_is_positive_cached(self, monkeypatch, fetch, kind, _field):
        requests = _stub_json_responses(monkeypatch, [])

        await fetch("10.1234/x")
        assert (await fetch("10.1234/x"))["count"] == 0
        assert len(requests) == 1

        assert cache.get(opencitations.NAMESPACE, kind, "10.1234/x") == {kind: [], "count": 0}
        assert cache.get_negative(opencitations.NAMESPACE, kind, "10.1234/x") is None


# ---------------------------------------------------------------------------
# The request path
# ---------------------------------------------------------------------------


class TestRequestPath:
    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_reserved_characters_are_percent_encoded(self, monkeypatch, fetch, kind, field):
        # A DOI containing '#' must be percent-encoded in the path; otherwise
        # httpx treats '#bar' as a fragment and requests the edges of the
        # wrong record (which then gets positive-cached).
        requests = _stub_json_responses(monkeypatch, [_record(field, "10.5555/other")])

        await fetch("10.1234/foo#bar")

        url = str(requests[0].url)
        assert "%23" in url
        assert "#" not in url

    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_the_doi_prefix_and_the_dois_own_slash_stay_literal(
        self, monkeypatch, fetch, kind, field
    ):
        requests = _stub_json_responses(monkeypatch, [_record(field, "10.5555/other")])

        await fetch("10.1038/nature12373")

        assert str(requests[0].url).endswith(f"/{kind}/doi:10.1038/nature12373")


class TestPathShorteningIsRefused:
    """A `.`/`..` segment is *removed* by RFC 3986 resolution after encoding —
    both characters are unreserved, so no `quote` escapes them. The shortened
    path answers 200 with a valid empty list, which would then cache as this
    DOI's edges for the full positive TTL."""

    @pytest.mark.parametrize("doi", ["10.1234/..", "10.1038/a/..", "10.1234/.", "10.1234/a/../b"])
    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_it_is_a_definitive_miss_costing_no_request(
        self, monkeypatch, fetch, kind, _field, doi
    ):
        _stub_no_network(monkeypatch)  # any outbound request fails the test

        result = await fetch(doi)

        assert result["not_found"] is True
        assert doi in result["error"]
        assert "retryable" not in result

    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_nothing_is_cached_for_it(self, monkeypatch, fetch, kind, _field):
        # Uncached on purpose: no request was spent, so nothing was learned
        # about the identifier that is worth holding for the negative TTL.
        _stub_no_network(monkeypatch)

        await fetch("10.1234/..")

        assert cache.get_negative(opencitations.NAMESPACE, kind, "10.1234/..") is None
        assert cache.get(opencitations.NAMESPACE, kind, "10.1234/..") is None

    @pytest.mark.asyncio
    async def test_an_escaped_dot_segment_is_a_normal_identifier(self, monkeypatch):
        # The guard reads the *encoded* path, so a DOI whose suffix is a
        # literal '%2E%2E' addresses a real record and must not be refused.
        requests = _stub_json_responses(monkeypatch, [])

        result = await opencitations.get_references("10.1234/%2E%2E")

        assert "error" not in result
        assert len(requests) == 1


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


class TestForceRefresh:
    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_it_drops_the_positive_entry(self, monkeypatch, fetch, kind, field):
        requests = _stub_json_responses(monkeypatch, [_record(field, "10.5555/other")])

        await fetch("10.1234/x")
        assert len(requests) == 1

        # Plain re-read is a cache hit — no new HTTP call.
        await fetch("10.1234/x")
        assert len(requests) == 1

        await fetch("10.1234/x", force_refresh=True)
        assert len(requests) == 2

    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_it_drops_the_negative_entry(self, monkeypatch, fetch, kind, field):
        # The half a plain re-read would be served from: without the
        # invalidate, a DOI that 404'd once stays absent for the negative TTL
        # however hard the agent asks for it fresh.
        good = [_record(field, "10.5555/other")]
        requests = _stub_json_responses(monkeypatch, _Resp(404), good)

        assert (await fetch("10.1234/x"))["not_found"] is True
        assert (await fetch("10.1234/x"))["not_found"] is True
        assert len(requests) == 1

        assert (await fetch("10.1234/x", force_refresh=True))["count"] == 1
        assert len(requests) == 2


class TestCacheKeying:
    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_every_doi_spelling_shares_one_entry(self, monkeypatch, fetch, kind, field):
        requests = _stub_json_responses(monkeypatch, [_record(field, "10.5555/other")])

        await fetch("10.1038/Nature12373")
        for spelling in (
            "10.1038/nature12373",
            "doi:10.1038/NATURE12373",
            "  https://doi.org/10.1038/nature12373  ",
            "http://dx.doi.org/10.1038/Nature12373",
        ):
            assert (await fetch(spelling))["count"] == 1

        assert len(requests) == 1

    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_the_negative_entry_is_keyed_canonically(self, monkeypatch, fetch, kind, _field):
        requests = _stub_json_responses(monkeypatch, _Resp(404))

        assert (await fetch("10.1234/MISSING"))["not_found"] is True
        assert (await fetch("https://doi.org/10.1234/missing"))["not_found"] is True

        assert len(requests) == 1

    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_every_caller_gets_an_independent_copy(self, monkeypatch, fetch, kind, field):
        # tools/graph.py's `_enrich_error` mutates the provider's result in
        # place; this deep copy is what keeps that from reaching the cache.
        _stub_json_responses(monkeypatch, [_record(field, "10.5555/other")])

        first = await fetch("10.1234/x")
        first[kind].clear()
        first["count"] = 99

        second = await fetch("10.1234/x")
        assert second["count"] == 1
        assert len(second[kind]) == 1


class TestSingleFlight:
    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_fetch(self, monkeypatch, fetch, kind, field):
        requests = _stub_json_responses(monkeypatch, [_record(field, "10.5555/other")], slow=True)

        results = await asyncio.gather(*(fetch("10.1234/x") for _ in range(5)))

        assert len(requests) == 1
        assert all(r["count"] == 1 for r in results)

    @pytest.mark.asyncio
    async def test_the_two_directions_are_two_in_flight_slots(self, monkeypatch):
        # One SingleFlight instance serves both directions, so the key has to
        # carry the kind: keyed on the canonical DOI alone, a concurrent
        # references+citations fetch would collapse into one request and the
        # follower would be served the other direction's payload.
        requests = _stub_json_responses(
            monkeypatch,
            [_record("cited", "10.5555/cited")],
            [_record("citing", "10.5555/citing")],
            slow=True,
        )

        refs, cites = await asyncio.gather(
            opencitations.get_references("10.1234/x"),
            opencitations.get_citations("10.1234/x"),
        )

        assert len(requests) == 2
        assert {urlsplit(str(r.url)).path.split("/")[3] for r in requests} == {
            "references",
            "citations",
        }
        assert refs["references"][0]["doi"] == "10.5555/cited"
        assert cites["citations"][0]["doi"] == "10.5555/citing"


# ---------------------------------------------------------------------------
# 404: the definitive miss and its negative-cache entry
# ---------------------------------------------------------------------------


class TestNotFound:
    """The one branch writing a durable negative entry, and the only one that
    can carry `not_found` — the flag tools/graph.py forwards so an agent can
    tell "no edges for this DOI" from "OpenCitations was briefly unreachable".
    Rare in practice: an unknown DOI answers 200 with an empty list, so this
    covers a malformed path or an upstream route change."""

    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_404_is_a_definitive_not_found(self, monkeypatch, fetch, kind, _field):
        _stub_json_responses(monkeypatch, _Resp(404))

        result = await fetch("10.1234/missing")

        assert kind in result["error"]
        assert result["not_found"] is True
        assert "retryable" not in result  # definitive

    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_404_is_negative_cached(self, monkeypatch, fetch, kind, _field):
        requests = _stub_json_responses(monkeypatch, _Resp(404))

        first = await fetch("10.1234/missing")
        second = await fetch("10.1234/missing")

        assert first == second
        assert second["not_found"] is True
        assert len(requests) == 1
        assert cache.get_negative(opencitations.NAMESPACE, kind, "10.1234/missing") is not None

    @pytest.mark.asyncio
    async def test_the_two_directions_cache_separately(self, monkeypatch):
        """The negative entry is keyed by direction, not by DOI alone."""
        requests = _stub_json_responses(monkeypatch, _Resp(404))

        await opencitations.get_references("10.1234/missing")
        await opencitations.get_citations("10.1234/missing")

        assert len(requests) == 2


# ---------------------------------------------------------------------------
# Transport and HTTP status failures
# ---------------------------------------------------------------------------


class TestTransportErrors:
    """A network failure or a server-side error says nothing about the DOI:
    same retryable contract as an unparseable body, and never negative-cached."""

    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_a_connect_error_is_retryable(self, monkeypatch, fetch, kind, _field):
        _stub_json_responses(monkeypatch, httpx.ConnectError("no route"))

        result = await fetch("10.1234/x")

        assert "error" in result
        assert result["retryable"] is True
        assert "not_found" not in result

    @pytest.mark.parametrize("status", [500, 502, 503])
    @pytest.mark.parametrize(("fetch", "kind", "field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_a_server_error_is_retryable_and_uncached(
        self, monkeypatch, fetch, kind, field, status
    ):
        good = [_record(field, "10.5555/other")]
        requests = _stub_json_responses(monkeypatch, _Resp(status), good)

        first = await fetch("10.1234/x")
        assert first["retryable"] is True
        assert "not_found" not in first

        assert (await fetch("10.1234/x"))["count"] == 1
        assert len(requests) == 2

    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_a_429_carries_the_servers_retry_after(self, monkeypatch, fetch, kind, _field):
        _stub_json_responses(monkeypatch, _Resp(429, headers={"Retry-After": "30"}))

        result = await fetch("10.1234/x")

        assert result["retryable"] is True
        assert result["retry_after_seconds"] == 30.0

    @pytest.mark.parametrize(("fetch", "kind", "_field"), _DIRECTIONS)
    @pytest.mark.asyncio
    async def test_local_backpressure_names_opencitations(self, monkeypatch, fetch, kind, _field):
        # A local refusal reaches the agent through the same contract as an
        # upstream failure, under the throttle's human-facing label.
        _stub_no_network(monkeypatch)
        monkeypatch.setattr(opencitations._throttle, "max_pending", 0)

        result = await fetch("10.1234/x")

        assert result["backpressure"] is True
        assert result["retryable"] is True
        assert "OpenCitations" in result["error"]


# ---------------------------------------------------------------------------
# Identifier round-trip
# ---------------------------------------------------------------------------


class TestNormalizedIdentifierReachesThePath:
    @pytest.mark.parametrize(
        "spelling",
        [
            "10.1038/nature12373",
            "doi:10.1038/nature12373",
            "DOI: 10.1038/nature12373",
            "https://doi.org/10.1038/nature12373",
            "  10.1038/nature12373  ",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_url_or_prefixed_spelling_is_bare_in_the_path(self, monkeypatch, spelling):
        # A spelling that reached the path unnormalized would build
        # `.../doi:https://doi.org/10.1038/...` and fetch nothing.
        requests = _stub_json_responses(monkeypatch, [])

        await opencitations.get_references(spelling, force_refresh=True)

        path = unquote(urlsplit(str(requests[-1].url)).path)
        assert path.endswith("/references/doi:10.1038/nature12373")
