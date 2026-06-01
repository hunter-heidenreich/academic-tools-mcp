
import pytest

from academic_tools_mcp.providers import opencitations

# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_bare_doi(self):
        assert opencitations._normalize_doi("10.1038/nature12373") == "10.1038/nature12373"

    def test_https_url(self):
        assert (
            opencitations._normalize_doi("https://doi.org/10.1038/nature12373")
            == "10.1038/nature12373"
        )

    def test_http_url(self):
        assert (
            opencitations._normalize_doi("http://doi.org/10.1038/nature12373")
            == "10.1038/nature12373"
        )

    def test_doi_prefix(self):
        assert opencitations._normalize_doi("doi:10.1038/nature12373") == "10.1038/nature12373"

    def test_strips_whitespace(self):
        assert opencitations._normalize_doi("  10.1038/nature12373  ") == "10.1038/nature12373"


class TestCanonicalDoi:
    def test_lowercases(self):
        assert opencitations.canonical_doi("10.1038/Nature12373") == "10.1038/nature12373"

    def test_normalizes_and_lowercases(self):
        assert (
            opencitations.canonical_doi("https://doi.org/10.1038/Nature12373")
            == "10.1038/nature12373"
        )


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# ID parsing
# ---------------------------------------------------------------------------


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

    def test_none(self):
        assert opencitations._parse_ids(None) == {}


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Malformed-body / parse-error handling, DOI path encoding, force_refresh
# ---------------------------------------------------------------------------


def _reset_opencitations(monkeypatch, tmp_path):
    """Point the cache at tmp_path and disable the throttle gap (no real sleeps).

    The conftest autouse fixture already resets each provider's ``_throttle``
    and ``_single_flight`` between tests; here we additionally zero the gap so a
    references+citations test doesn't wait out OpenCitations' pacing.
    """
    from academic_tools_mcp import _singleflight, cache

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(opencitations._throttle, "min_gap_seconds", 0.0)
    monkeypatch.setattr(opencitations, "_single_flight", _singleflight.SingleFlight())


# Sentinel: a payload whose .json() raises, simulating a malformed/truncated body.
_BAD_JSON = object()


def _stub_json_responses(monkeypatch, *payloads, status_code=200):
    """Install a stub client returning ``payloads`` from successive GETs.

    A payload of ``_BAD_JSON`` makes ``.json()`` raise ``json.JSONDecodeError``
    (garbled body). The returned object exposes ``.urls`` (every URL requested)
    and ``.count`` (number of GETs) for assertions.
    """
    import json

    from academic_tools_mcp import _clients

    seq = list(payloads)

    class _Recorder:
        def __init__(self):
            self.urls: list[str] = []

        @property
        def count(self) -> int:
            return len(self.urls)

    recorder = _Recorder()

    class StubResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = status_code
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            pass

        def json(self):
            if self._payload is _BAD_JSON:
                raise json.JSONDecodeError("Expecting value", "", 0)
            return self._payload

    class StubClient:
        async def get(self, url, **kwargs):
            recorder.urls.append(url)
            idx = len(recorder.urls) - 1
            payload = seq[idx] if idx < len(seq) else seq[-1]
            return StubResponse(payload)

    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())
    return recorder


def _references_response(cited_doi="10.5555/cited"):
    """A minimal valid OpenCitations /references response (a list of records)."""
    return [
        {
            "cited": f"omid:br/01 doi:{cited_doi} openalex:W1 pmid:1",
            "creation": "2020-01-01",
            "journal_sc": "no",
            "author_sc": "no",
        }
    ]


def _citations_response(citing_doi="10.5555/citing"):
    """A minimal valid OpenCitations /citations response (a list of records)."""
    return [
        {
            "citing": f"omid:br/02 doi:{citing_doi} openalex:W2 pmid:2",
            "creation": "2021-01-01",
            "journal_sc": "no",
            "author_sc": "no",
        }
    ]


