import asyncio
import gzip
import os
import time
from urllib.parse import quote, urlsplit

import httpx
import pytest

from academic_tools_mcp import manual
from academic_tools_mcp.net import clients
from academic_tools_mcp.providers import acl
from academic_tools_mcp.store import cache, stems

# ---------------------------------------------------------------------------
# Identifier shapes
# ---------------------------------------------------------------------------


class TestIsAnthologyId:
    @pytest.mark.parametrize(
        "identifier",
        [
            "P16-1160",
            "p16-1160",
            "W04-1013",
            "2023.acl-long.1",
            "2023.ACL-LONG.1",
            "2022.findings-emnlp.100",
            "10.18653/v1/2023.acl-long.1",
            "https://doi.org/10.18653/v1/P16-1160",
            "doi:10.18653/V1/2023.ACL-LONG.1",
            "10.3115/v1/W15-2301",
            "https://aclanthology.org/W04-1013/",
            "https://aclanthology.org/W04-1013",
            "aclanthology.org/2023.acl-long.1.pdf",
            "https://aclanthology.org/2023.acl-long.1.bib",
            "https://www.aclweb.org/anthology/P16-1160.pdf",
            "http://aclweb.org/anthology/P/P16/P16-1160.pdf",
        ],
    )
    def test_claimed(self, identifier):
        assert acl.is_anthology_id(identifier) is True

    @pytest.mark.parametrize(
        "identifier",
        [
            "P16-1",  # a volume, not a paper
            "10.18653/v1/W17-47",  # a volume DOI
            "10.18653/v1/",
            "10.18653/v1/foo;bar",
            "B12-3456",  # a letter the Anthology never used
            "PP16-1160",
            "P1-1160",
            "P16-11600",
            "2301.00001",  # arXiv
            "10.1162/tacl.a.63",  # hosted, but opaque: the index's job
            "10.1038/nature12373",
            "https://aclanthology.org/people/chin-yew-lin/",
            "",
        ],
    )
    def test_not_claimed(self, identifier):
        assert acl.is_anthology_id(identifier) is False


class TestNormalizeAnthologyId:
    @pytest.mark.parametrize(
        ("identifier", "expected"),
        [
            ("p16-1160", "P16-1160"),
            ("10.18653/v1/w04-1013", "W04-1013"),
            ("2023.ACL-long.1", "2023.acl-long.1"),
            ("10.18653/V1/2023.ACL-LONG.1", "2023.acl-long.1"),
            ("  10.18653/v1/2023.acl-long.1  ", "2023.acl-long.1"),
            ("https://aclanthology.org/2023.acl-long.1.xml", "2023.acl-long.1"),
            ("10.3115/V1/w15-2301", "W15-2301"),
        ],
    )
    def test_spellings(self, identifier, expected):
        assert acl.normalize_anthology_id(identifier) == expected

    def test_a_non_id_passes_through_doi_normalized(self):
        assert acl.normalize_anthology_id("doi:10.1038/X") == "10.1038/X"

    def test_canonical_key_is_the_normalized_id(self):
        assert acl.canonical_key("10.18653/v1/p16-1160") == "P16-1160"


class TestStripAclPrefix:
    def test_both_registrants(self):
        assert acl._strip_acl_prefix("10.18653/v1/x") == "x"
        assert acl._strip_acl_prefix("10.3115/V1/x") == "x"

    @pytest.mark.parametrize("doi", ["", "10.18653/v1", "10.186530/v1/x", "10.18653/v1/   "])
    def test_no_suffix(self, doi):
        assert acl._strip_acl_prefix(doi) is None

    def test_is_acl_doi_reads_the_prefix_only(self):
        assert acl.is_acl_doi("https://doi.org/10.18653/v1/P16-1160") is True
        assert acl.is_acl_doi("P16-1160") is False

    def test_bare_prefix_falls_through_to_the_generic_doi_route(self):
        assert manual.resolve_target("10.18653/v1/")["namespace"] == manual.NAMESPACE
        assert manual.resolve_metadata_source("10.18653/v1/") == "openalex"


