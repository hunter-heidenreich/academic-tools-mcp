import json
from typing import Any, NamedTuple

import httpx
import pytest

from academic_tools_mcp.net import clients, http
from academic_tools_mcp.providers import biorxiv
from academic_tools_mcp.store import cache

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


class TestVersionlessIdentity:
    """A bioRxiv DOI names the paper, not a version — the opposite of arXiv.

    bioRxiv mints one DOI for every version, the details API is only ever asked
    for the whole set (``/na/``) and ``_pick_latest_version`` takes the newest,
    so a version-suffixed key names no distinct record. Upstream rejects one
    outright ("DOI not recognizable"), which the details endpoint reports as a
    well-formed *empty* collection — i.e. it would be negative-cached as a
    definitive miss.
    """

    @pytest.mark.parametrize(
        "spelling",
        [
            "10.1101/2024.01.01.573838v1",
            "10.1101/2024.01.01.573838v12",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v1",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v1/",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v2.full",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v2.full.pdf",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v1.abstract",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v1.full-text",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v1.supplementary-material",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v1.article-info",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838.full.pdf",
            "HTTPS://WWW.BIORXIV.ORG/content/10.1101/2024.01.01.573838v1",
            "https://www.BioRxiv.org/content/10.1101/2024.01.01.573838v1",
            "www.biorxiv.org/content/10.1101/2024.01.01.573838v1",
            "biorxiv.org/content/10.1101/2024.01.01.573838v1",
            "https://www.medrxiv.org/content/10.1101/2024.01.01.573838v3.full.pdf",
        ],
    )
    def test_every_rendering_collapses_to_the_bare_doi(self, spelling):
        assert biorxiv.canonical_key(spelling) == "10.1101/2024.01.01.573838"

    @pytest.mark.parametrize(
        "spelling",
        [
            "HTTPS://WWW.BIORXIV.ORG/content/10.1101/2024.01.01.573838v1",
            "www.biorxiv.org/content/10.1101/2024.01.01.573838v1",
            "https://www.biorxiv.org/content/10.1101/2024.01.01.573838v1/",
            "10.1101/2024.01.01.573838v1",
        ],
    )
    def test_a_spelling_the_shape_test_misses_is_routed_elsewhere(self, spelling):
        # A spelling `is_biorxiv_doi` rejects is not merely unfetchable: manual
        # files it under its own namespace with a key that is already
        # biorxiv's, so the same paper caches, downloads and converts twice.
        from academic_tools_mcp import manual

        assert biorxiv.is_biorxiv_doi(spelling) is True
        assert manual.resolve_target(spelling)["namespace"] == biorxiv.NAMESPACE
        assert manual.resolve_metadata_source(spelling) == "biorxiv"

    @pytest.mark.parametrize(
        "other",
        ["10.1038/nature12373", "10.18653/v1/P16-1160", "10.5555/x.full", "10.1234/abcv2"],
    )
    def test_a_foreign_doi_is_left_alone(self, other):
        # The version rule is gated on the 10.1101/ prefix: `_normalize_doi`
        # also sees every non-bioRxiv DOI, through `is_biorxiv_doi`.
        assert biorxiv._normalize_doi(other) == other
        assert biorxiv.is_biorxiv_doi(other) is False


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

    def test_initials_first(self):
        result = biorxiv._parse_authors("Consortium, T.")
        assert len(result) == 1
        assert result[0]["name"] == "T. Consortium"

    def test_no_comma_is_taken_verbatim(self):
        # The genuine `else: name = part` branch — a corporate author has no
        # "Last, First" to invert.
        assert biorxiv._parse_authors("The Tabula Muris Consortium") == [
            {"name": "The Tabula Muris Consortium"}
        ]

    def test_trailing_comma_keeps_only_the_surname(self):
        # The empty-`first` arm of `f"{first} {last}" if first else last`.
        assert biorxiv._parse_authors("Smith,") == [{"name": "Smith"}]

    def test_leading_comma_keeps_only_the_forename(self):
        assert biorxiv._parse_authors(", J.") == [{"name": "J."}]

    def test_second_comma_stays_with_the_forename(self):
        # partition(",") splits once, so a suffix rides along with the given
        # name rather than being read as a third field.
        assert biorxiv._parse_authors("Smith, J., Jr.") == [{"name": "J., Jr. Smith"}]

    def test_whitespace_only_string(self):
        assert biorxiv._parse_authors("   ") == []

    def test_separators_only(self):
        assert biorxiv._parse_authors(";;;") == []

    def test_interior_blank_part_is_dropped(self):
        result = biorxiv._parse_authors("Doe, A.; ; Roe, B.")
        assert [a["name"] for a in result] == ["A. Doe", "B. Roe"]

    def test_many_authors(self):
        result = biorxiv._parse_authors("A, X.; B, Y.; C, Z.; D, W.")
        assert [a["name"] for a in result] == ["X. A", "Y. B", "Z. C", "W. D"]

    def test_falsy_non_string_is_empty(self):
        # `authors` present as JSON null reaches here from _parse_paper; the
        # falsy guard catches it before `.split` would raise AttributeError,
        # which is in neither _PARSE_ERRORS nor HTTPX_ERRORS.
        assert biorxiv._parse_authors(None) == []


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
# Malformed-body / parse-error handling and get_paper integration
# ---------------------------------------------------------------------------
#
# The rate-limiter gap, concurrency cap, and burst-cap backpressure are no
# longer per-provider code — they live in ``throttle.Throttle`` and are
# covered once in tests/test_throttle.py.


class TestSafeVersion:
    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            ({"version": "3"}, 3),
            ({"version": "03"}, 3),
            ({"version": 3}, 3),
            ({}, 0),
            ({"version": None}, 0),
            ({"version": ""}, 0),
            ({"version": "latest"}, 0),
            ({"version": "2.5"}, 0),
            ({"version": ["1"]}, 0),
        ],
    )
    def test_junk_sorts_to_the_bottom(self, entry, expected):
        assert biorxiv._safe_version(entry) == expected


