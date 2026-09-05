import asyncio
import json

import pytest

from academic_tools_mcp import cache
from academic_tools_mcp.providers import openalex
from academic_tools_mcp.providers.openalex import (
    _normalize_author_id,
    _normalize_doi,
    canonical_author_id,
    canonical_doi,
    reconstruct_abstract,
)


class TestNormalizeDoi:
    def test_bare_doi(self):
        assert _normalize_doi("10.1234/test") == "10.1234/test"

    def test_prefixed_doi(self):
        assert _normalize_doi("doi:10.1234/test") == "10.1234/test"

    def test_url_doi(self):
        assert _normalize_doi("https://doi.org/10.1234/test") == "10.1234/test"

    def test_http_url_doi(self):
        # Parity with crossref: the insecure scheme must also be stripped,
        # otherwise the bare DOI is never recovered and the request 404s.
        assert _normalize_doi("http://doi.org/10.1234/test") == "10.1234/test"

    def test_strips_whitespace(self):
        assert _normalize_doi("  10.1234/test  ") == "10.1234/test"


class TestCanonicalDoi:
    def test_lowercases(self):
        assert canonical_doi("10.1234/ABC") == "10.1234/abc"

    def test_strips_prefix_and_lowercases(self):
        assert canonical_doi("doi:10.1234/ABC") == "10.1234/abc"

    def test_strips_url_and_lowercases(self):
        assert canonical_doi("https://doi.org/10.1234/ABC") == "10.1234/abc"

    def test_strips_http_url_and_lowercases(self):
        assert canonical_doi("http://doi.org/10.1234/ABC") == "10.1234/abc"


class TestNormalizeAuthorId:
    def test_openalex_id(self):
        assert _normalize_author_id("A5023888391") == "A5023888391"

    def test_openalex_url(self):
        assert _normalize_author_id("https://openalex.org/A5023888391") == "A5023888391"

    def test_orcid_url_passthrough(self):
        orcid = "https://orcid.org/0000-0001-6187-6610"
        assert _normalize_author_id(orcid) == orcid


class TestCanonicalAuthorId:
    def test_lowercases(self):
        assert canonical_author_id("A5023888391") == "a5023888391"

    def test_strips_url_and_lowercases(self):
        assert canonical_author_id("https://openalex.org/A5023888391") == "a5023888391"

    def test_orcid_lowercased(self):
        assert (
            canonical_author_id("https://orcid.org/0000-0001-6187-6610")
            == "https://orcid.org/0000-0001-6187-6610"
        )


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


class TestGetWork404Marker:
    """A definitive 404 from get_work tags its error dict with
    ``not_found: True`` so server.get_paper_metadata can distinguish a true
    miss (eligible for the Crossref fallback) from a transient error."""

    @pytest.mark.asyncio
    async def test_404_error_carries_not_found_and_negative_caches(self, tmp_path, monkeypatch):
        class StubResponse:
            status_code = 404

            def raise_for_status(self):
                pass

        async def fake_throttled_get(url, **kwargs):
            return StubResponse()

        monkeypatch.setattr(openalex, "_throttled_get", fake_throttled_get)

        result = await openalex.get_work("10.9999/does-not-exist")
        assert result.get("not_found") is True
        assert "No work found for DOI" in result["error"]

        # The marker is persisted in the negative cache too, so a later
        # read (e.g. via a batch that warmed it) still triggers the fallback.
        neg = cache.get_negative(openalex.NAMESPACE, "works", "10.9999/does-not-exist")
        assert neg is not None
        assert neg.get("not_found") is True


# ---------------------------------------------------------------------------
# Hardening: parse errors, shape guards, DOI encoding, batch filter safety,
# get_author contract parity. Mirrors tests/test_crossref.py.
# ---------------------------------------------------------------------------

# Sentinel: a payload whose .json() raises, simulating a malformed/truncated body.
_BAD_JSON = object()


class _StubResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def raise_for_status(self):
        pass

    def json(self):
        if self._payload is _BAD_JSON:
            raise json.JSONDecodeError("Expecting value", "", 0)
        return self._payload