class TestParseId:
    @pytest.mark.parametrize(
        ("anthology_id", "parts"),
        [
            ("2022.acl-main.1", ("2022.acl", "main", "1")),
            ("P16-1160", ("P16", "1", "160")),
            ("P18-1007", ("P18", "1", "7")),
            ("W18-6310", ("W18", "63", "10")),
            ("W04-1013", ("W04", "10", "13")),
            ("W18-0501", ("W18", "5", "1")),
            ("D19-1001", ("D19", "1", "1")),
            ("D19-5702", ("D19", "57", "2")),
            ("C69-0101", ("C69", "1", "1")),
            ("P18-1000", ("P18", "1", "0")),
        ],
    )
    def test_matches_upstream(self, anthology_id, parts):
        assert acl.parse_id(anthology_id) == parts
        assert acl._build_id(*parts) == anthology_id


# ---------------------------------------------------------------------------
# PDF paths and URLs
# ---------------------------------------------------------------------------


class TestPdfPath:
    """Every ACL artifact keys on the Anthology ID, whichever spelling reached it."""

    def test_keys_on_the_anthology_id(self):
        path = acl.pdf_path("10.18653/v1/p16-1160")
        assert path.parent == cache.cache_dir(acl.NAMESPACE, "pdfs")
        assert path.name == "P16-1160.pdf"

    def test_every_spelling_shares_one_path(self):
        assert (
            acl.pdf_path("10.18653/V1/P16-1160")
            == acl.pdf_path("https://aclanthology.org/P16-1160/")
            == acl.pdf_path("p16-1160")
        )

    def test_agrees_with_the_markdown_stem(self):
        key = acl.canonical_key("2023.acl-long.1")
        assert acl.pdf_path(key).stem == stems.markdown_path(acl.NAMESPACE, key).stem

    @pytest.mark.parametrize("identifier", ["10.1038/s41586-021-03819-2", "10.18653/v1/", "P16-1"])
    def test_anything_else_raises(self, identifier):
        # Must not return a sentinel path whose .exists() could be True.
        with pytest.raises(ValueError):
            acl.pdf_path(identifier)


class TestPdfUrlEncoding:
    @pytest.mark.parametrize("aid", ["2023.acl-long.1", "P16-1160"])
    def test_real_ids_are_unchanged(self, aid):
        assert acl.pdf_url(aid) == f"https://aclanthology.org/{aid}.pdf"

    @pytest.mark.parametrize("aid", ["x?y", "x#y", "x y", "a/b"])
    def test_url_metacharacters_stay_in_one_path_segment(self, aid):
        parts = urlsplit(acl.pdf_url(aid))
        assert parts.query == ""
        assert parts.fragment == ""
        assert parts.path == "/" + quote(aid, safe="") + ".pdf"


# ---------------------------------------------------------------------------
# Transport fakes
# ---------------------------------------------------------------------------


def _stub_routes(monkeypatch, routes):
    """Serve each request from the first ``routes`` key its URL contains.

    A value is ``(status, body_bytes)`` or an ``httpx`` exception to raise; a URL
    no key matches is a 599 the test did not expect. Returns the captured requests.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        for fragment, answer in routes.items():
            if fragment in str(request.url):
                if isinstance(answer, Exception):
                    raise answer
                status, body = answer
                return httpx.Response(status, content=body)
        return httpx.Response(599, content=b"unexpected")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: client)
    return requests


_P16 = b"""<?xml version='1.0' encoding='UTF-8'?>
<collection id="P16">
  <volume id="1" type="proceedings">
    <meta>
      <booktitle>Proceedings of the 54th Annual Meeting of the <fixed-case>ACL</fixed-case></booktitle>
      <editor><first>Katrin</first><last>Erk</last></editor>
      <publisher>Association for Computational Linguistics</publisher>
      <address>Berlin, Germany</address>
      <month>August</month>
      <year>2016</year>
      <venue>acl</venue>
    </meta>
    <frontmatter>
      <bibkey>acl-2016-long</bibkey>
    </frontmatter>
    <paper id="160">
      <title>A Character-level Decoder for <fixed-case>NMT</fixed-case></title>
      <author orcid="0000-0002-1825-0097"><first>Junyoung</first><last>Chung</last>
        <affiliation>NYU</affiliation></author>
      <author><first>Kyunghyun</first><last>Cho</last></author>
      <pages>1693\xe2\x80\x931703</pages>
      <abstract>We use <i>no</i> segmentation <tex-math>x^2</tex-math>.</abstract>
      <doi>10.18653/v1/P16-1160</doi>
      <bibkey>chung-etal-2016-character</bibkey>
    </paper>
    <paper id="161">
      <title>Sibling</title>
    </paper>
  </volume>