class TestCollectionOf:
    """The shape guard: ``None`` is 'wrong shape', a list is 'well-formed'.

    Only the second is evidence about whether the DOI exists, and only a
    well-formed *empty* one is negative-cached.
    """

    @pytest.mark.parametrize(
        "body",
        [None, 5, "a string", [1, 2, 3], True, {}, {"collection": None}, {"collection": {"a": 1}}],
    )
    def test_wrong_shape_is_none(self, body):
        assert biorxiv._collection_of(body) is None

    def test_all_non_dict_entries_is_malformed_not_empty(self):
        assert biorxiv._collection_of({"collection": [1, "two"]}) is None

    def test_a_mixed_list_salvages_the_usable_entries(self):
        assert biorxiv._collection_of({"collection": [1, {"a": 1}, "two"]}) == [{"a": 1}]

    def test_well_formed_empty_is_a_list(self):
        assert biorxiv._collection_of({"collection": []}) == []

    def test_the_real_not_found_body_is_well_formed_empty(self):
        # What api.biorxiv.org actually answers for an unknown DOI: a 200
        # carrying a status message *and* an empty collection.
        body = {"messages": [{"status": "no posts found"}], "collection": []}
        assert biorxiv._collection_of(body) == []


def _reset_biorxiv(monkeypatch):
    """Zero the throttle gap so a two-request test doesn't wait it out.

    Cache root and single-flight state are already reset per test by the
    conftest autouse fixtures; only the gap is this module's business.
    """
    monkeypatch.setattr(biorxiv._throttle, "min_gap_seconds", 0.0)


# Sentinel: a 200 whose body is not JSON, simulating a malformed/truncated body.
_BAD_JSON = object()


class _Resp(NamedTuple):
    """A payload with a non-200 status, for the HTTP-error branch."""

    status_code: int
    payload: Any = None