class _Recorder:
    def __init__(self):
        self.urls: list[str] = []
        self.kwargs: list[dict] = []

    @property
    def count(self) -> int:
        return len(self.urls)


def _install_throttled_get(monkeypatch, *payloads, status_code=200):
    """Replace ``openalex._throttled_get`` with a recorder.

    Successive GETs return ``payloads`` in order (the last payload repeats
    once exhausted). A payload of ``_BAD_JSON`` makes ``.json()`` raise
    ``json.JSONDecodeError`` (garbled body). The returned recorder exposes
    ``.urls`` / ``.kwargs`` (per call) and ``.count``.
    """
    recorder = _Recorder()
    seq = list(payloads)

    async def fake_throttled_get(url, **kwargs):
        recorder.urls.append(url)
        recorder.kwargs.append(kwargs)
        idx = len(recorder.urls) - 1
        payload = seq[idx] if idx < len(seq) else seq[-1]
        return _StubResponse(payload, status_code=status_code)

    monkeypatch.setattr(openalex, "_throttled_get", fake_throttled_get)
    return recorder


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


class TestGetWorkParseErrors:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, tmp_path, monkeypatch):
        # A 200 with a garbled body must NOT crash — it should surface the
        # uniform {error, retryable} contract like every other failure.
        _install_throttled_get(monkeypatch, _BAD_JSON)

        result = await openalex.get_work("10.1234/x")

        assert isinstance(result, dict)
        assert "error" in result
        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_malformed_json_not_negative_cached(self, tmp_path, monkeypatch):
        # A parse failure is transient — a retry must re-fetch and succeed,
        # not be served a poisoned negative-cache entry.
        recorder = _install_throttled_get(monkeypatch, _BAD_JSON, _work_response())

        first = await openalex.get_work("10.1234/x")
        assert "error" in first
        assert cache.get_negative(openalex.NAMESPACE, "works", "10.1234/x") is None

        second = await openalex.get_work("10.1234/x")
        assert second.get("id") == "https://openalex.org/W1"
        assert recorder.count == 2  # re-fetched, not cached

    @pytest.mark.asyncio
    async def test_missing_id_not_cached(self, tmp_path, monkeypatch):
        # An anomalous 200 without an `id` must not positive-cache; it should
        # error and a retry re-fetches.
        recorder = _install_throttled_get(monkeypatch, {"status": "ok"}, _work_response())

        first = await openalex.get_work("10.1234/x")
        assert "error" in first

        second = await openalex.get_work("10.1234/x")
        assert second.get("id") == "https://openalex.org/W1"
        assert recorder.count == 2

    @pytest.mark.asyncio
    async def test_non_dict_json_returns_error(self, tmp_path, monkeypatch):
        # A JSON list body must not raise AttributeError downstream.
        _install_throttled_get(monkeypatch, [])

        result = await openalex.get_work("10.1234/x")

        assert isinstance(result, dict)
        assert "error" in result


class TestGetAuthorParseErrors:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, tmp_path, monkeypatch):
        _install_throttled_get(monkeypatch, _BAD_JSON)

        result = await openalex.get_author("A123")

        assert isinstance(result, dict)
        assert "error" in result
        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_missing_id_not_cached(self, tmp_path, monkeypatch):
        recorder = _install_throttled_get(monkeypatch, {"status": "ok"}, _author_response())

        first = await openalex.get_author("A123")
        assert "error" in first

        second = await openalex.get_author("A123")
        assert second.get("id") == "https://openalex.org/A1"
        assert recorder.count == 2


class TestGetWorkDoiEncoding:
    @pytest.mark.asyncio
    async def test_encodes_special_chars_in_doi(self, tmp_path, monkeypatch):
        # A DOI containing '#' must be percent-encoded in the path; otherwise
        # httpx treats '#bar' as a fragment and requests the wrong record.
        recorder = _install_throttled_get(monkeypatch, _work_response("10.1234/foo#bar"))

        await openalex.get_work("10.1234/foo#bar")

        url = recorder.urls[0]
        assert "%23" in url
        assert "#" not in url

    @pytest.mark.asyncio
    async def test_preserves_slash_in_doi(self, tmp_path, monkeypatch):
        recorder = _install_throttled_get(monkeypatch, _work_response("10.1038/nature12373"))

        await openalex.get_work("10.1038/nature12373")

        assert recorder.urls[0].endswith("/works/doi:10.1038/nature12373")


