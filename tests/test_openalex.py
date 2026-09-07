"""Unit tests for ``providers/openalex.py``.

The transport seam is a real ``httpx.MockTransport`` patched over
``_clients.get_client`` (as ``test_biorxiv.py`` does) rather than a stub over
``_throttled_get``: ``status_code``, ``raise_for_status`` and ``.json()`` are
then genuine, so ``_http.HTTPX_ERRORS`` is reachable and the throttle and retry
the provider actually uses stay in the path.
"""

import asyncio
import json
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from academic_tools_mcp import _clients, _stats, cache
from academic_tools_mcp.providers import openalex
from academic_tools_mcp.providers.openalex import (
    _canonical_from_response_doi,
    _normalize_author_id,
    canonical_author_id,
    reconstruct_abstract,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

# Sentinel: a 200 whose body is not JSON, simulating a malformed/truncated body.
_BAD_JSON = object()


class _Resp(NamedTuple):
    """A payload with a non-200 status, for the HTTP-error branches."""

    status_code: int
    payload: Any = None


def _reset_openalex(monkeypatch):
    """Zero the throttle gap and the retry so a multi-request test doesn't wait.

    Cache root, pooled clients and single-flight state are already reset per
    test by the conftest autouse fixtures; only pacing is this module's
    business. ``retry_attempts=1`` matters for the retryable-status tests,
    which would otherwise sleep out ``get_with_retry``'s one-second backoff.
    """
    monkeypatch.setattr(openalex._throttle, "min_gap_seconds", 0.0)
    monkeypatch.setattr(openalex._throttle, "retry_attempts", 1)


def _stub_json_responses(monkeypatch, *payloads, slow=False):
    """Serve ``payloads`` from successive GETs over a real MockTransport.

    A payload may be ``_BAD_JSON``, a ``_Resp`` carrying a status code, or an
    exception to raise; the last one repeats once the sequence is exhausted.
    ``slow=True`` yields to the event loop before answering, so concurrent
    callers actually overlap and single-flight has something to coalesce.
    Returns the list of captured ``httpx.Request``s.
    """
    _reset_openalex(monkeypatch)
    requests: list[httpx.Request] = []
    seq = list(payloads)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = seq[len(requests)] if len(requests) < len(seq) else seq[-1]
        requests.append(request)
        if isinstance(payload, Exception):
            raise payload
        status = 200
        if isinstance(payload, _Resp):
            status, payload = payload.status_code, payload.payload
        body = b"{ truncated" if payload is _BAD_JSON else json.dumps(payload).encode()
        return httpx.Response(status, headers={"content-type": "application/json"}, content=body)

    async def slow_respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        return respond(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow_respond if slow else respond))
    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: client)
    return requests


def _stub_no_network(monkeypatch):
    """Install a transport that fails the test if anything reaches it."""
    _reset_openalex(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: client)


def _stub_filter_echo(monkeypatch, *, known=None):
    """Answer any ``/works?filter=doi:a|b`` with a work per requested DOI.

    ``known`` restricts which DOIs come back; the rest are genuine misses.
    Returns the list of captured requests.
    """
    _reset_openalex(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raw = parse_qs(request.url.query.decode())["filter"][0]
        asked = raw.removeprefix("doi:").split("|")
        results = [_work_response(d) for d in asked if known is None or d in known]
        body = {"meta": {"count": len(results)}, "results": results}
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: client)
    return requests


def _work_response(doi="10.1234/x"):
    """A minimal valid OpenAlex work object (returned directly, no wrapper)."""
    return {
        "id": "https://openalex.org/W1",
        "doi": f"https://doi.org/{doi}",
        "title": "A Great Work",
    }


def _author_response(author_id="A1"):
    """A minimal valid OpenAlex author object."""
    return {"id": f"https://openalex.org/{author_id}", "display_name": "Jane Doe"}


def _path_of(request: httpx.Request) -> str:
    """The raw (still percent-encoded) path of a captured request."""
    return urlsplit(str(request.url)).path


# ---------------------------------------------------------------------------
# Identifier normalization
# ---------------------------------------------------------------------------