def _stub_json_responses(monkeypatch, *payloads):
    """Serve ``payloads`` from successive GETs over a real MockTransport.

    A genuine transport rather than a hand-rolled stub: ``status_code`` and
    ``raise_for_status`` are real, so ``http.HTTPX_ERRORS`` is reachable and
    the request URL is assertable. bioRxiv may issue two GETs per ``get_paper``
    (bioRxiv then the medRxiv fallback). A payload may be ``_BAD_JSON``, a
    ``_Resp`` carrying a status code, or an ``httpx`` exception to raise.
    Returns the list of captured requests.
    """
    requests: list[httpx.Request] = []
    seq = list(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = seq[len(requests)] if len(requests) < len(seq) else seq[-1]
        requests.append(request)
        if isinstance(payload, Exception):
            raise payload
        status = 200
        if isinstance(payload, _Resp):
            status, payload = payload.status_code, payload.payload
        body = b"{ truncated" if payload is _BAD_JSON else json.dumps(payload).encode()
        return httpx.Response(status, headers={"content-type": "application/json"}, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: client)
    return requests


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
    async def test_malformed_json_returns_retryable_error(self, monkeypatch):
        # A 200 with a garbled body must NOT crash — it should surface the
        # uniform {error, retryable} contract like every other failure.
        _reset_biorxiv(monkeypatch)
        _stub_json_responses(monkeypatch, _BAD_JSON)

        result = await biorxiv.get_paper("10.1101/2024.01.01.573838")

        assert isinstance(result, dict)
        assert "error" in result
        assert result.get("retryable") is True

    @pytest.mark.asyncio
    async def test_bad_version_returns_no_crash(self, monkeypatch):
        # A non-numeric version in a multi-version collection must not raise
        # ValueError out of ``_pick_latest_version`` (the single-entry case
        # short-circuits before int(); only 2+ entries hit the int() sort key).
        _reset_biorxiv(monkeypatch)
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
    async def test_parse_failure_not_negative_cached(self, monkeypatch):
        # A parse failure is transient (garbled body), not "not found": it must
        # NOT be negative-cached, so a retry re-fetches.

        _reset_biorxiv(monkeypatch)
        calls = _stub_json_responses(monkeypatch, _BAD_JSON, _BAD_JSON, _BAD_JSON, _BAD_JSON)

        await biorxiv.get_paper("10.1101/2024.01.01.573838")
        canonical = biorxiv.canonical_key("10.1101/2024.01.01.573838")
        assert cache.get_negative(biorxiv.NAMESPACE, "papers", canonical) is None

        first = len(calls)
        await biorxiv.get_paper("10.1101/2024.01.01.573838")
        assert len(calls) > first, "transient parse failure must not be cached; retry re-fetches"


class TestGetPaperIntegration:
    @pytest.mark.asyncio
    async def test_success_parses_collection(self, monkeypatch):
        _reset_biorxiv(monkeypatch)
        _stub_json_responses(monkeypatch, _collection())

        result = await biorxiv.get_paper("10.1101/2024.01.01.573838")

        assert result["title"] == "A Great Discovery"
        assert result["doi"] == "10.1101/2024.01.01.573838"
        assert result["server"] == "biorxiv"

    @pytest.mark.asyncio
    async def test_medrxiv_fallback(self, monkeypatch):
        # Empty bioRxiv response, then a populated medRxiv response.
        _reset_biorxiv(monkeypatch)
        med = _collection(
            doi="10.1101/2020.09.09.20191205", title="A Medical Study", server="medRxiv"
        )
        calls = _stub_json_responses(monkeypatch, {"collection": []}, med)

        result = await biorxiv.get_paper("10.1101/2020.09.09.20191205")

        assert len(calls) == 2, "empty bioRxiv response should trigger medRxiv fallback"
        assert result["title"] == "A Medical Study"
        assert result["server"] == "medrxiv"

    @pytest.mark.asyncio
    async def test_not_found_negative_cached(self, monkeypatch):

        _reset_biorxiv(monkeypatch)
        calls = _stub_json_responses(monkeypatch, {"collection": []}, {"collection": []})

        result = await biorxiv.get_paper("10.1101/9999.99.99.999999")
        assert "error" in result

        canonical = biorxiv.canonical_key("10.1101/9999.99.99.999999")
        assert cache.get_negative(biorxiv.NAMESPACE, "papers", canonical) is not None

        before = len(calls)
        await biorxiv.get_paper("10.1101/9999.99.99.999999")
        assert len(calls) == before, "not-found is negative-cached; retry served from cache"


class TestMalformedPayloadShape:
    """A 200 whose body is valid JSON but the wrong shape must surface the
    uniform {error, retryable} contract, not raise.

    ``data.get("collection", [])`` raises AttributeError on a JSON ``null`` or
    scalar, and a collection of non-dicts raises inside ``_pick_latest_version``.
    Neither AttributeError nor TypeError is in ``_PARSE_ERRORS`` or
    ``HTTPX_ERRORS``, so both escaped the provider entirely.
    """

    @pytest.fixture(autouse=True)
    def _no_gap(self, monkeypatch):
        # These cases issue up to three GETs each; without this they pay the
        # real 0.5s inter-request gap and dominate the suite's runtime.
        _reset_biorxiv(monkeypatch)

    @pytest.mark.parametrize("payload", [None, 5, "a string", [1, 2, 3], True])
    @pytest.mark.asyncio
    async def test_get_paper_does_not_raise(self, monkeypatch, payload):
        # Both the bioRxiv call and the medRxiv fallback get the bad payload.
        _stub_json_responses(monkeypatch, payload, payload)

        result = await biorxiv.get_paper("10.1101/2024.01.01.573838")

        assert "error" in result, f"{payload!r} escaped the error contract"

    @pytest.mark.asyncio
    async def test_collection_of_non_dicts_does_not_raise(self, monkeypatch):
        bad = {"collection": [1, 2, "three"]}
        _stub_json_responses(monkeypatch, bad, bad)

        result = await biorxiv.get_paper("10.1101/2024.01.01.573838")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_null_server_field_does_not_raise(self, monkeypatch):
        # `raw.get("server", "").lower()` returned None for a present-but-null
        # key; the sibling `published` field already handled this correctly.
        payload = {
            "collection": [
                {
                    "doi": "10.1101/2024.01.01.573838",
                    "title": "T",
                    "authors": "Doe, J.",
                    "date": "2024-01-01",
                    "version": "1",
                    "server": None,
                }
            ]
        }
        _stub_json_responses(monkeypatch, payload)

        result = await biorxiv.get_paper("10.1101/2024.01.01.573838")

        assert "error" not in result
        assert result["title"] == "T"

    @pytest.mark.parametrize("payload", [None, 5, [1, 2, 3]])
    @pytest.mark.asyncio
    async def test_malformed_payload_is_not_negative_cached(self, monkeypatch, payload):
        good = {
            "collection": [
                {
                    "doi": "10.1101/2024.01.01.573838",
                    "title": "Real Paper",
                    "authors": "Doe, J.",
                    "date": "2024-01-01",
                    "version": "1",
                    "server": "biorxiv",
                }
            ]
        }
        _stub_json_responses(monkeypatch, payload, payload, good)

        first = await biorxiv.get_paper("10.1101/2024.01.01.573838")
        second = await biorxiv.get_paper("10.1101/2024.01.01.573838")

        assert "error" in first
        assert second.get("title") == "Real Paper", "a bad shape poisoned the cache"


# ---------------------------------------------------------------------------
# What counts as evidence of "not found"
# ---------------------------------------------------------------------------

_DOI = "10.1101/2024.01.01.573838"
_EMPTY: dict = {"collection": []}

# Valid JSON of the wrong *shape* — distinct from `_BAD_JSON`, which doesn't
# parse at all and so never reaches `_collection_of`. This is the body that
# makes `_collection_of` answer None: an anomalous 200 that says nothing about
# whether the DOI exists.
_WRONG_SHAPE: dict = {"messages": [{"status": "no posts found"}]}


def _negative_entry():
    return cache.get_negative(biorxiv.NAMESPACE, "papers", biorxiv.canonical_key(_DOI))


class TestNotFoundNeedsBothServers:
    """A definitive miss needs *both* servers to have answered well-formed.

    Nothing in the shared 10.1101/ prefix tells bioRxiv from medRxiv, so a
    "not found" is only established once each has returned a well-formed empty
    collection. If either returned garbage nothing was established, and
    negative-caching would file a live preprint as absent for the hour.
    """

    @pytest.fixture(autouse=True)
    def _no_gap(self, monkeypatch):
        _reset_biorxiv(monkeypatch)

    @pytest.mark.asyncio
    async def test_both_empty_is_a_definitive_miss(self, monkeypatch):
        _stub_json_responses(monkeypatch, _EMPTY, _EMPTY)

        result = await biorxiv.get_paper(_DOI)

        assert result["not_found"] is True
        assert "retryable" not in result
        assert _negative_entry() is not None

    @pytest.mark.asyncio
    async def test_the_negative_entry_carries_the_not_found_flag(self, monkeypatch):
        # It distinguishes a definitive miss from a transient failure for every
        # caller, as arxiv/openalex/wikipedia's do — and must survive the
        # negative-cache round-trip, not just the first return.
        _stub_json_responses(monkeypatch, _EMPTY, _EMPTY)
        await biorxiv.get_paper(_DOI)

        assert _negative_entry()["not_found"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("first", "second"),
        [(_EMPTY, _WRONG_SHAPE), (_WRONG_SHAPE, _EMPTY), (_WRONG_SHAPE, _WRONG_SHAPE)],
        ids=["medrxiv-wrong-shape", "biorxiv-wrong-shape", "both-wrong-shape"],
    )
    async def test_one_wrong_shape_reply_leaves_it_retryable(self, monkeypatch, first, second):
        calls = _stub_json_responses(monkeypatch, first, second)

        result = await biorxiv.get_paper(_DOI)

        assert result["retryable"] is True
        assert "not_found" not in result
        assert _negative_entry() is None, "an anomalous body must not be cached as absent"

        before = len(calls)
        await biorxiv.get_paper(_DOI)
        assert len(calls) > before, "nothing was cached; a retry re-fetches"

    @pytest.mark.asyncio
    async def test_a_wrong_shape_biorxiv_reply_still_lets_medrxiv_answer(self, monkeypatch):
        # The documented fallback: an anomalous first response is not fatal,
        # because medRxiv may still answer cleanly on the same call.
        med = _collection(title="A Medical Study", server="medRxiv")
        calls = _stub_json_responses(monkeypatch, _WRONG_SHAPE, med)

        result = await biorxiv.get_paper(_DOI)

        assert len(calls) == 2
        assert result["title"] == "A Medical Study"
        assert result["server"] == "medrxiv"

    @pytest.mark.asyncio
    async def test_a_biorxiv_hit_never_touches_medrxiv(self, monkeypatch):
        calls = _stub_json_responses(monkeypatch, _collection())

        await biorxiv.get_paper(_DOI)

        assert len(calls) == 1


class TestHttpErrors:
    """The `http.HTTPX_ERRORS` branch: transient, and never negative-cached.

    An unknown DOI is a 200 with an empty collection here, so a real HTTP
    status is a server problem rather than an answer about the paper.
    """

    @pytest.fixture(autouse=True)
    def _no_gap(self, monkeypatch):
        _reset_biorxiv(monkeypatch)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [404, 500, 503])
    async def test_a_status_error_is_retryable_and_uncached(self, monkeypatch, status):
        _stub_json_responses(monkeypatch, _Resp(status, {"collection": []}))

        result = await biorxiv.get_paper(_DOI)

        assert "error" in result
        assert result.get("not_found") is not True
        assert _negative_entry() is None

    @pytest.mark.asyncio
    async def test_a_transport_error_is_retryable_and_uncached(self, monkeypatch):
        _stub_json_responses(monkeypatch, httpx.ConnectError("no route"))

        result = await biorxiv.get_paper(_DOI)

        assert result["retryable"] is True
        assert _negative_entry() is None

    @pytest.mark.asyncio
    async def test_a_timeout_is_retryable_and_uncached(self, monkeypatch):
        _stub_json_responses(monkeypatch, httpx.ReadTimeout("slow"))

        result = await biorxiv.get_paper(_DOI)

        assert result["retryable"] is True
        assert _negative_entry() is None


