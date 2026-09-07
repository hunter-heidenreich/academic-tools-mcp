import json
from typing import Any, NamedTuple

import httpx
import pytest

from academic_tools_mcp import _clients, cache
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
# longer per-provider code — they live in ``_throttle.Throttle`` and are
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
    ``raise_for_status`` are real, so ``_http.HTTPX_ERRORS`` is reachable and
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
    monkeypatch.setattr(_clients, "get_client", lambda *a, **kw: client)
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
    """The `_http.HTTPX_ERRORS` branch: transient, and never negative-cached.

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
        # `_doi.normalize` keeps a literal `#`/`?` in a bare DOI. Unquoted, the
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