class TestGetReferencesParseErrors:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, tmp_path, monkeypatch):
        # A 200 with a garbled body must NOT crash — it should surface the
        # uniform {error, retryable} contract like every other failure.
        _reset_opencitations(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await opencitations.get_references("10.1234/x")

        assert isinstance(result, dict)
        assert "error" in result
        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_malformed_json_not_negative_cached(self, tmp_path, monkeypatch):
        # A parse failure is transient — a retry must re-fetch and succeed,
        # not be served a poisoned negative-cache entry.
        _reset_opencitations(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _BAD_JSON, _references_response())

        first = await opencitations.get_references("10.1234/x")
        assert "error" in first

        second = await opencitations.get_references("10.1234/x")
        assert second.get("count") == 1
        assert recorder.count == 2  # re-fetched, not cached

    @pytest.mark.asyncio
    async def test_non_list_body_not_cached(self, tmp_path, monkeypatch):
        # A 200 that is a dict instead of a list must not crash the record
        # comprehension nor positive-cache garbage; it errors and a retry
        # re-fetches.
        _reset_opencitations(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, {"status": "ok"}, _references_response())

        first = await opencitations.get_references("10.1234/x")
        assert "error" in first

        second = await opencitations.get_references("10.1234/x")
        assert second.get("count") == 1
        assert recorder.count == 2


class TestGetCitationsParseErrors:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, tmp_path, monkeypatch):
        _reset_opencitations(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await opencitations.get_citations("10.1234/x")

        assert isinstance(result, dict)
        assert "error" in result
        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_non_list_body_not_cached(self, tmp_path, monkeypatch):
        _reset_opencitations(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, {"status": "ok"}, _citations_response())

        first = await opencitations.get_citations("10.1234/x")
        assert "error" in first

        second = await opencitations.get_citations("10.1234/x")
        assert second.get("count") == 1
        assert recorder.count == 2


class TestGetReferencesDoiEncoding:
    @pytest.mark.asyncio
    async def test_encodes_special_chars_in_doi(self, tmp_path, monkeypatch):
        # A DOI containing '#' must be percent-encoded in the path; otherwise
        # httpx treats '#bar' as a fragment and requests references for the
        # wrong record (which then gets positive-cached).
        _reset_opencitations(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _references_response())

        await opencitations.get_references("10.1234/foo#bar")

        url = recorder.urls[0]
        assert "%23" in url
        assert "#" not in url

    @pytest.mark.asyncio
    async def test_preserves_slash_and_doi_prefix(self, tmp_path, monkeypatch):
        # Regression: the "doi:" scheme prefix and the DOI's own slash stay
        # literal — only reserved chars are encoded.
        _reset_opencitations(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _references_response())

        await opencitations.get_references("10.1038/nature12373")

        assert recorder.urls[0].endswith("/references/doi:10.1038/nature12373")


class TestGetCitationsDoiEncoding:
    @pytest.mark.asyncio
    async def test_encodes_special_chars_in_doi(self, tmp_path, monkeypatch):
        _reset_opencitations(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _citations_response())

        await opencitations.get_citations("10.1234/foo#bar")

        url = recorder.urls[0]
        assert "%23" in url
        assert "#" not in url

    @pytest.mark.asyncio
    async def test_preserves_slash_and_doi_prefix(self, tmp_path, monkeypatch):
        _reset_opencitations(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _citations_response())

        await opencitations.get_citations("10.1038/nature12373")

        assert recorder.urls[0].endswith("/citations/doi:10.1038/nature12373")


class TestGetReferencesForceRefresh:
    @pytest.mark.asyncio
    async def test_force_refresh_rebypasses_cache(self, tmp_path, monkeypatch):
        _reset_opencitations(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _references_response())

        await opencitations.get_references("10.1234/x")
        assert recorder.count == 1

        # Plain re-read is a cache hit — no new HTTP call.
        await opencitations.get_references("10.1234/x")
        assert recorder.count == 1

        # force_refresh drops the cached entry and re-fetches.
        await opencitations.get_references("10.1234/x", force_refresh=True)
        assert recorder.count == 2


class TestGetCitationsForceRefresh:
    @pytest.mark.asyncio
    async def test_force_refresh_rebypasses_cache(self, tmp_path, monkeypatch):
        _reset_opencitations(monkeypatch, tmp_path)
        recorder = _stub_json_responses(monkeypatch, _citations_response())

        await opencitations.get_citations("10.1234/x")
        assert recorder.count == 1

        await opencitations.get_citations("10.1234/x")
        assert recorder.count == 1

        await opencitations.get_citations("10.1234/x", force_refresh=True)
        assert recorder.count == 2
