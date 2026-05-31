import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from academic_tools_mcp.providers import biorxiv

# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------


class TestNormalizeDoi:
    def test_bare_doi(self):
        assert biorxiv._normalize_doi("10.1101/2024.01.01.573838") == "10.1101/2024.01.01.573838"

    def test_doi_prefix(self):
        assert (
            biorxiv._normalize_doi("doi:10.1101/2024.01.01.573838") == "10.1101/2024.01.01.573838"
        )

    def test_doi_prefix_uppercase(self):
        assert (
            biorxiv._normalize_doi("DOI:10.1101/2024.01.01.573838") == "10.1101/2024.01.01.573838"
        )

    def test_https_doi_url(self):
        assert (
            biorxiv._normalize_doi("https://doi.org/10.1101/2024.01.01.573838")
            == "10.1101/2024.01.01.573838"
        )

    def test_http_doi_url(self):
        assert (
            biorxiv._normalize_doi("http://doi.org/10.1101/2024.01.01.573838")
            == "10.1101/2024.01.01.573838"
        )

    def test_dx_doi_url(self):
        assert (
            biorxiv._normalize_doi("https://dx.doi.org/10.1101/2024.01.01.573838")
            == "10.1101/2024.01.01.573838"
        )

    def test_biorxiv_content_url(self):
        assert (
            biorxiv._normalize_doi("https://www.biorxiv.org/content/10.1101/2024.01.01.573838v1")
            == "10.1101/2024.01.01.573838"
        )

    def test_biorxiv_content_url_full_pdf(self):
        assert (
            biorxiv._normalize_doi(
                "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v2.full.pdf"
            )
            == "10.1101/2024.01.01.573838"
        )

    def test_medrxiv_content_url(self):
        assert (
            biorxiv._normalize_doi("https://www.medrxiv.org/content/10.1101/2020.09.09.20191205v1")
            == "10.1101/2020.09.09.20191205"
        )

    def test_strips_whitespace(self):
        assert (
            biorxiv._normalize_doi("  10.1101/2024.01.01.573838  ") == "10.1101/2024.01.01.573838"
        )

    def test_biorxiv_url_no_www(self):
        assert (
            biorxiv._normalize_doi("https://biorxiv.org/content/10.1101/2024.01.01.573838v1")
            == "10.1101/2024.01.01.573838"
        )

    def test_doi_url_with_query_string(self):
        assert (
            biorxiv._normalize_doi("https://doi.org/10.1101/2024.01.01.573838?ref=foo")
            == "10.1101/2024.01.01.573838"
        )

    def test_doi_url_with_fragment(self):
        assert (
            biorxiv._normalize_doi("https://doi.org/10.1101/2024.01.01.573838#abstract")
            == "10.1101/2024.01.01.573838"
        )

    def test_biorxiv_content_url_with_query_string(self):
        assert (
            biorxiv._normalize_doi(
                "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v1?download=1"
            )
            == "10.1101/2024.01.01.573838"
        )

    def test_biorxiv_content_url_full_pdf_with_query_string(self):
        assert (
            biorxiv._normalize_doi(
                "https://www.medrxiv.org/content/10.1101/2020.09.09.20191205v2.full.pdf?x=1"
            )
            == "10.1101/2020.09.09.20191205"
        )


class TestCanonicalKey:
    def test_lowercases(self):
        assert biorxiv.canonical_key("10.1101/2024.01.01.573838") == "10.1101/2024.01.01.573838"

    def test_url_normalizes_and_lowercases(self):
        assert (
            biorxiv.canonical_key("https://doi.org/10.1101/2024.01.01.573838")
            == "10.1101/2024.01.01.573838"
        )


class TestIsBiorxivDoi:
    def test_biorxiv_doi(self):
        assert biorxiv.is_biorxiv_doi("10.1101/2024.01.01.573838") is True

    def test_non_biorxiv_doi(self):
        assert biorxiv.is_biorxiv_doi("10.1038/s41586-024-00001-1") is False

    def test_acl_doi(self):
        assert biorxiv.is_biorxiv_doi("10.18653/v1/2023.acl-long.1") is False


# ---------------------------------------------------------------------------
# Author parsing
# ---------------------------------------------------------------------------