class TestGetWorksBatch:
    @pytest.mark.asyncio
    async def test_malformed_json_chunk_not_negative_cached(self, tmp_path, monkeypatch):
        # A parse failure on the batch GET maps every DOI in the chunk to a
        # retryable error and must NOT be negative-cached.
        _install_throttled_get(monkeypatch, _BAD_JSON)

        result = await openalex.get_works_batch(["10.1234/x"])

        assert "error" in result["10.1234/x"]
        assert result["10.1234/x"].get("retryable") is True
        assert cache.get_negative(openalex.NAMESPACE, "works", "10.1234/x") is None

    @pytest.mark.asyncio
    async def test_http_doi_is_matched_not_negative_cached(self, tmp_path, monkeypatch):
        # An http:// input must canonicalize the same way the response DOI
        # does, so a returned work is matched rather than reported missing.
        _install_throttled_get(monkeypatch, {"results": [_work_response("10.1234/x")]})

        result = await openalex.get_works_batch(["http://doi.org/10.1234/x"])

        vals = list(result.values())
        assert len(vals) == 1
        assert "error" not in vals[0]
        assert vals[0].get("id") == "https://openalex.org/W1"

    @pytest.mark.asyncio
    async def test_doi_with_pipe_resolved_individually(self, tmp_path, monkeypatch):
        # A DOI containing '|' (OpenAlex OR) corrupts the filter query; it must
        # be resolved via the singleton path endpoint instead.
        recorder = _Recorder()

        async def fake_throttled_get(url, **kwargs):
            recorder.urls.append(url)
            recorder.kwargs.append(kwargs)
            if url.endswith("/works"):
                return _StubResponse({"results": [_work_response("10.1234/normal")]})
            return _StubResponse(_work_response("10.1234/a|b"))

        monkeypatch.setattr(openalex, "_throttled_get", fake_throttled_get)

        result = await openalex.get_works_batch(["10.1234/normal", "10.1234/a|b"])

        assert "error" not in result["10.1234/normal"]
        assert "error" not in result["10.1234/a|b"]
        joined = " ".join(recorder.urls)
        # The pipe DOI went through the encoded singleton path...
        assert "%7C" in joined
        # ...and was kept out of the OR-joined filter.
        filters = [kw.get("params", {}).get("filter", "") for kw in recorder.kwargs]
        assert all("a|b" not in f for f in filters)


class TestGetAuthorContract:
    @pytest.mark.asyncio
    async def test_404_carries_not_found_and_negative_caches(self, tmp_path, monkeypatch):
        _install_throttled_get(monkeypatch, None, status_code=404)

        result = await openalex.get_author("A123")
        assert result.get("not_found") is True
        assert "No author found" in result["error"]

        neg = cache.get_negative(openalex.NAMESPACE, "authors", "a123")
        assert neg is not None
        assert neg.get("not_found") is True

    @pytest.mark.asyncio
    async def test_force_refresh_refetches(self, tmp_path, monkeypatch):
        recorder = _install_throttled_get(monkeypatch, _author_response(), _author_response())

        first = await openalex.get_author("A123")
        assert first.get("id") == "https://openalex.org/A1"
        assert recorder.count == 1

        second = await openalex.get_author("A123", force_refresh=True)
        assert second.get("id") == "https://openalex.org/A1"
        assert recorder.count == 2, "force_refresh must drop the cache and re-fetch"


def _install_slow_throttled_get(monkeypatch, payload):
    """Like ``_install_throttled_get`` but yields to the event loop first.

    ``_install_throttled_get``'s fake has no await point, so an ``async def``
    with no suspension runs to completion before ``asyncio.gather`` starts the
    next coroutine — concurrent callers never actually overlap and nothing can
    coalesce. Yielding once lets followers pile up behind the leader.
    """
    recorder = _Recorder()

    async def fake_throttled_get(url, **kwargs):
        recorder.urls.append(url)
        recorder.kwargs.append(kwargs)
        await asyncio.sleep(0)
        return _StubResponse(payload)

    monkeypatch.setattr(openalex, "_throttled_get", fake_throttled_get)
    return recorder