</collection>
"""


# ---------------------------------------------------------------------------
# get_paper
# ---------------------------------------------------------------------------


class TestGetPaper:
    @pytest.mark.asyncio
    async def test_parses_the_record(self, monkeypatch):
        requests = _stub_routes(monkeypatch, {"/data/xml/P16.xml": (200, _P16)})

        paper = await acl.get_paper("10.18653/v1/p16-1160")

        assert str(requests[0].url).endswith("/data/xml/P16.xml")
        assert paper["anthology_id"] == "P16-1160"
        assert paper["title"] == "A Character-level Decoder for NMT"
        assert paper["abstract"] == "We use no segmentation x^2."
        assert paper["authors"][0] == {
            "name": "Junyoung Chung",
            "first": "Junyoung",
            "last": "Chung",
            "orcid": "0000-0002-1825-0097",
            "affiliation": "NYU",
        }
        assert paper["editors"][0]["name"] == "Katrin Erk"
        assert paper["booktitle"] == "Proceedings of the 54th Annual Meeting of the ACL"
        assert paper["volume_type"] == "proceedings"
        assert (paper["year"], paper["month"], paper["pages"]) == ("2016", "August", "1693–1703")
        assert paper["doi"] == "10.18653/v1/P16-1160"
        assert paper["pdf_url"] == "https://aclanthology.org/P16-1160.pdf"

    @pytest.mark.asyncio
    async def test_one_collection_fetch_serves_every_paper_in_it(self, monkeypatch):
        requests = _stub_routes(monkeypatch, {"P16.xml": (200, _P16)})

        await acl.get_paper("P16-1160")
        sibling = await acl.get_paper("P16-1161")
        frontmatter = await acl.get_paper("P16-1000")

        assert len(requests) == 1
        assert sibling["title"] == "Sibling"
        assert frontmatter["bibkey"] == "acl-2016-long"

    @pytest.mark.asyncio
    async def test_concurrent_papers_share_one_collection_fetch(self, monkeypatch):
        requests = _stub_routes(monkeypatch, {"P16.xml": (200, _P16)})

        await asyncio.gather(acl.get_paper("P16-1160"), acl.get_paper("P16-1161"))

        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_a_paper_missing_from_its_collection_is_negative_cached_briefly(
        self, monkeypatch
    ):
        requests = _stub_routes(monkeypatch, {"P16.xml": (200, _P16)})

        first = await acl.get_paper("P16-1999")
        second = await acl.get_paper("P16-1999")

        assert first["not_found"] is True
        assert second["not_found"] is True
        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_a_missing_collection_is_negative_cached(self, monkeypatch):
        requests = _stub_routes(monkeypatch, {"Q99.xml": (404, b"")})

        assert (await acl.get_paper("Q99-1001"))["not_found"] is True
        assert (await acl.get_paper("Q99-1002"))["not_found"] is True
        assert len(requests) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            b"<collection id='P16'><volume",  # truncated
            b"<html>rate limited</html>",  # wrong root
            b"<collection id='W04'><volume id='1'/></collection>",  # another collection
            b"<collection id='P16'></collection>",  # no volumes
        ],
    )
    async def test_a_bad_body_is_transient_and_uncached(self, monkeypatch, body):
        requests = _stub_routes(monkeypatch, {"P16.xml": (200, body)})

        result = await acl.get_paper("P16-1160")
        await acl.get_paper("P16-1160")

        assert result["retryable"] is True
        assert "not_found" not in result
        assert len(requests) == 2

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_retryable(self, monkeypatch):
        _stub_routes(monkeypatch, {"P16.xml": httpx.ConnectError("down")})

        result = await acl.get_paper("P16-1160")

        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_force_refresh_refetches_the_collection(self, monkeypatch):
        requests = _stub_routes(monkeypatch, {"P16.xml": (200, _P16)})

        await acl.get_paper("P16-1160")
        await acl.get_paper("P16-1160", force_refresh=True)

        assert len(requests) == 2

    @pytest.mark.asyncio
    async def test_force_refresh_clears_a_negative_collection(self, monkeypatch):
        routes = {"P16.xml": (404, b"")}
        requests = _stub_routes(monkeypatch, routes)
        await acl.get_paper("P16-1160")

        routes["P16.xml"] = (200, _P16)
        paper = await acl.get_paper("P16-1160", force_refresh=True)

        assert paper["anthology_id"] == "P16-1160"
        assert len(requests) == 2

    @pytest.mark.asyncio
    async def test_a_non_id_is_refused_without_a_request(self, monkeypatch):
        requests = _stub_routes(monkeypatch, {})

        result = await acl.get_paper("10.1038/nature12373")

        assert result["not_found"] is True
        assert requests == []


# ---------------------------------------------------------------------------
# Hosted-DOI index
# ---------------------------------------------------------------------------


def _dump(*entries):
    """A gzipped anthology.bib holding ``(anthology_id, doi)`` entries."""
    text = "".join(
        f'@inproceedings{{k{i},\n    url = "https://aclanthology.org/{aid}/",\n'
        f'    doi = "{doi}",\n}}\n'
        for i, (aid, doi) in enumerate(entries)
    )
    return gzip.compress(text.encode())


_DUMP = _dump(
    ("2026.tacl-1.1", "10.1162/tacl.a.63"),
    ("P04-1077", "10.3115/1218955.1219032"),
    ("P16-1160", "10.18653/v1/P16-1160"),  # derivable: left out
)


@pytest.mark.real_doi_index
class TestAnthologyIdForDoi:
    @pytest.mark.asyncio
    async def test_a_hosted_doi_resolves(self, monkeypatch):
        _stub_routes(monkeypatch, {"anthology.bib.gz": (200, _DUMP)})

        assert (
            await acl.anthology_id_for_doi("https://doi.org/10.1162/TACL.a.63") == "2026.tacl-1.1"
        )
        assert await acl.anthology_id_for_doi("10.3115/1218955.1219032") == "P04-1077"

    @pytest.mark.asyncio
    async def test_derivable_dois_are_not_indexed(self, monkeypatch):
        _stub_routes(monkeypatch, {"anthology.bib.gz": (200, _DUMP)})

        await acl.anthology_id_for_doi("10.1162/tacl.a.63")
        meta = cache.get(acl.NAMESPACE, "doi_index", "meta")

        assert meta["registrants"] == ["10.1162", "10.3115"]

    @pytest.mark.asyncio
    async def test_the_dump_is_fetched_once_and_an_unknown_registrant_costs_nothing(
        self, monkeypatch
    ):
        requests = _stub_routes(monkeypatch, {"anthology.bib.gz": (200, _DUMP)})

        assert await acl.anthology_id_for_doi("10.1162/tacl.a.63") is not None
        assert await acl.anthology_id_for_doi("10.1038/nature12373") is None
        assert await acl.anthology_id_for_doi("10.1162/other") is None

        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_a_non_doi_makes_no_request(self, monkeypatch):
        requests = _stub_routes(monkeypatch, {})

        assert await acl.anthology_id_for_doi("P16-1160") is None
        assert requests == []

    @pytest.mark.asyncio
    async def test_a_failed_refresh_keeps_the_stale_index(self, monkeypatch):
        routes = {"anthology.bib.gz": (200, _DUMP)}
        _stub_routes(monkeypatch, routes)
        await acl.anthology_id_for_doi("10.1162/tacl.a.63")

        meta = cache.get(acl.NAMESPACE, "doi_index", "meta")
        meta["fetched_at"] = time.time() - acl._DOI_INDEX_MAX_AGE_SECONDS - 1
        cache.put(acl.NAMESPACE, "doi_index", "meta", meta)
        routes["anthology.bib.gz"] = httpx.ConnectError("down")

        assert await acl.anthology_id_for_doi("10.1162/tacl.a.63") == "2026.tacl-1.1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [httpx.ConnectError("down"), (200, b"not gzip"), (200, gzip.compress(b"no entries"))],
    )
    async def test_a_failure_is_remembered_so_the_next_caller_skips_it(self, monkeypatch, answer):
        requests = _stub_routes(monkeypatch, {"anthology.bib.gz": answer})

        assert await acl.anthology_id_for_doi("10.1162/tacl.a.63") is None
        attempts = len(requests)
        assert await acl.anthology_id_for_doi("10.1162/tacl.a.63") is None

        assert len(requests) == attempts
        assert cache.get(acl.NAMESPACE, "doi_index", "meta") is None

    @pytest.mark.asyncio
    async def test_the_failure_record_expires(self, monkeypatch):
        routes = {"anthology.bib.gz": httpx.ConnectError("down")}
        _stub_routes(monkeypatch, routes)
        await acl.anthology_id_for_doi("10.1162/tacl.a.63")

        path = cache._entry_path(acl.NAMESPACE, "doi_index", "last_failure")
        old = time.time() - acl._DOI_INDEX_RETRY_SECONDS - 1
        os.utime(path, (old, old))
        routes["anthology.bib.gz"] = (200, _DUMP)

        assert await acl.anthology_id_for_doi("10.1162/tacl.a.63") == "2026.tacl-1.1"


# ---------------------------------------------------------------------------
# download_pdf
# ---------------------------------------------------------------------------


def _fake_stream(monkeypatch, result=None):
    """Replace ``stream_to_file``; writes a PDF unless ``result`` is an error. Returns URLs."""
    from academic_tools_mcp.download import streaming

    calls: list[str] = []

    async def fake_stream(client, url, dest, **kwargs):
        calls.append(url)
        if result is not None:
            return dict(result)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-1.4 acl")
        return {"path": str(dest), "size_bytes": dest.stat().st_size, "cached": False}

    monkeypatch.setattr(streaming, "stream_to_file", fake_stream)
    return calls


class TestDownloadPdf:
    @pytest.mark.asyncio
    async def test_fresh_and_cached_payloads_agree(self, monkeypatch):
        calls = _fake_stream(monkeypatch)

        fresh = await acl.download_pdf("10.18653/v1/p16-1160")
        cached = await acl.download_pdf("https://aclanthology.org/P16-1160/")

        assert (fresh["cached"], cached["cached"]) == (False, True)
        for key in ("anthology_id", "pdf_url"):
            assert fresh[key] == cached[key]
        assert fresh["anthology_id"] == "P16-1160"
        assert calls == ["https://aclanthology.org/P16-1160.pdf"]

    @pytest.mark.asyncio
    async def test_a_404_is_negative_cached_under_the_anthology_id(self, monkeypatch):
        calls = _fake_stream(monkeypatch, {"error": "PDF not found", "retryable": False})

        await acl.download_pdf("W04-1013")
        await acl.download_pdf("w04-1013")

        assert len(calls) == 1
        assert cache.get_negative(acl.NAMESPACE, "downloads", "W04-1013") is not None

    @pytest.mark.asyncio
    async def test_a_retryable_failure_is_not_negative_cached(self, monkeypatch):
        calls = _fake_stream(monkeypatch, {"error": "network error", "retryable": True})

        await acl.download_pdf("W04-1013")
        await acl.download_pdf("W04-1013")

        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_force_refresh_drops_the_negative_entry(self, monkeypatch):
        calls = _fake_stream(monkeypatch, {"error": "PDF not found", "retryable": False})

        await acl.download_pdf("W04-1013")
        await acl.download_pdf("W04-1013", force_refresh=True)

        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_an_error_carries_no_provenance(self, monkeypatch):
        _fake_stream(monkeypatch, {"error": "PDF not found", "retryable": False})

        assert "anthology_id" not in await acl.download_pdf("W04-1013")

    @pytest.mark.asyncio
    async def test_a_non_anthology_identifier_is_rejected_before_any_fetch(self, monkeypatch):
        calls = _fake_stream(monkeypatch)

        result = await acl.download_pdf("10.1038/nature12373")

        assert result["not_found"] is True
        assert calls == []

    @pytest.mark.asyncio
    async def test_concurrent_downloads_collapse_to_one_stream(self, monkeypatch):
        from academic_tools_mcp.download import streaming

        started = 0
        release = asyncio.Event()

        async def fake_stream(client, url, dest, **kwargs):
            nonlocal started
            started += 1
            await release.wait()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4 acl")
            return {"path": str(dest), "size_bytes": 12, "cached": False}

        monkeypatch.setattr(streaming, "stream_to_file", fake_stream)

        tasks = [asyncio.create_task(acl.download_pdf("2023.acl-long.1")) for _ in range(5)]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks)

        assert started == 1
        # `tools/pipeline` writes into what it receives, so followers need copies.
        results[0]["cascaded_invalidated"] = ["markdown"]
        assert all("cascaded_invalidated" not in r for r in results[1:])

    @pytest.mark.asyncio
    async def test_a_download_and_a_metadata_fetch_do_not_share_a_slot(self, monkeypatch):
        """One `SingleFlight` carries every key, so the PDF's must be namespaced."""
        _fake_stream(monkeypatch)
        _stub_routes(monkeypatch, {"P16.xml": (200, _P16)})

        pdf, paper = await asyncio.gather(acl.download_pdf("P16-1160"), acl.get_paper("P16-1160"))

        assert pdf["anthology_id"] == paper["anthology_id"] == "P16-1160"