class TestRequestPath:
    @pytest.fixture(autouse=True)
    def _no_gap(self, monkeypatch):
        _reset_biorxiv(monkeypatch)

    @pytest.mark.asyncio
    async def test_it_asks_both_servers_in_order(self, monkeypatch):
        calls = _stub_json_responses(monkeypatch, _EMPTY, _EMPTY)

        await biorxiv.get_paper(_DOI)

        assert [str(r.url) for r in calls] == [
            f"https://api.biorxiv.org/details/biorxiv/{_DOI}/na/json",
            f"https://api.biorxiv.org/details/medrxiv/{_DOI}/na/json",
        ]

    @pytest.mark.asyncio
    async def test_a_reserved_character_cannot_split_the_path(self, monkeypatch):
        # `doinorm.normalize` keeps a literal `#`/`?` in a bare DOI. Unquoted, the
        # first truncates the request at the fragment and the second turns the
        # rest of the path into a query string — either way we ask about a
        # different record than the caller named.
        calls = _stub_json_responses(monkeypatch, _EMPTY, _EMPTY)

        await biorxiv.get_paper("10.1101/2024.01.01.573838#fig2")

        url = str(calls[0].url)
        assert url.endswith("/details/biorxiv/10.1101/2024.01.01.573838%23fig2/na/json")
        assert calls[0].url.fragment == ""

    @pytest.mark.asyncio
    async def test_the_doi_s_own_slash_stays_literal(self, monkeypatch):
        calls = _stub_json_responses(monkeypatch, _collection())

        await biorxiv.get_paper(_DOI)

        assert "/10.1101/2024.01.01.573838/na/json" in str(calls[0].url)


class TestPathTraversalIsRefused:
    """The DOI is a middle path segment, so a `.`/`..` or empty one shortens
    `/details/{server}/{doi}/na/json` to the endpoint's interval/cursor form —
    a live route whose collection would cache as this DOI's paper."""

    @pytest.fixture(autouse=True)
    def _no_gap(self, monkeypatch):
        _reset_biorxiv(monkeypatch)

    @pytest.mark.parametrize(
        "doi",
        [
            "10.1101/..",
            "10.1101/x/..",
            "10.1101/2024.01.01.573838/../../na",
            "10.1101/.",
            "10.1101/",
            "",
            "   ",
            "doi:",
        ],
    )
    @pytest.mark.asyncio
    async def test_it_is_refused_before_any_request(self, monkeypatch, doi):
        # A live-looking collection, so a leaked request would cache a paper.
        calls = _stub_json_responses(monkeypatch, _collection(), _collection())

        result = await biorxiv.get_paper(doi)

        assert result["not_found"] is True
        assert calls == []

    @pytest.mark.parametrize("doi", ["10.1101/..", "10.1101/", ""])
    @pytest.mark.asyncio
    async def test_the_refusal_is_uncached(self, monkeypatch, doi):
        # No request was spent, so nothing was learned worth caching.
        _stub_json_responses(monkeypatch, _collection())
        canonical = biorxiv.canonical_key(doi)

        await biorxiv.get_paper(doi)

        assert cache.get(biorxiv.NAMESPACE, "papers", canonical) is None
        assert cache.get_negative(biorxiv.NAMESPACE, "papers", canonical) is None

    @pytest.mark.asyncio
    async def test_the_router_really_sends_these_here(self):
        # `is_biorxiv_doi` is a bare prefix test, so these really do route here.
        from academic_tools_mcp import manual

        assert biorxiv.is_biorxiv_doi("10.1101/x/..") is True
        assert manual.resolve_target("10.1101/x/..")["namespace"] == biorxiv.NAMESPACE


