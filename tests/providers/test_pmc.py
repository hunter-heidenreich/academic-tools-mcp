"""Unit tests for providers/pmc.py (Europe PMC open-access JATS XML)."""

import httpx
import pytest

from academic_tools_mcp.providers import openalex, pmc
from academic_tools_mcp.store import cache

_JATS_XML = (
    "<article><front><article-meta><title-group><article-title>Sample</article-title>"
    "</title-group></article-meta></front><body><sec><title>Introduction</title>"
    "<p>Hello world.</p></sec></body></article>"
)


def _stub_pmc_client(monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]) -> list[str]:
    seen_urls: list[str] = []
    queue = list(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return queue.pop(0)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(pmc, "_get_client", lambda: client)
    return seen_urls


class TestPmcidNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("PMC1234567", "PMC1234567"),
            ("pmc98765", "PMC98765"),
            ("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4200001/", "PMC4200001"),
            ("  PMC555  ", "PMC555"),
            ("", None),
            ("1234567", None),
            ("not-a-pmcid", None),
        ],
    )
    def test_normalize_pmcid(self, raw: str, expected: str | None) -> None:
        assert pmc.normalize_pmcid(raw) == expected

    def test_extract_pmcid_from_openalex_work(self) -> None:
        work = {"ids": {"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7654321"}}
        assert pmc.extract_pmcid(work) == "PMC7654321"
        assert pmc.extract_pmcid({"ids": {}}) is None
        assert pmc.extract_pmcid({}) is None


class TestGetJats:
    @pytest.mark.asyncio
    async def test_fetches_jats_xml_for_doi_with_pmcid(self, monkeypatch):
        async def fake_work(identifier, *, force_refresh=False):
            return {"ids": {"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1234567"}}

        monkeypatch.setattr(openalex, "get_work", fake_work)
        urls = _stub_pmc_client(
            monkeypatch,
            [httpx.Response(200, text=_JATS_XML)],
        )

        result = await pmc.get_jats("10.1371/journal.pone.0000001")

        assert result == {"markup": _JATS_XML}
        assert urls == ["https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1234567/fullTextXML"]

    @pytest.mark.asyncio
    async def test_no_pmcid_negative_caches_and_skips_second_lookup(self, monkeypatch):
        calls = 0

        async def fake_work(identifier, *, force_refresh=False):
            nonlocal calls
            calls += 1
            return {"ids": {"doi": "https://doi.org/10.1234/no-pmc"}}

        monkeypatch.setattr(openalex, "get_work", fake_work)

        first = await pmc.get_jats("10.1234/no-pmc")
        second = await pmc.get_jats("10.1234/no-pmc")

        assert first["not_found"] is True
        assert second["not_found"] is True
        assert calls == 1
        assert cache.get_negative(pmc.NAMESPACE, "jats", "10.1234/no-pmc") is not None

    @pytest.mark.asyncio
    async def test_openalex_not_found_negative_caches(self, monkeypatch):
        async def fake_work(identifier, *, force_refresh=False):
            return {"error": "No work found", "not_found": True}

        monkeypatch.setattr(openalex, "get_work", fake_work)

        result = await pmc.get_jats("10.1234/missing")
        assert result["not_found"] is True
        assert cache.get_negative(pmc.NAMESPACE, "jats", "10.1234/missing") is not None

    @pytest.mark.asyncio
    async def test_openalex_transient_error_not_negative_cached(self, monkeypatch):
        async def fake_work(identifier, *, force_refresh=False):
            return {"error": "OpenAlex 503", "retryable": True}

        monkeypatch.setattr(openalex, "get_work", fake_work)

        result = await pmc.get_jats("10.1234/transient")
        assert result["retryable"] is True
        assert cache.get_negative(pmc.NAMESPACE, "jats", "10.1234/transient") is None

    @pytest.mark.asyncio
    async def test_europe_pmc_404_negative_caches_and_force_refresh_bypasses(self, monkeypatch):
        async def fake_work(identifier, *, force_refresh=False):
            return {"ids": {"pmcid": "PMC9999999"}}

        monkeypatch.setattr(openalex, "get_work", fake_work)
        urls = _stub_pmc_client(
            monkeypatch,
            [httpx.Response(404), httpx.Response(200, text=_JATS_XML)],
        )

        miss = await pmc.get_jats("10.1234/closed-pmc")
        assert miss["not_found"] is True
        assert len(urls) == 1

        cached_miss = await pmc.get_jats("10.1234/closed-pmc")
        assert cached_miss["not_found"] is True
        assert len(urls) == 1

        refreshed = await pmc.get_jats("10.1234/closed-pmc", force_refresh=True)
        assert refreshed == {"markup": _JATS_XML}
        assert len(urls) == 2

    @pytest.mark.asyncio
    async def test_europe_pmc_503_returns_retryable_without_negative_cache(self, monkeypatch):
        async def fake_work(identifier, *, force_refresh=False):
            return {"ids": {"pmcid": "PMC1111111"}}

        monkeypatch.setattr(openalex, "get_work", fake_work)
        _stub_pmc_client(
            monkeypatch,
            [httpx.Response(503), httpx.Response(503)],
        )

        result = await pmc.get_jats("10.1234/outage")
        assert result["retryable"] is True
        assert cache.get_negative(pmc.NAMESPACE, "jats", "10.1234/outage") is None

    @pytest.mark.asyncio
    async def test_non_jats_body_returns_parse_error(self, monkeypatch):
        async def fake_work(identifier, *, force_refresh=False):
            return {"ids": {"pmcid": "PMC2222222"}}

        monkeypatch.setattr(openalex, "get_work", fake_work)
        _stub_pmc_client(monkeypatch, [httpx.Response(200, text="<html>not xml</html>")])

        result = await pmc.get_jats("10.1234/bad-xml")
        assert "error" in result
        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_accepts_openalex_work_id(self, monkeypatch):
        async def fake_work(identifier, *, force_refresh=False):
            assert identifier == "W4312223440"
            return {"ids": {"pmcid": "PMC3333333"}}

        monkeypatch.setattr(openalex, "get_work", fake_work)
        _stub_pmc_client(monkeypatch, [httpx.Response(200, text=_JATS_XML)])

        result = await pmc.get_jats("W4312223440")
        assert result == {"markup": _JATS_XML}