class TestParseAuthors:
    def test_standard_format(self):
        result = biorxiv._parse_authors("Smith, J.; Doe, Jane A.")
        assert len(result) == 2
        assert result[0]["name"] == "J. Smith"
        assert result[1]["name"] == "Jane A. Doe"

    def test_empty_string(self):
        assert biorxiv._parse_authors("") == []

    def test_single_author(self):
        result = biorxiv._parse_authors("Einstein, A.")
        assert len(result) == 1
        assert result[0]["name"] == "A. Einstein"

    def test_trailing_semicolon(self):
        result = biorxiv._parse_authors("Smith, J.;")
        assert len(result) == 1

    def test_no_comma_format(self):
        result = biorxiv._parse_authors("Consortium, T.")
        assert len(result) == 1
        assert result[0]["name"] == "T. Consortium"


# ---------------------------------------------------------------------------
# Version selection
# ---------------------------------------------------------------------------


class TestPickLatestVersion:
    def test_single_version(self):
        collection = [{"version": "1", "title": "Paper"}]
        assert biorxiv._pick_latest_version(collection)["version"] == "1"

    def test_multiple_versions(self):
        collection = [
            {"version": "1", "title": "Paper v1"},
            {"version": "3", "title": "Paper v3"},
            {"version": "2", "title": "Paper v2"},
        ]
        result = biorxiv._pick_latest_version(collection)
        assert result["version"] == "3"
        assert result["title"] == "Paper v3"


# ---------------------------------------------------------------------------
# Paper parsing
# ---------------------------------------------------------------------------


_SAMPLE_RAW = {
    "doi": "10.1101/2024.01.01.573838",
    "title": "A Great Discovery",
    "authors": "Fujii, S.; Wang, Y.",
    "author_corresponding": "Thaddeus S Stappenbeck",
    "author_corresponding_institution": "Cleveland Clinic",
    "date": "2024-01-02",
    "version": "2",
    "type": "new results",
    "license": "cc_by",
    "category": "cell biology",
    "abstract": "We discovered something great.",
    "server": "bioRxiv",
    "published": "10.1038/s41586-024-00001-1",
    "jatsxml": "https://www.biorxiv.org/content/early/2024/01/02/2024.01.01.573838.source.xml",
    "funder": "NA",
}

_MEDRXIV_RAW = {
    "doi": "10.1101/2020.09.09.20191205",
    "title": "A Medical Study",
    "authors": "Doe, J.",
    "author_corresponding": "J. Doe",
    "author_corresponding_institution": "Hospital",
    "date": "2020-09-10",
    "version": "1",
    "type": "PUBLISHAHEADOFPRINT",
    "license": "cc_no",
    "category": "infectious diseases",
    "abstract": "A medical abstract.",
    "server": "medRxiv",
    "published": "NA",
    "jatsxml": "https://www.medrxiv.org/content/early/2020/09/10/2020.09.09.20191205.source.xml",
    "funder": "NA",
}