class TestGetPaperCaching:
    @pytest.fixture(autouse=True)
    def _no_gap(self, monkeypatch):
        _reset_biorxiv(monkeypatch)

    @pytest.mark.asyncio
    async def test_a_second_call_is_served_from_cache(self, monkeypatch):
        calls = _stub_json_responses(monkeypatch, _collection())

        first = await biorxiv.get_paper(_DOI)
        second = await biorxiv.get_paper(_DOI)

        assert len(calls) == 1
        assert first == second

    @pytest.mark.asyncio
    async def test_two_spellings_share_one_entry(self, monkeypatch):
        calls = _stub_json_responses(monkeypatch, _collection())

        await biorxiv.get_paper(_DOI)
        await biorxiv.get_paper(f"https://www.BIORXIV.org/content/{_DOI}v2.full.pdf")

        assert len(calls) == 1, "one paper, one cache entry, one fetch"

    @pytest.mark.asyncio
    async def test_force_refresh_drops_both_halves_and_refetches(self, monkeypatch):
        calls = _stub_json_responses(monkeypatch, _EMPTY, _EMPTY, _collection(title="Published"))

        await biorxiv.get_paper(_DOI)
        assert _negative_entry() is not None

        result = await biorxiv.get_paper(_DOI, force_refresh=True)

        assert len(calls) == 3
        assert result["title"] == "Published"
        assert _negative_entry() is None

    @pytest.mark.asyncio
    async def test_concurrent_callers_collapse_to_one_fetch(self, monkeypatch):
        import asyncio

        calls = _stub_json_responses(monkeypatch, _collection())

        results = await asyncio.gather(*(biorxiv.get_paper(_DOI) for _ in range(5)))

        assert len(calls) == 1
        assert all(r["title"] == "A Great Discovery" for r in results)

    @pytest.mark.asyncio
    async def test_each_caller_gets_its_own_copy(self, monkeypatch):
        _stub_json_responses(monkeypatch, _collection())

        first = await biorxiv.get_paper(_DOI)
        first["title"] = "mutated"

        assert (await biorxiv.get_paper(_DOI))["title"] == "A Great Discovery"


class TestPdfPath:
    def test_it_keys_on_the_canonical_doi(self):
        assert biorxiv.pdf_path(_DOI).name == "10.1101_2024.01.01.573838.pdf"

    def test_every_spelling_names_one_file(self):
        spellings = [
            _DOI,
            f"doi:{_DOI}",
            f"https://doi.org/{_DOI}",
            f"HTTPS://WWW.BIORXIV.ORG/content/{_DOI}v3.full.pdf",
        ]
        assert len({biorxiv.pdf_path(s) for s in spellings}) == 1

    def test_it_lands_in_the_provider_namespace(self):
        assert biorxiv.pdf_path(_DOI).parent.parent.name == biorxiv.NAMESPACE


class TestDownloadPdfMetadataBranches:
    @pytest.fixture(autouse=True)
    def _no_gap(self, monkeypatch):
        _reset_biorxiv(monkeypatch)

    @pytest.mark.asyncio
    async def test_a_metadata_error_is_returned_untouched(self, monkeypatch):
        _stub_json_responses(monkeypatch, _EMPTY, _EMPTY)

        result = await biorxiv.download_pdf(_DOI)

        assert result["error"] == f"No paper found for DOI: {_DOI}"
        assert not biorxiv.pdf_path(_DOI).exists()

    @pytest.mark.asyncio
    async def test_a_transient_metadata_error_is_not_cached_as_a_failed_download(self, monkeypatch):
        _stub_json_responses(monkeypatch, _BAD_JSON, _BAD_JSON)

        result = await biorxiv.download_pdf(_DOI)

        assert result["retryable"] is True
        canonical = biorxiv.canonical_key(_DOI)
        assert cache.get_negative(biorxiv.NAMESPACE, "downloads", canonical) is None

    @pytest.mark.asyncio
    async def test_an_entry_with_no_doi_still_builds_a_pdf_url(self, monkeypatch):
        # `_parse_paper` falls back to the requested DOI, so a record missing
        # its own `doi` doesn't yield `.../content/v1.full.pdf` — a URL that
        # names no paper.
        _stub_json_responses(monkeypatch, {"collection": [{"version": "2"}]})

        paper = await biorxiv.get_paper(_DOI)

        assert paper["doi"] == _DOI
        assert paper["pdf_url"] == f"https://www.biorxiv.org/content/{_DOI}v2.full.pdf"

    @pytest.mark.asyncio
    async def test_a_record_with_no_pdf_url_is_a_definitive_failure(self, monkeypatch):
        async def _no_url(doi, *, force_refresh=False):
            return {"doi": doi, "pdf_url": None}

        monkeypatch.setattr(biorxiv, "get_paper", _no_url)

        result = await biorxiv.download_pdf(_DOI)

        assert result["retryable"] is False
        canonical = biorxiv.canonical_key(_DOI)
        assert cache.get_negative(biorxiv.NAMESPACE, "downloads", canonical) is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("force", [False, True])
    async def test_force_refresh_reaches_the_metadata_lookup(self, monkeypatch, force):
        # A forced re-download must not build its URL from the stale version
        # number it was asked to replace.
        seen = {}

        async def _fake_get_paper(doi, *, force_refresh=False):
            seen["force_refresh"] = force_refresh
            return {"error": "stop here"}

        monkeypatch.setattr(biorxiv, "get_paper", _fake_get_paper)

        await biorxiv.download_pdf(_DOI, force_refresh=force)

        assert seen["force_refresh"] is force