class TestNormalizeAuthorId:
    def test_openalex_id(self):
        assert _normalize_author_id("A5023888391") == "A5023888391"

    def test_openalex_url(self):
        assert _normalize_author_id("https://openalex.org/A5023888391") == "A5023888391"

    @pytest.mark.parametrize(
        "spelling",
        [
            "https://openalex.org/A5023888391",
            "http://openalex.org/A5023888391",
            "//openalex.org/A5023888391".removeprefix("//"),  # bare host
            "openalex.org/A5023888391",
            "https://www.openalex.org/A5023888391",
            "https://api.openalex.org/authors/A5023888391",
            "HTTPS://OpenAlex.ORG/A5023888391",
            "https://openalex.org/A5023888391/",
            "  https://openalex.org/A5023888391  ",
        ],
    )
    def test_url_family_collapses_to_the_bare_id(self, spelling):
        """The latitude ``_doi._DOI_URL_RE`` and ``arxiv._ARXIV_URL_RE`` carry,
        for the same reason: these are the forms pasted citations hold."""
        assert _normalize_author_id(spelling) == "A5023888391"

    def test_orcid_url_passthrough(self):
        orcid = "https://orcid.org/0000-0001-6187-6610"
        assert _normalize_author_id(orcid) == orcid

    def test_unrecognised_string_is_returned_stripped(self):
        assert _normalize_author_id("  not-an-id  ") == "not-an-id"


class TestCanonicalAuthorId:
    def test_lowercases(self):
        assert canonical_author_id("A5023888391") == "a5023888391"

    def test_strips_url_and_lowercases(self):
        assert canonical_author_id("https://openalex.org/A5023888391") == "a5023888391"

    def test_every_url_spelling_shares_one_key(self):
        keys = {
            canonical_author_id(s)
            for s in (
                "A5023888391",
                "a5023888391",
                "https://openalex.org/A5023888391",
                "http://openalex.org/A5023888391/",
                "https://api.openalex.org/authors/A5023888391",
            )
        }
        assert keys == {"a5023888391"}

    def test_orcid_lowercased(self):
        assert (
            canonical_author_id("https://orcid.org/0000-0001-6187-6610")
            == "https://orcid.org/0000-0001-6187-6610"
        )


class TestCanonicalFromResponseDoi:
    """The response-side counterpart of ``canonical_doi``; a disagreement
    between the two is what makes a batch report a returned work as missing."""

    @pytest.mark.parametrize(
        "prefix",
        [
            "https://doi.org/",
            "http://doi.org/",
            "https://dx.doi.org/",
            "http://dx.doi.org/",
            "https://www.doi.org/",
            "HTTPS://DOI.ORG/",
        ],
    )
    def test_strips_every_resolver_prefix(self, prefix):
        assert _canonical_from_response_doi(f"{prefix}10.1234/Foo") == "10.1234/foo"

    def test_bare_doi_is_folded(self):
        assert _canonical_from_response_doi("10.1234/FOO") == "10.1234/foo"

    def test_strips_unconditionally_not_only_doi_shaped_tails(self):
        """``_doi.canonical`` strips only a DOI-shaped path, so an unrecognised
        registrant would survive as a URL and miss the key we asked for."""
        assert _canonical_from_response_doi("https://doi.org/weird/thing") == "weird/thing"

    def test_surrounding_whitespace(self):
        assert _canonical_from_response_doi("  https://doi.org/10.1/a  ") == "10.1/a"

    @pytest.mark.parametrize("bad", [None, "", "   ", 42, 3.5, [], {}, {"doi": "x"}, True])
    def test_anything_unusable_is_none_not_a_crash(self, bad):
        """The value comes from untyped JSON and this runs *after* the request's
        try block has closed — a raise here escapes the whole batch."""
        assert _canonical_from_response_doi(bad) is None