class TestBatchNegativeCaching:
    """A DOI missing from a batch response is only "not found" if the
    response actually accounted for everything.

    ``get_works_batch`` used to negative-cache (24h) every requested DOI that
    wasn't in ``results`` — including one whose OpenAlex-stored DOI string
    merely differed from what we asked for (the loop ``continue``s past those),
    and without checking ``meta.count`` against the number of records sent, so
    a truncated response poisoned the whole remainder of the chunk.
    """

    @pytest.mark.asyncio
    async def test_clean_miss_is_negative_cached(self, monkeypatch):
        _install_throttled_get(
            monkeypatch,
            {"meta": {"count": 1}, "results": [_work_response("10.1234/found")]},
        )

        out = await openalex.get_works_batch(["10.1234/found", "10.1234/missing"])

        missing = out["10.1234/missing"]
        assert missing["not_found"] is True
        assert cache.get_negative("openalex", "works", "10.1234/missing") is not None

    @pytest.mark.asyncio
    async def test_unmatched_returned_doi_blocks_negative_caching(self, monkeypatch):
        # OpenAlex answers with a DOI we didn't ask for: we can no longer tell
        # which request it satisfies, so "missing" is inconclusive.
        _install_throttled_get(
            monkeypatch,
            {"meta": {"count": 1}, "results": [_work_response("10.1234/something-else")]},
        )

        out = await openalex.get_works_batch(["10.1234/asked"])

        assert out["10.1234/asked"].get("not_found") is None
        assert out["10.1234/asked"]["retryable"] is True
        assert cache.get_negative("openalex", "works", "10.1234/asked") is None

    @pytest.mark.asyncio
    async def test_unmatched_work_is_still_cached_under_its_own_doi(self, monkeypatch):
        _install_throttled_get(
            monkeypatch,
            {"meta": {"count": 1}, "results": [_work_response("10.1234/something-else")]},
        )

        await openalex.get_works_batch(["10.1234/asked"])

        assert cache.get("openalex", "works", "10.1234/something-else") is not None

    @pytest.mark.asyncio
    async def test_truncated_response_blocks_negative_caching(self, monkeypatch):
        # meta.count says there were more matches than were sent.
        _install_throttled_get(
            monkeypatch,
            {"meta": {"count": 9}, "results": [_work_response("10.1234/a")]},
        )

        out = await openalex.get_works_batch(["10.1234/a", "10.1234/b"])

        assert out["10.1234/b"]["retryable"] is True
        assert cache.get_negative("openalex", "works", "10.1234/b") is None


class TestBatchSingleFlight:
    """Overlapping concurrent batches must coalesce into one HTTP call.

    ``get_works_batch`` bypassed the shared getter protocol and had no
    single-flight at all, so two ``get_papers_metadata`` calls with the same
    DOI list both issued a full batch GET.
    """

    @pytest.mark.asyncio
    async def test_concurrent_identical_batches_issue_one_request(self, monkeypatch):
        recorder = _install_slow_throttled_get(
            monkeypatch,
            {"meta": {"count": 1}, "results": [_work_response("10.1234/x")]},
        )

        dois = ["10.1234/x", "10.1234/y"]
        await asyncio.gather(
            openalex.get_works_batch(dois),
            openalex.get_works_batch(dois),
        )

        assert recorder.count == 1, f"expected coalescing, got {recorder.count} GETs"

    @pytest.mark.asyncio
    async def test_doi_order_does_not_defeat_coalescing(self, monkeypatch):
        recorder = _install_slow_throttled_get(
            monkeypatch,
            {"meta": {"count": 1}, "results": [_work_response("10.1234/x")]},
        )

        await asyncio.gather(
            openalex.get_works_batch(["10.1234/x", "10.1234/y"]),
            openalex.get_works_batch(["10.1234/y", "10.1234/x"]),
        )

        assert recorder.count == 1