class TestContentHostPacing:
    @pytest.mark.asyncio
    async def test_the_content_gap_is_waited_out_before_the_pdf_stream(self, monkeypatch):
        order: list[str] = []

        async def _fake_get_paper(doi, *, force_refresh=False):
            return {"doi": doi, "pdf_url": f"https://www.biorxiv.org/content/{doi}v1.full.pdf"}

        async def _fake_wait():
            order.append("gap")

        async def _fake_stream(*args, **kwargs):
            order.append("stream")
            return {"error": "stop here", "retryable": True}

        monkeypatch.setattr(biorxiv, "get_paper", _fake_get_paper)
        monkeypatch.setattr(biorxiv._content_gap, "wait", _fake_wait)
        monkeypatch.setattr(biorxiv.streaming, "stream_to_file", _fake_stream)

        await biorxiv.download_pdf(_DOI)

        assert order == ["gap", "stream"]

    @pytest.mark.asyncio
    async def test_a_refused_gap_is_an_error_dict_not_an_exception(self, monkeypatch):
        """A stacked caller gets the retryable error dict, not a raw backpressure raise."""

        async def _fake_get_paper(doi, *, force_refresh=False):
            return {"doi": doi, "pdf_url": f"https://www.biorxiv.org/content/{doi}v1.full.pdf"}

        async def _refuse():
            raise http.LocalBackpressureError(
                biorxiv.LABEL, pending=5, max_pending=5, min_gap_seconds=3.0
            )

        async def _fake_stream(*args, **kwargs):
            raise AssertionError("streamed despite a refused gap")

        monkeypatch.setattr(biorxiv, "get_paper", _fake_get_paper)
        monkeypatch.setattr(biorxiv._content_gap, "wait", _refuse)
        monkeypatch.setattr(biorxiv.streaming, "stream_to_file", _fake_stream)

        result = await biorxiv.download_pdf(_DOI)

        assert result["retryable"] is True
        assert biorxiv.LABEL in result["error"]
        # Retryable: a negative entry would serve the refusal back on retry.
        assert cache.get_negative(biorxiv.NAMESPACE, biorxiv._NEG_ENTITY, _DOI) is None

    def test_the_content_gap_is_stricter_than_the_api_gap(self):
        assert biorxiv._content_gap.min_gap_seconds > biorxiv._throttle.min_gap_seconds
        assert biorxiv._content_gap.throttle is biorxiv._throttle