class TestBuildParams:
    def test_neither_configured(self, monkeypatch):
        assert openalex._build_params() == {}

    def test_api_key_only(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_API_KEY", "k1")
        assert openalex._build_params() == {"api_key": "k1"}

    def test_mailto_only(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_MAILTO", "a@b.example")
        assert openalex._build_params() == {"mailto": "a@b.example"}

    def test_both(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_API_KEY", "k1")
        monkeypatch.setenv("OPENALEX_MAILTO", "a@b.example")
        assert openalex._build_params() == {"api_key": "k1", "mailto": "a@b.example"}

    def test_blank_is_unset(self, monkeypatch):
        monkeypatch.setenv("OPENALEX_MAILTO", "   ")
        assert openalex._build_params() == {}


class TestReconstructAbstract:
    def test_simple(self):
        index = {"Hello": [0], "world": [1]}
        assert reconstruct_abstract(index) == "Hello world"

    def test_out_of_order(self):
        index = {"world": [1], "Hello": [0], "beautiful": [2]}
        assert reconstruct_abstract(index) == "Hello world beautiful"

    def test_repeated_words(self):
        index = {"the": [0, 2], "cat": [1], "sat": [3]}
        assert reconstruct_abstract(index) == "the cat the sat"

    def test_empty(self):
        assert reconstruct_abstract({}) == ""

    def test_none(self):
        assert reconstruct_abstract(None) == ""

    @pytest.mark.parametrize(
        "malformed",
        [
            ["Hello", "world"],  # a list body -> AttributeError on .items()
            "Hello world",  # a plain string
            42,
            {"word": 5},  # non-list positions -> TypeError in `for`
            {"word": "0"},
            {5: [0]},  # non-str word
        ],
    )
    def test_a_malformed_index_is_empty_not_a_crash(self, malformed):
        """``get_paper_abstract`` has no try, and none of AttributeError /
        TypeError is in ``_PARSE_ERRORS`` or ``HTTPX_ERRORS``."""
        assert reconstruct_abstract(malformed) == ""

    def test_mixed_position_types_do_not_raise_in_sort(self):
        # `sorted` comparing int against str is a TypeError nothing catches.
        assert reconstruct_abstract({"a": [0], "b": ["1"], "c": [2]}) == "a c"


# ---------------------------------------------------------------------------
# get_work
# ---------------------------------------------------------------------------


class TestGetWork:
    @pytest.mark.asyncio
    async def test_happy_path_caches_and_second_call_is_free(self, monkeypatch):
        requests = _stub_json_responses(monkeypatch, _work_response())

        first = await openalex.get_work("10.1234/x")
        second = await openalex.get_work("10.1234/x")

        assert first["id"] == second["id"] == "https://openalex.org/W1"
        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_404_error_carries_not_found_and_negative_caches(self, monkeypatch):
        """A definitive 404 tags its error dict with ``not_found: True`` so
        ``get_paper_metadata`` can tell a true miss (eligible for the Crossref
        fallback) from a transient error — and persists the marker, so a later
        read still triggers the fallback."""
        _stub_json_responses(monkeypatch, _Resp(404))

        result = await openalex.get_work("10.9999/does-not-exist")
        assert result.get("not_found") is True
        assert "No work found for DOI" in result["error"]

        neg = cache.get_negative(openalex.NAMESPACE, "works", "10.9999/does-not-exist")
        assert neg is not None
        assert neg.get("not_found") is True

    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, monkeypatch):
        # A 200 with a garbled body must NOT crash — it should surface the
        # uniform {error, retryable} contract like every other failure.
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await openalex.get_work("10.1234/x")

        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_malformed_json_not_negative_cached(self, monkeypatch):
        # A parse failure is transient — a retry must re-fetch and succeed,
        # not be served a poisoned negative-cache entry.
        requests = _stub_json_responses(monkeypatch, _BAD_JSON, _work_response())

        first = await openalex.get_work("10.1234/x")
        assert "error" in first
        assert cache.get_negative(openalex.NAMESPACE, "works", "10.1234/x") is None

        second = await openalex.get_work("10.1234/x")
        assert second.get("id") == "https://openalex.org/W1"
        assert len(requests) == 2  # re-fetched, not cached

    @pytest.mark.asyncio
    async def test_missing_id_not_cached(self, monkeypatch):
        # An anomalous 200 without an `id` must not positive-cache; it should
        # error and a retry re-fetches.
        requests = _stub_json_responses(monkeypatch, {"status": "ok"}, _work_response())

        first = await openalex.get_work("10.1234/x")
        assert "error" in first

        second = await openalex.get_work("10.1234/x")
        assert second.get("id") == "https://openalex.org/W1"
        assert len(requests) == 2

    @pytest.mark.asyncio
    async def test_non_dict_json_returns_error(self, monkeypatch):
        # A JSON list body must not raise AttributeError downstream.
        _stub_json_responses(monkeypatch, [])

        result = await openalex.get_work("10.1234/x")

        assert isinstance(result, dict)
        assert "error" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_transient_status_is_retryable_and_uncached(self, monkeypatch, status):
        requests = _stub_json_responses(monkeypatch, _Resp(status), _work_response())

        first = await openalex.get_work("10.1234/x")
        assert first["retryable"] is True
        assert first.get("not_found") is None
        assert cache.get_negative(openalex.NAMESPACE, "works", "10.1234/x") is None

        assert (await openalex.get_work("10.1234/x")).get("id") == "https://openalex.org/W1"
        assert len(requests) == 2

    @pytest.mark.asyncio
    async def test_other_4xx_is_unflagged_and_uncached(self, monkeypatch):
        """`_http.error_dict` leaves a non-retryable 4xx unflagged rather than
        ``retryable: False``; it must still not negative-cache."""
        _stub_json_responses(monkeypatch, _Resp(403, {"message": "nope"}))

        result = await openalex.get_work("10.1234/x")

        assert "retryable" not in result
        assert result.get("not_found") is None
        assert cache.get_negative(openalex.NAMESPACE, "works", "10.1234/x") is None

    @pytest.mark.asyncio
    async def test_transport_error_is_retryable(self, monkeypatch):
        _stub_json_responses(monkeypatch, httpx.ConnectError("boom"))

        result = await openalex.get_work("10.1234/x")

        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_force_refresh_refetches(self, monkeypatch):
        requests = _stub_json_responses(monkeypatch, _work_response(), _work_response())

        await openalex.get_work("10.1234/x")
        assert len(requests) == 1

        await openalex.get_work("10.1234/x", force_refresh=True)
        assert len(requests) == 2, "force_refresh must drop the cache and re-fetch"

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_fetch(self, monkeypatch):
        requests = _stub_json_responses(monkeypatch, _work_response(), slow=True)

        await asyncio.gather(openalex.get_work("10.1234/x"), openalex.get_work("10.1234/x"))

        assert len(requests) == 1


class TestGetWorkDoiEncoding:
    @pytest.mark.asyncio
    async def test_encodes_special_chars_in_doi(self, monkeypatch):
        # A DOI containing '#' must be percent-encoded in the path; otherwise
        # httpx treats '#bar' as a fragment and requests the wrong record.
        requests = _stub_json_responses(monkeypatch, _work_response("10.1234/foo#bar"))

        await openalex.get_work("10.1234/foo#bar")

        path = _path_of(requests[0])
        assert "%23" in path
        assert "#" not in path

    @pytest.mark.parametrize(
        "dotted", ["10.1000/.", "10.1000/a/..", "10.1000/../b", "", "   ", "doi:", "DOI: "]
    )
    @pytest.mark.asyncio
    async def test_a_dot_segment_suffix_is_rejected_before_any_request(self, monkeypatch, dotted):
        """The `doi:` prefix protects the first segment only — a `.`/`..` in the
        DOI's own suffix still shortens the path, and the record that shorter
        path returns would cache under *this* DOI's key."""
        requests = _stub_json_responses(monkeypatch, _work_response())

        result = await openalex.get_work(dotted)

        assert result["not_found"] is True
        assert requests == []

    @pytest.mark.asyncio
    async def test_an_empty_doi_is_not_negative_cached_under_the_empty_key(self, monkeypatch):
        """The `doi:` prefix keeps the last path segment non-empty, so
        `addresses_a_record` alone passes `/works/doi:` — a spent request whose
        404 would then negative-cache every blank identifier as one entry."""
        requests = _stub_json_responses(monkeypatch, _work_response())

        result = await openalex.get_work("doi: ")

        assert result["not_found"] is True
        assert requests == []
        assert cache.get_negative(openalex.NAMESPACE, "works", "") is None

    @pytest.mark.asyncio
    async def test_preserves_slash_in_doi(self, monkeypatch):
        requests = _stub_json_responses(monkeypatch, _work_response("10.1038/nature12373"))

        await openalex.get_work("10.1038/nature12373")

        assert _path_of(requests[0]) == "/works/doi:10.1038/nature12373"


# ---------------------------------------------------------------------------
# get_author
# ---------------------------------------------------------------------------


class TestGetAuthor:
    @pytest.mark.asyncio
    async def test_404_carries_not_found_and_negative_caches(self, monkeypatch):
        _stub_json_responses(monkeypatch, _Resp(404))

        result = await openalex.get_author("A123")
        assert result.get("not_found") is True
        assert "No author found" in result["error"]

        neg = cache.get_negative(openalex.NAMESPACE, "authors", "a123")
        assert neg is not None
        assert neg.get("not_found") is True

    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, monkeypatch):
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await openalex.get_author("A123")

        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_missing_id_not_cached(self, monkeypatch):
        requests = _stub_json_responses(monkeypatch, {"status": "ok"}, _author_response())

        first = await openalex.get_author("A123")
        assert "error" in first

        second = await openalex.get_author("A123")
        assert second.get("id") == "https://openalex.org/A1"
        assert len(requests) == 2

    @pytest.mark.asyncio
    async def test_non_dict_json_returns_error(self, monkeypatch):
        _stub_json_responses(monkeypatch, [])

        result = await openalex.get_author("A123")

        assert isinstance(result, dict)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_transient_status_is_retryable_and_uncached(self, monkeypatch):
        _stub_json_responses(monkeypatch, _Resp(503))

        result = await openalex.get_author("A123")

        assert result["retryable"] is True
        assert cache.get_negative(openalex.NAMESPACE, "authors", "a123") is None

    @pytest.mark.asyncio
    async def test_force_refresh_refetches(self, monkeypatch):
        requests = _stub_json_responses(monkeypatch, _author_response(), _author_response())

        first = await openalex.get_author("A123")
        assert first.get("id") == "https://openalex.org/A1"
        assert len(requests) == 1

        second = await openalex.get_author("A123", force_refresh=True)
        assert second.get("id") == "https://openalex.org/A1"
        assert len(requests) == 2, "force_refresh must drop the cache and re-fetch"

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_fetch(self, monkeypatch):
        requests = _stub_json_responses(monkeypatch, _author_response(), slow=True)

        await asyncio.gather(openalex.get_author("A123"), openalex.get_author("A123"))

        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_a_work_and_an_author_do_not_share_a_single_flight_slot(self, monkeypatch):
        """One ``SingleFlight`` serves both entities, so the key must be
        tuple-namespaced or an id spelled the same collapses into one fetch."""
        requests = _stub_json_responses(
            monkeypatch, _work_response(), _author_response(), slow=True
        )

        await asyncio.gather(openalex.get_work("A123"), openalex.get_author("A123"))

        assert len(requests) == 2


class TestGetAuthorIdEncoding:
    @pytest.mark.asyncio
    async def test_encodes_reserved_characters(self, monkeypatch):
        requests = _stub_json_responses(monkeypatch, _author_response())

        await openalex.get_author("A1#x y")

        path = _path_of(requests[0])
        assert "#" not in path
        assert "%23" in path and "%20" in path

    @pytest.mark.parametrize("dotted", [".", "..", "", "   ", "a/../b", "./A1", "A1/.."])
    @pytest.mark.asyncio
    async def test_a_dot_segment_id_is_rejected_before_any_request(self, monkeypatch, dotted):
        """RFC 3986 removes a `.`/`..` segment *after* percent-encoding — both
        characters are unreserved, so no `quote` escapes them — and the request
        would land on `/authors` or the API root and read as a retryable parse
        error. It is a bad identifier, and definitively so."""
        requests = _stub_json_responses(monkeypatch, _author_response())

        result = await openalex.get_author(dotted)

        assert result["not_found"] is True
        assert requests == [], "a dot-segment id must not be spent upstream"

    @pytest.mark.asyncio
    async def test_an_orcid_url_survives_byte_identical(self, monkeypatch):
        """``safe=":/"`` — the ORCID-URL spelling is the one OpenAlex itself
        resolves, so escaping its scheme would break the lookup."""
        requests = _stub_json_responses(monkeypatch, _author_response())

        await openalex.get_author("https://orcid.org/0000-0001-6187-6610")

        assert _path_of(requests[0]) == "/authors/https://orcid.org/0000-0001-6187-6610"


# ---------------------------------------------------------------------------
# get_works_batch
# ---------------------------------------------------------------------------


class TestGetWorksBatch:
    @pytest.mark.asyncio
    async def test_empty_input(self, monkeypatch):
        _stub_no_network(monkeypatch)
        assert await openalex.get_works_batch([]) == {}

    @pytest.mark.asyncio
    async def test_serves_cached_without_http(self, monkeypatch):
        canonicals = ["10.1/x", "10.2/y"]
        for c in canonicals:
            cache.put("openalex", "works", c, {"id": c, "title": f"work {c}"})
        _stub_no_network(monkeypatch)

        out = await openalex.get_works_batch(canonicals)

        assert set(out) == set(canonicals)
        assert out["10.1/x"]["title"] == "work 10.1/x"

    @pytest.mark.asyncio
    async def test_serves_a_negative_hit_without_http(self, monkeypatch):
        cache.put_negative(
            "openalex", "works", "10.1/gone", {"error": "No work found", "not_found": True}
        )
        _stub_no_network(monkeypatch)

        out = await openalex.get_works_batch(["10.1/gone"])

        assert out["10.1/gone"]["not_found"] is True

    @pytest.mark.asyncio
    async def test_fetches_misses_in_one_batch_and_warms_the_singleton_cache(self, monkeypatch):
        requests = _stub_filter_echo(monkeypatch)

        out = await openalex.get_works_batch(["10.1/a", "10.2/b"])

        assert len(requests) == 1
        filter_param = parse_qs(requests[0].url.query.decode())["filter"][0]
        assert filter_param == "doi:10.1/a|10.2/b"
        assert out["10.1/a"]["title"] == "A Great Work"
        # Each result is also written to the singleton cache, so a follow-up
        # get_work(doi) is a free hit.
        assert cache.get("openalex", "works", "10.1/a") is not None
        assert cache.get("openalex", "works", "10.2/b") is not None

    @pytest.mark.asyncio
    async def test_dedupes_input_and_keys_in_input_order(self, monkeypatch):
        requests = _stub_filter_echo(monkeypatch)

        out = await openalex.get_works_batch(["10.2/y", "10.1/x", "10.1/x", "10.1/X"])

        # All three spellings of the second DOI collapse to one; one fetch.
        assert len(requests) == 1
        assert list(out) == ["10.2/y", "10.1/x"]

    @pytest.mark.asyncio
    async def test_force_refresh_drops_cached_first(self, monkeypatch):
        cache.put("openalex", "works", "10.1/x", {"id": "old"})
        _stub_filter_echo(monkeypatch)

        out = await openalex.get_works_batch(["10.1/x"], force_refresh=True)

        assert out["10.1/x"]["id"] == "https://openalex.org/W1"

    @pytest.mark.asyncio
    async def test_http_doi_is_matched_not_negative_cached(self, monkeypatch):
        # An http:// input must canonicalize the same way the response DOI
        # does, so a returned work is matched rather than reported missing.
        _stub_json_responses(
            monkeypatch, {"meta": {"count": 1}, "results": [_work_response("10.1234/x")]}
        )

        result = await openalex.get_works_batch(["http://doi.org/10.1234/x"])

        assert list(result) == ["10.1234/x"]
        assert result["10.1234/x"].get("id") == "https://openalex.org/W1"

    @pytest.mark.asyncio
    async def test_books_one_cache_miss_per_upstream_doi(self, monkeypatch):
        """``_fetch_chunk`` bypasses ``cached_lookup``, so it books its own —
        and a warm DOI must not be counted as one."""
        _stats.reset()
        cache.put("openalex", "works", "10.1/warm", {"id": "W0"})
        _stub_filter_echo(monkeypatch)

        await openalex.get_works_batch(["10.1/warm", "10.1/a", "10.2/b"])

        row = _stats.snapshot()["providers"]["openalex"]
        assert row["cache_misses"] == 2
        assert row["cache_hits"] == 1


class TestGetWorksBatchChunking:
    """It chunks the *misses*, not the argument — so a warm cache exercises
    nothing, and the boundary is ``_BATCH_CHUNK_SIZE`` cache misses."""

    @staticmethod
    def _dois(n):
        return [f"10.1/{i}" for i in range(n)]

    @pytest.mark.asyncio
    async def test_exactly_the_chunk_size_is_one_request(self, monkeypatch):
        requests = _stub_filter_echo(monkeypatch)

        dois = self._dois(openalex._BATCH_CHUNK_SIZE)
        out = await openalex.get_works_batch(dois)

        assert len(requests) == 1
        assert len(out) == len(dois)
        assert all("error" not in v for v in out.values())

    @pytest.mark.asyncio
    async def test_one_past_the_chunk_size_is_two_requests(self, monkeypatch):
        requests = _stub_filter_echo(monkeypatch)

        dois = self._dois(openalex._BATCH_CHUNK_SIZE + 1)
        out = await openalex.get_works_batch(dois)

        assert len(requests) == 2
        assert len(out) == len(dois)

    @pytest.mark.asyncio
    async def test_a_warm_cache_shrinks_the_chunking(self, monkeypatch):
        dois = self._dois(openalex._BATCH_CHUNK_SIZE + 10)
        for c in dois[:-1]:
            cache.put("openalex", "works", c, {"id": c})
        requests = _stub_filter_echo(monkeypatch)

        out = await openalex.get_works_batch(dois)

        assert len(requests) == 1, "only the single miss should go upstream"
        assert len(out) == len(dois)


class TestGetWorksBatchUnsafeDois:
    """A DOI carrying an OpenAlex filter metacharacter ('|' = OR, ',' = AND)
    would corrupt the OR-joined filter and silently shift which records
    resolve, so it is resolved through the singleton path endpoint instead."""

    @pytest.mark.parametrize("unsafe", ["10.1234/a|b", "10.1234/a,b"])
    @pytest.mark.asyncio
    async def test_resolved_individually_and_kept_out_of_the_filter(self, monkeypatch, unsafe):
        requests: list[httpx.Request] = []
        _reset_openalex(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/works":
                return httpx.Response(
                    200, json={"meta": {"count": 1}, "results": [_work_response("10.1234/normal")]}
                )
            return httpx.Response(200, json=_work_response(unsafe))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: client)

        result = await openalex.get_works_batch(["10.1234/normal", unsafe])

        assert "error" not in result["10.1234/normal"]
        assert "error" not in result[unsafe]
        # The metacharacter went through the encoded singleton path...
        singleton = next(r for r in requests if r.url.path != "/works")
        assert unsafe.rsplit("/", 1)[1] not in _path_of(singleton)
        # ...and never reached the OR-joined filter.
        filters = [
            parse_qs(r.url.query.decode()).get("filter", [""])[0]
            for r in requests
            if r.url.path == "/works"
        ]
        assert all(unsafe not in f for f in filters)

    @pytest.mark.asyncio
    async def test_several_unsafe_dois_stay_aligned_with_their_results(self, monkeypatch):
        """``zip(..., strict=True)`` pairs the gather results back onto
        ``unsafe_misses`` positionally; a reordering mislabels every record."""
        _reset_openalex(monkeypatch)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/works":
                raw = parse_qs(request.url.query.decode())["filter"][0]
                asked = raw.removeprefix("doi:").split("|")
                return httpx.Response(
                    200,
                    json={
                        "meta": {"count": len(asked)},
                        "results": [_work_response(d) for d in asked],
                    },
                )
            doi = _path_of(request).removeprefix("/works/doi:")
            from urllib.parse import unquote

            return httpx.Response(200, json={"id": f"W-{unquote(doi)}", "doi": unquote(doi)})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: client)

        dois = ["10.1/a|1", "10.1/safe1", "10.1/b,2", "10.1/safe2"]
        out = await openalex.get_works_batch(dois)

        assert out["10.1/a|1"]["id"] == "W-10.1/a|1"
        assert out["10.1/b,2"]["id"] == "W-10.1/b,2"
        assert list(out) == dois