class TestParsePaper:
    def test_parses_basic_fields(self):
        result = biorxiv._parse_paper(_SAMPLE_RAW)
        assert result["doi"] == "10.1101/2024.01.01.573838"
        assert result["title"] == "A Great Discovery"
        assert result["date"] == "2024-01-02"
        assert result["version"] == "2"
        assert result["category"] == "cell biology"
        assert result["server"] == "biorxiv"

    def test_parses_authors(self):
        result = biorxiv._parse_paper(_SAMPLE_RAW)
        assert len(result["authors"]) == 2
        assert result["authors"][0]["name"] == "S. Fujii"
        assert result["authors"][1]["name"] == "Y. Wang"

    def test_parses_corresponding_author(self):
        result = biorxiv._parse_paper(_SAMPLE_RAW)
        assert result["author_corresponding"] == "Thaddeus S Stappenbeck"
        assert result["author_corresponding_institution"] == "Cleveland Clinic"

    def test_parses_published_doi(self):
        result = biorxiv._parse_paper(_SAMPLE_RAW)
        assert result["published_doi"] == "10.1038/s41586-024-00001-1"

    def test_published_na_becomes_none(self):
        result = biorxiv._parse_paper(_MEDRXIV_RAW)
        assert result["published_doi"] is None

    def test_published_empty_string_becomes_none(self):
        raw = dict(_SAMPLE_RAW, published="")
        result = biorxiv._parse_paper(raw)
        assert result["published_doi"] is None

    def test_published_missing_becomes_none(self):
        raw = {k: v for k, v in _SAMPLE_RAW.items() if k != "published"}
        result = biorxiv._parse_paper(raw)
        assert result["published_doi"] is None

    def test_biorxiv_pdf_url(self):
        result = biorxiv._parse_paper(_SAMPLE_RAW)
        assert (
            result["pdf_url"]
            == "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v2.full.pdf"
        )

    def test_medrxiv_pdf_url(self):
        result = biorxiv._parse_paper(_MEDRXIV_RAW)
        assert (
            result["pdf_url"]
            == "https://www.medrxiv.org/content/10.1101/2020.09.09.20191205v1.full.pdf"
        )

    def test_medrxiv_server_detection(self):
        result = biorxiv._parse_paper(_MEDRXIV_RAW)
        assert result["server"] == "medrxiv"

    def test_parses_abstract(self):
        result = biorxiv._parse_paper(_SAMPLE_RAW)
        assert result["abstract"] == "We discovered something great."


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class TestThrottledGet:
    @pytest.mark.asyncio
    async def test_first_request_no_delay(self, monkeypatch):
        """First request (when _last_request_time is 0) should not sleep."""
        monkeypatch.setattr(biorxiv, "_last_request_time", 0.0)
        monkeypatch.setattr(biorxiv, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await biorxiv._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 0

    @pytest.mark.asyncio
    async def test_second_request_waits(self, monkeypatch):
        """Second request made immediately should sleep."""
        monkeypatch.setattr(biorxiv, "_last_request_time", time.monotonic())
        monkeypatch.setattr(biorxiv, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await biorxiv._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 1
        assert slept[0] >= 0.3  # should be close to 0.5

    @pytest.mark.asyncio
    async def test_no_delay_after_gap(self, monkeypatch):
        """No sleep needed when enough time has passed."""
        monkeypatch.setattr(biorxiv, "_last_request_time", time.monotonic() - 2.0)
        monkeypatch.setattr(biorxiv, "_request_lock", asyncio.Lock())

        slept = []

        async def mock_sleep(duration):
            slept.append(duration)

        monkeypatch.setattr(asyncio, "sleep", mock_sleep)

        mock_response = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        result = await biorxiv._throttled_get(mock_client, "http://example.com")
        assert result is mock_response
        assert len(slept) == 0


# ---------------------------------------------------------------------------
# Malformed-body / parse-error handling and get_paper integration
# ---------------------------------------------------------------------------


def _reset_biorxiv(monkeypatch, tmp_path):
    """Reset bioRxiv pooled state + cache root and no-op the throttle sleep."""
    from academic_tools_mcp import _singleflight, cache

    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(biorxiv, "_pending", 0)
    monkeypatch.setattr(biorxiv, "_last_request_time", 0.0)
    monkeypatch.setattr(biorxiv, "_request_lock", asyncio.Lock())
    monkeypatch.setattr(biorxiv, "_single_flight", _singleflight.SingleFlight())

    async def mock_sleep(_):
        pass

    monkeypatch.setattr(biorxiv.asyncio, "sleep", mock_sleep)


# Sentinel: a payload whose .json() raises, simulating a malformed/truncated body.
_BAD_JSON = object()


def _stub_json_responses(monkeypatch, *payloads):
    """Install a stub client returning ``payloads`` from successive GETs.

    bioRxiv may issue two GETs per ``get_paper`` (bioRxiv then medRxiv
    fallback), so multiple payloads are supported. A payload of ``_BAD_JSON``
    makes ``.json()`` raise ``json.JSONDecodeError`` (garbled body). Returns a
    1-element list whose ``[0]`` counts GETs.
    """
    import json

    from academic_tools_mcp import _clients

    calls = [0]
    seq = list(payloads)

    class StubResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200
            self.headers: dict[str, str] = {}

        def raise_for_status(self):
            pass

        def json(self):
            if self._payload is _BAD_JSON:
                raise json.JSONDecodeError("Expecting value", "", 0)
            return self._payload

    class StubClient:
        async def get(self, url, **kwargs):
            payload = seq[calls[0]] if calls[0] < len(seq) else seq[-1]
            calls[0] += 1
            return StubResponse(payload)

    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: StubClient())
    return calls


def _collection(**overrides):
    """A minimal valid bioRxiv details collection wrapping one entry."""
    entry = {
        "doi": "10.1101/2024.01.01.573838",
        "title": "A Great Discovery",
        "authors": "Fujii, S.",
        "version": "1",
        "server": "bioRxiv",
        "published": "NA",
    }
    entry.update(overrides)
    return {"collection": [entry]}


class TestGetPaperParseErrors:
    @pytest.mark.asyncio
    async def test_malformed_json_returns_retryable_error(self, tmp_path, monkeypatch):
        # A 200 with a garbled body must NOT crash — it should surface the
        # uniform {error, retryable} contract like every other failure.
        _reset_biorxiv(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await biorxiv.get_paper("10.1101/2024.01.01.573838")

        assert isinstance(result, dict)
        assert "error" in result
        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_bad_version_returns_no_crash(self, tmp_path, monkeypatch):
        # A non-numeric version in a multi-version collection must not raise
        # ValueError out of ``_pick_latest_version`` (the single-entry case
        # short-circuits before int(); only 2+ entries hit the int() sort key).
        _reset_biorxiv(monkeypatch, tmp_path)
        bad = {
            "collection": [
                {
                    "doi": "10.1101/2024.01.01.573838",
                    "title": "v1",
                    "authors": "Fujii, S.",
                    "version": "1",
                    "server": "bioRxiv",
                    "published": "NA",
                },
                {
                    "doi": "10.1101/2024.01.01.573838",
                    "title": "vlatest",
                    "authors": "Fujii, S.",
                    "version": "latest",
                    "server": "bioRxiv",
                    "published": "NA",
                },
            ]
        }
        _stub_json_responses(monkeypatch, bad)

        result = await biorxiv.get_paper("10.1101/2024.01.01.573838")

        assert isinstance(result, dict)
        assert "error" not in result
        # The numeric version sorts above the unparseable one (treated as 0).
        assert result["title"] == "v1"

    @pytest.mark.asyncio
    async def test_parse_failure_not_negative_cached(self, tmp_path, monkeypatch):
        # A parse failure is transient (garbled body), not "not found": it must
        # NOT be negative-cached, so a retry re-fetches.
        from academic_tools_mcp import cache

        _reset_biorxiv(monkeypatch, tmp_path)
        calls = _stub_json_responses(monkeypatch, _BAD_JSON, _BAD_JSON, _BAD_JSON, _BAD_JSON)

        await biorxiv.get_paper("10.1101/2024.01.01.573838")
        canonical = biorxiv.canonical_key("10.1101/2024.01.01.573838")
        assert cache.get_negative(biorxiv.NAMESPACE, "papers", canonical) is None

        first = calls[0]
        await biorxiv.get_paper("10.1101/2024.01.01.573838")
        assert calls[0] > first, "transient parse failure must not be cached; retry re-fetches"


class TestGetPaperIntegration:
    @pytest.mark.asyncio
    async def test_success_parses_collection(self, tmp_path, monkeypatch):
        _reset_biorxiv(monkeypatch, tmp_path)
        _stub_json_responses(monkeypatch, _collection())

        result = await biorxiv.get_paper("10.1101/2024.01.01.573838")

        assert result["title"] == "A Great Discovery"
        assert result["doi"] == "10.1101/2024.01.01.573838"
        assert result["server"] == "biorxiv"

    @pytest.mark.asyncio
    async def test_medrxiv_fallback(self, tmp_path, monkeypatch):
        # Empty bioRxiv response, then a populated medRxiv response.
        _reset_biorxiv(monkeypatch, tmp_path)
        med = _collection(
            doi="10.1101/2020.09.09.20191205", title="A Medical Study", server="medRxiv"
        )
        calls = _stub_json_responses(monkeypatch, {"collection": []}, med)

        result = await biorxiv.get_paper("10.1101/2020.09.09.20191205")

        assert calls[0] == 2, "empty bioRxiv response should trigger medRxiv fallback"
        assert result["title"] == "A Medical Study"
        assert result["server"] == "medrxiv"

    @pytest.mark.asyncio
    async def test_not_found_negative_cached(self, tmp_path, monkeypatch):
        from academic_tools_mcp import cache

        _reset_biorxiv(monkeypatch, tmp_path)
        calls = _stub_json_responses(monkeypatch, {"collection": []}, {"collection": []})

        result = await biorxiv.get_paper("10.1101/9999.99.99.999999")
        assert "error" in result

        canonical = biorxiv.canonical_key("10.1101/9999.99.99.999999")
        assert cache.get_negative(biorxiv.NAMESPACE, "papers", canonical) is not None

        before = calls[0]
        await biorxiv.get_paper("10.1101/9999.99.99.999999")
        assert calls[0] == before, "not-found is negative-cached; retry served from cache"