def _stub_routes(monkeypatch, routes):
    """Serve JSON by URL-path prefix (``/details/``, ``/pubs/``); records every request."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        for prefix, payload in routes.items():
            if request.url.path.startswith(prefix):
                if isinstance(payload, _Resp):
                    return httpx.Response(payload.status_code, json=payload.payload)
                if payload is _BAD_JSON:
                    return httpx.Response(200, content=b"")
                return httpx.Response(200, json=payload)
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: client)
    return requests


_PUBS = {
    "messages": [{"status": "ok"}],
    "collection": [
        {
            "preprint_doi": "10.1101/2024.01.01.573838",
            "published_doi": "10.1038/s41586-024-00001-1",
            "published_journal": "Nature",
            "preprint_platform": "bioRxiv",
            "published_date": "2024-06-01",
        }
    ],
}

_NO_PUBS = {"messages": [{"status": "no articles found"}], "collection": []}


class TestParsePublication:
    def test_fields(self):
        assert biorxiv._parse_publication(_PUBS["collection"][0]) == {
            "published_doi": "10.1038/s41586-024-00001-1",
            "published_journal": "Nature",
            "published_date": "2024-06-01",
        }

    @pytest.mark.parametrize("value", [None, "", "   ", 7, ["Nature"]])
    def test_blank_or_non_string_fields_are_none(self, value):
        parsed = biorxiv._parse_publication({"published_journal": value})
        assert parsed["published_journal"] is None


class TestGetPublication:
    @pytest.fixture(autouse=True)
    def _no_gap(self, monkeypatch):
        _reset_biorxiv(monkeypatch)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("server", ["biorxiv", "medrxiv"])
    async def test_asks_the_named_server(self, monkeypatch, server):
        calls = _stub_routes(monkeypatch, {"/pubs/": _PUBS})

        result = await biorxiv.get_publication(_DOI, server)

        assert result["published_journal"] == "Nature"
        assert calls[0].url.path == f"/pubs/{server}/{_DOI}/na/json"

    @pytest.mark.asyncio
    async def test_an_unknown_server_label_asks_biorxiv(self, monkeypatch):
        calls = _stub_routes(monkeypatch, {"/pubs/": _PUBS})

        await biorxiv.get_publication(_DOI, "bioRxiv ")

        assert calls[0].url.path.startswith("/pubs/biorxiv/")

    @pytest.mark.asyncio
    async def test_is_cached(self, monkeypatch):
        calls = _stub_routes(monkeypatch, {"/pubs/": _PUBS})

        await biorxiv.get_publication(_DOI, "biorxiv")
        await biorxiv.get_publication(_DOI, "biorxiv")

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_an_empty_collection_is_a_negative_cached_miss(self, monkeypatch):
        calls = _stub_routes(monkeypatch, {"/pubs/": _NO_PUBS})

        result = await biorxiv.get_publication(_DOI, "biorxiv")
        await biorxiv.get_publication(_DOI, "biorxiv")

        assert result["not_found"] is True
        assert (
            cache.get_negative(biorxiv.NAMESPACE, "pubs", biorxiv.canonical_key(_DOI)) is not None
        )
        assert len(calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [{"collection": "nope"}, [], _BAD_JSON])
    async def test_a_wrong_shape_is_transient_and_uncached(self, monkeypatch, payload):
        _stub_routes(monkeypatch, {"/pubs/": payload})

        result = await biorxiv.get_publication(_DOI, "biorxiv")

        assert result["retryable"] is True
        canonical = biorxiv.canonical_key(_DOI)
        assert cache.get_negative(biorxiv.NAMESPACE, "pubs", canonical) is None
        assert cache.get(biorxiv.NAMESPACE, "pubs", canonical) is None

    @pytest.mark.asyncio
    async def test_a_5xx_is_retryable(self, monkeypatch):
        monkeypatch.setattr(biorxiv._throttle, "retry_attempts", 1)
        _stub_routes(monkeypatch, {"/pubs/": _Resp(503, {})})

        result = await biorxiv.get_publication(_DOI, "biorxiv")

        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_an_empty_path_segment_is_refused_without_a_request(self, monkeypatch):
        calls = _stub_routes(monkeypatch, {"/pubs/": _PUBS})

        result = await biorxiv.get_publication("10.1101/", "biorxiv")

        assert result["not_found"] is True
        assert calls == []


class TestGetPaperMergesPublication:
    @pytest.fixture(autouse=True)
    def _no_gap(self, monkeypatch):
        _reset_biorxiv(monkeypatch)

    @pytest.mark.asyncio
    async def test_a_published_record_gains_journal_and_date(self, monkeypatch):
        _stub_routes(
            monkeypatch,
            {"/details/": _collection(published="10.1038/s41586-024-00001-1"), "/pubs/": _PUBS},
        )

        paper = await biorxiv.get_paper(_DOI)

        assert paper["published_journal"] == "Nature"
        assert paper["published_date"] == "2024-06-01"

    @pytest.mark.asyncio
    async def test_an_unpublished_record_never_asks_pubs(self, monkeypatch):
        calls = _stub_routes(monkeypatch, {"/details/": _collection()})

        paper = await biorxiv.get_paper(_DOI)

        assert [c.url.path.split("/")[1] for c in calls] == ["details"]
        assert paper["published_journal"] is None
        assert paper["published_date"] is None

    @pytest.mark.asyncio
    async def test_pubs_asks_the_records_own_server(self, monkeypatch):
        calls = _stub_routes(
            monkeypatch,
            {
                "/details/": _collection(server="medRxiv", published="10.1038/x"),
                "/pubs/": _PUBS,
            },
        )

        await biorxiv.get_paper(_DOI)

        assert calls[-1].url.path.startswith("/pubs/medrxiv/")

    @pytest.mark.asyncio
    async def test_a_pubs_failure_leaves_nulls_and_is_not_frozen_into_the_record(self, monkeypatch):
        monkeypatch.setattr(biorxiv._throttle, "retry_attempts", 1)
        _stub_routes(
            monkeypatch,
            {"/details/": _collection(published="10.1038/x"), "/pubs/": _Resp(503, {})},
        )

        paper = await biorxiv.get_paper(_DOI)

        assert "error" not in paper
        assert paper["published_journal"] is None
        cached = cache.get(biorxiv.NAMESPACE, "papers", biorxiv.canonical_key(_DOI))
        assert "published_journal" not in cached

        _stub_routes(
            monkeypatch,
            {"/details/": _collection(published="10.1038/x"), "/pubs/": _PUBS},
        )
        assert (await biorxiv.get_paper(_DOI))["published_journal"] == "Nature"

    @pytest.mark.asyncio
    async def test_force_refresh_reaches_pubs(self, monkeypatch):
        calls = _stub_routes(
            monkeypatch,
            {"/details/": _collection(published="10.1038/x"), "/pubs/": _PUBS},
        )

        await biorxiv.get_paper(_DOI)
        await biorxiv.get_paper(_DOI, force_refresh=True)

        assert [c.url.path.split("/")[1] for c in calls] == ["details", "pubs", "details", "pubs"]

    @pytest.mark.asyncio
    async def test_a_details_error_skips_pubs(self, monkeypatch):
        calls = _stub_routes(monkeypatch, {"/details/": _EMPTY})

        paper = await biorxiv.get_paper(_DOI)

        assert paper["not_found"] is True
        assert all(c.url.path.startswith("/details/") for c in calls)


class TestParseFunding:
    def test_na_is_no_funders(self):
        assert biorxiv._parse_funding(_SAMPLE_RAW) == []

    def test_missing_is_no_funders(self):
        assert biorxiv._parse_funding({}) == []

    def test_a_list_under_the_funder_key(self):
        raw = {
            "funder": [
                {"name": "NIH", "id": "01cwqze88", "id-type": "ROR", "award": "R01 GM000000"},
                {"name": "Wellcome Trust", "id": "029chgv08", "id-type": "ROR", "award": "NA"},
            ]
        }
        assert biorxiv._parse_funding(raw) == [
            {"name": "NIH", "id": "01cwqze88", "id_type": "ROR", "award": "R01 GM000000"},
            {"name": "Wellcome Trust", "id": "029chgv08", "id_type": "ROR", "award": "NA"},
        ]

    def test_a_single_object_under_the_funding_key(self):
        raw = {"funding": {"name": "ERC", "id_type": "ROR"}}
        assert biorxiv._parse_funding(raw) == [
            {"name": "ERC", "id": None, "id_type": "ROR", "award": None}
        ]

    @pytest.mark.parametrize("value", ["NA", 3, None, ["NIH"], [{}], [{"name": "  "}]])
    def test_malformed_or_empty_entries_are_dropped(self, value):
        assert biorxiv._parse_funding({"funder": value}) == []

    def test_parse_paper_carries_funding(self):
        raw = {**_SAMPLE_RAW, "funder": [{"name": "NIH"}]}
        assert biorxiv._parse_paper(raw)["funding"] == [
            {"name": "NIH", "id": None, "id_type": None, "award": None}
        ]


class TestParseVersions:
    def test_oldest_first_with_per_version_license(self):
        collection = [
            {"version": "2", "date": "2024-03-04", "license": "cc_by"},
            {"version": "1", "date": "2024-01-02", "license": "cc_no"},
            {"version": "10", "date": "2024-09-09", "license": "cc_by"},
        ]
        assert biorxiv._parse_versions(collection) == [
            {"version": "1", "date": "2024-01-02", "license": "cc_no"},
            {"version": "2", "date": "2024-03-04", "license": "cc_by"},
            {"version": "10", "date": "2024-09-09", "license": "cc_by"},
        ]

    def test_junk_fields_are_null_not_errors(self):
        assert biorxiv._parse_versions([{"version": None, "date": 5}]) == [
            {"version": None, "date": None, "license": None}
        ]

    @pytest.mark.asyncio
    async def test_get_paper_records_every_version(self, monkeypatch):
        _reset_biorxiv(monkeypatch)
        body = {
            "collection": [
                {**_collection()["collection"][0], "version": "1", "date": "2024-01-02"},
                {**_collection()["collection"][0], "version": "2", "date": "2024-03-04"},
            ]
        }
        _stub_routes(monkeypatch, {"/details/": body})

        paper = await biorxiv.get_paper(_DOI)

        assert paper["version"] == "2"
        assert [v["version"] for v in paper["versions"]] == ["1", "2"]


_JATS_URL = "https://www.biorxiv.org/content/early/2024/01/02/2024.01.01.573838.source.xml"
_JATS_BODY = "<article><body><sec><title>Intro</title><p>Text.</p></sec></body></article>"


def _jats_setup(monkeypatch, *responses, jatsxml=_JATS_URL):
    """A record naming ``jatsxml`` and a content host answering ``(status, text)`` in turn."""
    _reset_biorxiv(monkeypatch)
    monkeypatch.setattr(biorxiv._content_gap, "min_gap_seconds", 0.0)
    papers_asked: list[bool] = []

    async def fake_get_paper(doi, *, force_refresh=False):
        papers_asked.append(force_refresh)
        return {"doi": doi, "jatsxml": jatsxml}

    monkeypatch.setattr(biorxiv, "get_paper", fake_get_paper)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status, text = responses[min(len(seen), len(responses) - 1)]
        seen.append(str(request.url))
        return httpx.Response(status, text=text)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: client)
    return seen, papers_asked


class TestGetJats:
    @pytest.mark.asyncio
    async def test_the_records_jatsxml_is_fetched(self, monkeypatch):
        seen, _ = _jats_setup(monkeypatch, (200, _JATS_BODY))

        result = await biorxiv.get_jats(_DOI)

        assert result == {"markup": _JATS_BODY}
        assert seen == [_JATS_URL]

    @pytest.mark.asyncio
    async def test_the_content_gap_is_waited_out_first(self, monkeypatch):
        _jats_setup(monkeypatch, (200, _JATS_BODY))
        waited = []

        async def fake_wait():
            waited.append(1)

        monkeypatch.setattr(biorxiv._content_gap, "wait", fake_wait)

        await biorxiv.get_jats(_DOI)

        assert waited == [1]

    @pytest.mark.asyncio
    async def test_a_refused_gap_is_an_error_dict_not_an_exception(self, monkeypatch):
        """A stacked caller gets bioRxiv's retryable error, not a raw backpressure raise."""
        seen, _ = _jats_setup(monkeypatch, (200, _JATS_BODY))

        async def refuse():
            raise http.LocalBackpressureError(
                biorxiv.LABEL, pending=5, max_pending=5, min_gap_seconds=3.0
            )

        monkeypatch.setattr(biorxiv._content_gap, "wait", refuse)

        result = await biorxiv.get_jats(_DOI)

        assert result["retryable"] is True
        assert biorxiv.LABEL in result["error"]
        assert seen == []
        # Retryable: nothing may be negative-cached, or the retry serves the refusal.
        assert cache.get_negative(biorxiv.NAMESPACE, "jats", biorxiv.canonical_key(_DOI)) is None

    @pytest.mark.asyncio
    async def test_a_404_is_negative_cached(self, monkeypatch):
        seen, _ = _jats_setup(monkeypatch, (404, "gone"))

        result = await biorxiv.get_jats(_DOI)
        again = await biorxiv.get_jats(_DOI)

        assert result == {"error": f"No JATS full text for DOI: {_DOI}", "not_found": True}
        assert again == result
        assert cache.get_negative(biorxiv.NAMESPACE, "jats", biorxiv.canonical_key(_DOI)) == result
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_force_refresh_asks_again_after_a_404_and_refreshes_the_record(self, monkeypatch):
        seen, papers_asked = _jats_setup(monkeypatch, (404, ""), (200, _JATS_BODY))

        await biorxiv.get_jats(_DOI)
        result = await biorxiv.get_jats(_DOI, force_refresh=True)

        assert result == {"markup": _JATS_BODY}
        assert len(seen) == 2
        assert papers_asked == [False, True]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "jatsxml",
        [
            None,
            "",
            "NA",
            "http://www.biorxiv.org/content/early/x.source.xml",
            "https://evil.example/content/x.source.xml",
            "https://www.biorxiv.org.evil.example/x.source.xml",
        ],
    )
    async def test_a_url_off_the_content_hosts_is_never_fetched(self, monkeypatch, jatsxml):
        seen, _ = _jats_setup(monkeypatch, (200, _JATS_BODY), jatsxml=jatsxml)

        result = await biorxiv.get_jats(_DOI)

        assert result["not_found"] is True
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_medrxiv_host_is_allowed(self, monkeypatch):
        url = "https://www.medrxiv.org/content/early/2020/09/10/2020.09.09.20191205.source.xml"
        seen, _ = _jats_setup(monkeypatch, (200, _JATS_BODY), jatsxml=url)

        assert await biorxiv.get_jats("10.1101/2020.09.09.20191205") == {"markup": _JATS_BODY}
        assert seen == [url]

    @pytest.mark.asyncio
    async def test_a_200_that_is_not_an_article_is_transient_and_uncached(self, monkeypatch):
        _jats_setup(monkeypatch, (200, "<html>Just a moment...</html>"))

        result = await biorxiv.get_jats(_DOI)

        assert result["retryable"] is True
        assert cache.get_negative(biorxiv.NAMESPACE, "jats", biorxiv.canonical_key(_DOI)) is None

    @pytest.mark.asyncio
    async def test_a_cloudflare_rate_limit_is_retryable(self, monkeypatch):
        monkeypatch.setattr(biorxiv._throttle, "retry_attempts", 1)
        _jats_setup(monkeypatch, (429, "error code: 1015"))

        result = await biorxiv.get_jats(_DOI)

        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_a_body_over_the_byte_cap_is_refused(self, monkeypatch):
        monkeypatch.setenv("MAX_PDF_BYTES", "10")
        _jats_setup(monkeypatch, (200, _JATS_BODY))

        result = await biorxiv.get_jats(_DOI)

        assert result["retryable"] is False
        assert result["max_bytes"] == 10

    @pytest.mark.asyncio
    async def test_a_metadata_error_is_returned_without_a_content_request(self, monkeypatch):
        seen, _ = _jats_setup(monkeypatch, (200, _JATS_BODY))

        async def failing(doi, *, force_refresh=False):
            return {
                "error": "bioRxiv returned a response that could not be parsed.",
                "retryable": True,
            }

        monkeypatch.setattr(biorxiv, "get_paper", failing)

        result = await biorxiv.get_jats(_DOI)

        assert result["retryable"] is True
        assert seen == []