class TestBatchNegativeCaching:
    """A DOI missing from a batch response is only "not found" if the response
    actually accounted for everything it returned.

    Negative-caching a DOI that a truncated page, or a record we could not
    attribute, merely failed to mention poisons a live entry for the full
    negative TTL.
    """

    @pytest.mark.asyncio
    async def test_clean_miss_is_negative_cached(self, monkeypatch):
        _stub_json_responses(
            monkeypatch, {"meta": {"count": 1}, "results": [_work_response("10.1234/found")]}
        )

        out = await openalex.get_works_batch(["10.1234/found", "10.1234/missing"])

        missing = out["10.1234/missing"]
        assert missing["not_found"] is True
        assert cache.get_negative("openalex", "works", "10.1234/missing") is not None

    @pytest.mark.asyncio
    async def test_unmatched_returned_doi_blocks_negative_caching(self, monkeypatch):
        # OpenAlex answers with a DOI we didn't ask for: we can no longer tell
        # which request it satisfies, so "missing" is inconclusive.
        _stub_json_responses(
            monkeypatch,
            {"meta": {"count": 1}, "results": [_work_response("10.1234/something-else")]},
        )

        out = await openalex.get_works_batch(["10.1234/asked"])

        assert out["10.1234/asked"].get("not_found") is None
        assert out["10.1234/asked"]["retryable"] is True
        assert cache.get_negative("openalex", "works", "10.1234/asked") is None

    @pytest.mark.asyncio
    async def test_unmatched_work_is_still_cached_under_its_own_doi(self, monkeypatch):
        _stub_json_responses(
            monkeypatch,
            {"meta": {"count": 1}, "results": [_work_response("10.1234/something-else")]},
        )

        await openalex.get_works_batch(["10.1234/asked"])

        assert cache.get("openalex", "works", "10.1234/something-else") is not None

    @pytest.mark.asyncio
    async def test_truncated_response_blocks_negative_caching(self, monkeypatch):
        # meta.count says there were more matches than were sent.
        _stub_json_responses(
            monkeypatch, {"meta": {"count": 9}, "results": [_work_response("10.1234/a")]}
        )

        out = await openalex.get_works_batch(["10.1234/a", "10.1234/b"])

        assert out["10.1234/b"]["retryable"] is True
        assert cache.get_negative("openalex", "works", "10.1234/b") is None

    @pytest.mark.parametrize(
        "unattributable",
        [
            "not-a-dict",
            {"id": "W9"},  # no doi at all
            {"id": "W9", "doi": None},
            {"id": "W9", "doi": 42},  # non-str: reaches the key builder untyped
            {"id": "W9", "doi": ""},
        ],
        ids=["non-dict", "no-doi", "null-doi", "non-str-doi", "empty-doi"],
    )
    @pytest.mark.asyncio
    async def test_an_unattributable_record_blocks_negative_caching(
        self, monkeypatch, unattributable
    ):
        """A record we cannot pin to a DOI we asked for is the same epistemic
        situation as one carrying a DOI we didn't ask for: the response no
        longer accounts for itself, so no miss in the chunk is definitive."""
        _stub_json_responses(
            monkeypatch,
            {
                "meta": {"count": 2},
                "results": [_work_response("10.1234/found"), unattributable],
            },
        )

        out = await openalex.get_works_batch(["10.1234/found", "10.1234/missing"])

        assert out["10.1234/found"]["id"] == "https://openalex.org/W1"
        assert out["10.1234/missing"].get("not_found") is None
        assert out["10.1234/missing"]["retryable"] is True
        assert cache.get_negative("openalex", "works", "10.1234/missing") is None

    @pytest.mark.asyncio
    async def test_non_list_results_is_a_parse_error_for_the_whole_chunk(self, monkeypatch):
        _stub_json_responses(monkeypatch, {"meta": {"count": 1}, "results": {"not": "a list"}})

        out = await openalex.get_works_batch(["10.1234/a", "10.1234/b"])

        assert all(v["retryable"] is True for v in out.values())
        assert cache.get_negative("openalex", "works", "10.1234/a") is None

    @pytest.mark.asyncio
    async def test_a_non_dict_meta_does_not_crash_the_truncation_check(self, monkeypatch):
        _stub_json_responses(
            monkeypatch, {"meta": "nope", "results": [_work_response("10.1234/a")]}
        )

        out = await openalex.get_works_batch(["10.1234/a", "10.1234/b"])

        # No count to compare against, and every record was attributed, so the
        # miss stays definitive.
        assert out["10.1234/b"]["not_found"] is True


class TestBatchErrorDictsAreNotAliased:
    """``dict.fromkeys(chunk, _parse_error_dict())`` put ONE dict object behind
    up to 50 keys, so a caller mutating its own error corrupted the other 49.

    Nothing was visibly broken — the sole consumer happens to shallow-copy
    before mutating — but that copy was load-bearing without saying so, and
    ``get_works_batch`` is public. It also contradicted
    ``_http.parse_error_dict``'s documented "a new dict each call".
    """

    @pytest.mark.asyncio
    async def test_a_parse_failure_yields_independent_dicts(self, monkeypatch):
        _stub_json_responses(monkeypatch, _BAD_JSON)

        out = await openalex.get_works_batch(["10.1234/a", "10.1234/b"])

        assert out["10.1234/a"] is not out["10.1234/b"]
        out["10.1234/a"]["suggestion"] = "mine"
        assert "suggestion" not in out["10.1234/b"]

    @pytest.mark.asyncio
    async def test_a_transport_failure_yields_independent_dicts(self, monkeypatch):
        _stub_json_responses(monkeypatch, httpx.ConnectError("boom"))

        out = await openalex.get_works_batch(["10.1234/a", "10.1234/b"])

        assert out["10.1234/a"] is not out["10.1234/b"]
        out["10.1234/a"]["retryable"] = "mutated"
        assert out["10.1234/b"].get("retryable") != "mutated"

    @pytest.mark.asyncio
    async def test_a_non_dict_body_yields_independent_dicts(self, monkeypatch):
        _stub_json_responses(monkeypatch, [])

        out = await openalex.get_works_batch(["10.1234/a", "10.1234/b"])

        assert out["10.1234/a"] is not out["10.1234/b"]


class TestBatchSingleFlight:
    """Overlapping concurrent batches must coalesce into one HTTP call.

    ``get_works_batch`` bypasses the shared getter protocol, so the
    coalescing is its own.
    """

    @pytest.mark.asyncio
    async def test_concurrent_identical_batches_issue_one_request(self, monkeypatch):
        requests = _stub_json_responses(
            monkeypatch, {"meta": {"count": 1}, "results": [_work_response("10.1234/x")]}, slow=True
        )

        dois = ["10.1234/x", "10.1234/y"]
        await asyncio.gather(
            openalex.get_works_batch(dois),
            openalex.get_works_batch(dois),
        )

        assert len(requests) == 1, f"expected coalescing, got {len(requests)} GETs"

    @pytest.mark.asyncio
    async def test_doi_order_does_not_defeat_coalescing(self, monkeypatch):
        requests = _stub_json_responses(
            monkeypatch, {"meta": {"count": 1}, "results": [_work_response("10.1234/x")]}, slow=True
        )

        await asyncio.gather(
            openalex.get_works_batch(["10.1234/x", "10.1234/y"]),
            openalex.get_works_batch(["10.1234/y", "10.1234/x"]),
        )

        assert len(requests) == 1
