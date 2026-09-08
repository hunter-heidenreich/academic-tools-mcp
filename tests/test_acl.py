import asyncio
from urllib.parse import quote, urlsplit

import pytest

from academic_tools_mcp import _stems, manual, papers
from academic_tools_mcp.providers import acl
from academic_tools_mcp.util import doinorm

# ---------------------------------------------------------------------------
# DOI detection
# ---------------------------------------------------------------------------


class TestIsAclDoi:
    def test_acl_doi_bare(self):
        assert acl.is_acl_doi("10.18653/v1/2023.acl-long.1") is True

    def test_acl_doi_url(self):
        assert acl.is_acl_doi("https://doi.org/10.18653/v1/2023.acl-long.1") is True

    def test_acl_doi_prefixed(self):
        assert acl.is_acl_doi("doi:10.18653/v1/2023.acl-long.1") is True

    def test_non_acl_doi(self):
        assert acl.is_acl_doi("10.1038/s41586-021-03819-2") is False

    def test_arxiv_doi(self):
        assert acl.is_acl_doi("10.48550/arXiv.2301.00001") is False

    def test_uppercase_prefix(self):
        # DOIs are case-insensitive; an uppercased 'V1' prefix is still ACL.
        assert acl.is_acl_doi("10.18653/V1/2023.acl-long.1") is True

    def test_uppercase_prefix_url(self):
        assert acl.is_acl_doi("https://doi.org/10.18653/V1/P16-1160") is True


# ---------------------------------------------------------------------------
# DOI → Anthology ID
# ---------------------------------------------------------------------------


class TestDoiToAnthologyId:
    def test_bare_doi(self):
        assert acl.doi_to_anthology_id("10.18653/v1/2023.acl-long.1") == "2023.acl-long.1"

    def test_url_doi(self):
        assert (
            acl.doi_to_anthology_id("https://doi.org/10.18653/v1/2023.acl-long.1")
            == "2023.acl-long.1"
        )

    def test_prefixed_doi(self):
        assert acl.doi_to_anthology_id("doi:10.18653/v1/2023.acl-long.1") == "2023.acl-long.1"

    def test_emnlp(self):
        assert acl.doi_to_anthology_id("10.18653/v1/2022.emnlp-main.100") == "2022.emnlp-main.100"

    def test_naacl(self):
        assert acl.doi_to_anthology_id("10.18653/v1/2022.naacl-main.50") == "2022.naacl-main.50"

    def test_findings(self):
        assert acl.doi_to_anthology_id("10.18653/v1/2023.findings-acl.42") == "2023.findings-acl.42"

    def test_non_acl_returns_none(self):
        assert acl.doi_to_anthology_id("10.1038/s41586-021-03819-2") is None

    def test_whitespace_stripped(self):
        assert acl.doi_to_anthology_id("  10.18653/v1/2023.acl-long.1  ") == "2023.acl-long.1"

    def test_old_format_lowercased_uppercased(self):
        # Crossref hands old-format DOIs back lowercased; the CDN path is
        # case-sensitive, so the extracted ID must be uppercased.
        assert acl.doi_to_anthology_id("10.18653/v1/p16-1160") == "P16-1160"

    def test_old_format_already_uppercase_idempotent(self):
        assert acl.doi_to_anthology_id("10.18653/v1/P16-1160") == "P16-1160"

    def test_old_format_workshop_venue(self):
        assert acl.doi_to_anthology_id("10.18653/v1/w04-1013") == "W04-1013"

    def test_new_format_stays_lowercase(self):
        # New-format IDs carry lowercase venue letters that must be preserved.
        assert acl.doi_to_anthology_id("10.18653/v1/2023.acl-long.1") == "2023.acl-long.1"

    def test_uppercase_prefix_new_format(self):
        # DOIs are case-insensitive: an uppercased 'V1' prefix still resolves.
        assert acl.doi_to_anthology_id("10.18653/V1/2023.acl-long.1") == "2023.acl-long.1"

    def test_uppercase_prefix_old_format(self):
        # Case-insensitive prefix match AND old-format suffix uppercasing.
        assert acl.doi_to_anthology_id("10.18653/V1/p16-1160") == "P16-1160"


# ---------------------------------------------------------------------------
# PDF URL construction
# ---------------------------------------------------------------------------


class TestPdfUrl:
    def test_basic(self):
        assert acl.pdf_url("2023.acl-long.1") == "https://aclanthology.org/2023.acl-long.1.pdf"

    def test_emnlp(self):
        assert (
            acl.pdf_url("2022.emnlp-main.100") == "https://aclanthology.org/2022.emnlp-main.100.pdf"
        )

    def test_old_format_lowercased_doi_round_trip(self):
        # A Crossref-lowercased old-format DOI must produce the case-sensitive
        # URL the CDN expects (P16-1160.pdf, not p16-1160.pdf).
        aid = acl.doi_to_anthology_id("10.18653/v1/p16-1160")
        assert acl.pdf_url(aid) == "https://aclanthology.org/P16-1160.pdf"


# ---------------------------------------------------------------------------
# Anthology ID normalization
# ---------------------------------------------------------------------------


class TestNormalizeAnthologyId:
    def test_old_format_lowercased(self):
        assert acl._normalize_anthology_id("p16-1160") == "P16-1160"

    def test_old_format_already_uppercase(self):
        assert acl._normalize_anthology_id("P16-1160") == "P16-1160"

    def test_new_format_unchanged(self):
        assert acl._normalize_anthology_id("2023.acl-long.1") == "2023.acl-long.1"

    def test_new_format_mixed_case_left_as_is(self):
        # Not an old-format match, so it is returned verbatim (no spurious upper()).
        assert acl._normalize_anthology_id("2023.ACL-long.1") == "2023.ACL-long.1"


# ---------------------------------------------------------------------------
# PDF cache path
# ---------------------------------------------------------------------------


class TestPdfPath:
    """Every ACL artifact keys on the canonical DOI, as every other provider's does.

    The Anthology ID addresses the CDN and nothing on disk. Keying the PDF on it
    instead made ACL the one namespace whose PDF stem disagreed with its markdown
    and section-index stems, which is the identity ``cache_search`` inverts an ACL
    filename with.
    """

    def test_acl_doi(self):
        path = acl.pdf_path("10.18653/v1/2023.acl-long.1")
        assert path.parent.name == "pdfs"
        assert path.parent.parent.name == "acl_anthology"
        assert path.name == "10.18653_v1_2023.acl-long.1.pdf"

    def test_keys_on_the_canonical_doi_not_the_anthology_id(self):
        # Crossref hands old-format DOIs back lowercased and the CDN wants
        # P16-1160 — but that casing belongs to the URL, not the filename.
        path = acl.pdf_path("10.18653/v1/p16-1160")
        assert path.name == "10.18653_v1_p16-1160.pdf"
        assert path == _stems.pdf_path(acl.NAMESPACE, acl.canonical_key("10.18653/v1/p16-1160"))

    def test_case_variants_share_one_path(self):
        assert acl.pdf_path("10.18653/V1/P16-1160") == acl.pdf_path("10.18653/v1/p16-1160")

    def test_agrees_with_the_markdown_stem(self):
        # The two used to disagree, and cache_search's ACL stem inversion was
        # correct only by accident of walking markdown rather than pdfs.
        doi = "10.18653/v1/2023.acl-long.1"
        canonical = acl.canonical_key(doi)
        assert acl.pdf_path(doi).stem == papers.markdown_path(acl.NAMESPACE, canonical).stem

    def test_non_acl_doi_raises(self):
        # Must not return a sentinel path (e.g. /dev/null) whose .exists() is
        # True — that would let a non-PDF slip past the convert guard.
        with pytest.raises(ValueError):
            acl.pdf_path("10.1038/s41586-021-03819-2")

    def test_bare_prefix_raises(self):
        with pytest.raises(ValueError):
            acl.pdf_path("10.18653/v1/")


# ---------------------------------------------------------------------------
# PDF filename sanitization
# ---------------------------------------------------------------------------


class TestPdfFilename:
    def test_new_format(self):
        assert acl.pdf_path("10.18653/v1/2023.acl-long.1").name == "10.18653_v1_2023.acl-long.1.pdf"

    def test_old_format(self):
        assert acl.pdf_path("10.18653/v1/P16-1160").name == "10.18653_v1_p16-1160.pdf"

    def test_metacharacters_neutralized(self):
        # Defense-in-depth: shell/path metacharacters never reach the filename.
        # They are percent-encoded (injective) rather than collapsed to "_".
        assert acl.pdf_path("10.18653/v1/foo;bar").name == "10.18653_v1_foo%3Bbar.pdf"


# ---------------------------------------------------------------------------
# download_pdf
# ---------------------------------------------------------------------------


class TestDownloadPdfProvenance:
    """ACL decorates its response with ``anthology_id`` and ``pdf_url``.

    The cached and fresh branches were two hand-copied blocks, so nothing
    stopped them drifting apart — and both called ``dest.stat()`` outside any
    try, so a concurrent unlink between the usability check and the stat raised
    OSError straight out of ``download_pdf``, breaking the module's uniform
    ``{error}`` contract. Both now come from one ``extra_fields`` dict.
    """

    _DOI = "10.18653/v1/2023.acl-long.1"

    @pytest.mark.asyncio
    async def test_fresh_and_cached_payloads_agree(self, tmp_path, monkeypatch):
        from academic_tools_mcp import _pdf_download, cache

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)

        async def fake_stream(client, url, dest, **kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4 acl")
            return {"path": str(dest), "size_bytes": dest.stat().st_size, "cached": False}

        monkeypatch.setattr(_pdf_download, "stream_to_file", fake_stream)

        fresh = await acl.download_pdf(self._DOI)
        cached = await acl.download_pdf(self._DOI)

        assert fresh["cached"] is False
        assert cached["cached"] is True
        # Provenance is identical across both branches, by construction.
        for key in ("anthology_id", "pdf_url"):
            assert fresh[key] == cached[key]
        assert fresh["anthology_id"] == "2023.acl-long.1"
        assert fresh["pdf_url"] == "https://aclanthology.org/2023.acl-long.1.pdf"

    @pytest.mark.asyncio
    async def test_a_404_is_negative_cached(self, tmp_path, monkeypatch):
        """A missing camera-ready re-hit the CDN on every call: only
        oa_download negative-cached its download failures, the three native
        providers cached nothing."""
        from academic_tools_mcp import _pdf_download, cache

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)
        calls = 0

        async def fake_stream(client, url, dest, **kwargs):
            nonlocal calls
            calls += 1
            return {"error": "PDF not found", "retryable": False}

        monkeypatch.setattr(_pdf_download, "stream_to_file", fake_stream)

        assert "error" in await acl.download_pdf(self._DOI)
        assert "error" in await acl.download_pdf(self._DOI)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_an_error_carries_no_provenance(self, tmp_path, monkeypatch):
        from academic_tools_mcp import _pdf_download, cache

        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path)

        async def fake_stream(client, url, dest, **kwargs):
            return {"error": "PDF not found", "retryable": False}

        monkeypatch.setattr(_pdf_download, "stream_to_file", fake_stream)

        result = await acl.download_pdf(self._DOI)

        assert "anthology_id" not in result

    @pytest.mark.asyncio
    async def test_a_non_acl_doi_is_rejected_before_any_fetch(self):
        result = await acl.download_pdf("10.1038/nature12373")
        assert "error" in result
        assert "Not an ACL Anthology DOI" in result["error"]


# ---------------------------------------------------------------------------
# canonical_key
# ---------------------------------------------------------------------------


class TestCanonicalKey:
    """The key every ACL artifact and the negative-cache entry are filed under."""

    def test_delegates_to_the_shared_normalizer(self):
        # ACL layers no URL form of its own, so a second definition here would
        # be `doinorm.canonical` respelled — and free to drift from it.
        assert acl.canonical_key("10.18653/V1/P16-1160") == doinorm.canonical(
            "10.18653/V1/P16-1160"
        )

    def test_folds_case(self):
        assert acl.canonical_key("10.18653/V1/P16-1160") == "10.18653/v1/p16-1160"

    def test_case_variants_share_one_key(self):
        assert acl.canonical_key("10.18653/V1/P16-1160") == acl.canonical_key(
            "https://doi.org/10.18653/v1/p16-1160"
        )

    def test_idempotent(self):
        key = acl.canonical_key("doi:10.18653/V1/2023.acl-long.1")
        assert acl.canonical_key(key) == key

    def test_is_the_key_download_pdf_files_under(self):
        doi = "10.18653/v1/P16-1160"
        assert acl.pdf_path(doi).stem == _stems.safe_stem(acl.canonical_key(doi))


# ---------------------------------------------------------------------------
# Prefix stripping — the edges the venue examples don't reach
# ---------------------------------------------------------------------------


class TestStripAclPrefix:
    def test_empty_string(self):
        assert acl._strip_acl_prefix("") is None

    def test_prefix_without_trailing_slash(self):
        assert acl._strip_acl_prefix("10.18653/v1") is None

    def test_longer_registrant_sharing_the_prefix_slice(self):
        # The slice is fixed-length and includes the trailing "/", so a
        # registrant that merely starts with 10.18653 cannot match.
        assert acl._strip_acl_prefix("10.186530/v1/x") is None

    @pytest.mark.parametrize("doi", ["10.18653/v1/", "10.18653/V1/", "10.18653/v1/   "])
    def test_empty_suffix_is_not_an_acl_doi(self, doi):
        # It names no paper, and `safe_stem("")` is "" — so every such DOI
        # would cache as the same `.pdf`.
        assert acl._strip_acl_prefix(doi.strip()) is None
        assert acl.is_acl_doi(doi) is False
        assert acl.doi_to_anthology_id(doi) is None

    def test_bare_prefix_falls_through_to_the_generic_doi_route(self):
        target = manual.resolve_target("10.18653/v1/")
        assert target["namespace"] == manual.NAMESPACE
        assert manual.resolve_metadata_source("10.18653/v1/") == "openalex"

    def test_one_character_suffix_is_accepted(self):
        # The boundary: empty is rejected, one character is a (short) id.
        assert acl._strip_acl_prefix("10.18653/v1/x") == "x"


# ---------------------------------------------------------------------------
# Old-format ID grammar — the boundaries around `^[A-Za-z]\d{2}-\d+$`
# ---------------------------------------------------------------------------


class TestOldFormatBoundaries:
    """One letter, exactly two year digits, at least one paper digit.

    Anything outside that is left verbatim: uppercasing a new-format id would
    404 the CDN just as surely as leaving an old-format one lowercased.
    """

    @pytest.mark.parametrize("aid", ["p16-1160", "w04-1013", "d14-1162", "j93-2004", "l16-1"])
    def test_matches_are_uppercased(self, aid):
        assert acl._normalize_anthology_id(aid) == aid.upper()

    @pytest.mark.parametrize(
        "aid",
        [
            "pp16-1160",  # two leading letters
            "p1-1160",  # one year digit
            "p166-1160",  # three year digits
            "p16-1160a",  # trailing non-digit
            "p16-",  # no paper number
            "16-1160",  # no letter
            "2023.acl-long.1",  # new format
        ],
    )
    def test_non_matches_are_verbatim(self, aid):
        assert acl._normalize_anthology_id(aid) == aid


# ---------------------------------------------------------------------------
# PDF URL encoding
# ---------------------------------------------------------------------------


class TestPdfUrlEncoding:
    """The id is percent-encoded, so the URL names exactly the resource it claims.

    A DOI suffix reaches here untouched — `doinorm.normalize` keeps a literal `?`
    or `#` in a bare DOI deliberately — so an unencoded interpolation would
    request one resource while the response reported another as `pdf_url`.
    """

    @pytest.mark.parametrize("aid", ["2023.acl-long.1", "P16-1160"])
    def test_real_ids_are_unchanged(self, aid):
        # Every character of a real Anthology ID is RFC 3986 unreserved, so no
        # cached URL moves.
        assert acl.pdf_url(aid) == f"https://aclanthology.org/{aid}.pdf"

    @pytest.mark.parametrize("aid", ["x?y", "x#y", "x y", "a/b"])
    def test_url_metacharacters_stay_in_one_path_segment(self, aid):
        parts = urlsplit(acl.pdf_url(aid))
        assert parts.query == ""
        assert parts.fragment == ""
        assert parts.path == "/" + quote(aid, safe="") + ".pdf"
        assert parts.path.count("/") == 1


class TestDownloadPdfNegativeCache:
    """What a failure is worth remembering, and for how long.

    ``_NEG_TTL_SECONDS`` is 24h here rather than the preprint servers' 1h: an
    Anthology camera-ready is a static file, so a 404 means a wrong ID or a
    paper not yet posted — neither resolves in minutes.
    """

    _DOI = "10.18653/v1/2023.acl-long.1"

    @staticmethod
    def _counting_stream(monkeypatch, result):
        from academic_tools_mcp import _pdf_download

        calls = []

        async def fake_stream(client, url, dest, **kwargs):
            calls.append(url)
            if "error" not in result:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"%PDF-1.4 acl")
            return dict(result)

        monkeypatch.setattr(_pdf_download, "stream_to_file", fake_stream)
        return calls

    @pytest.mark.asyncio
    async def test_a_retryable_failure_is_not_negative_cached(self, monkeypatch):
        # `is_definitive_failure` is an allowlist, so a transport blip must
        # leave the paper refetchable rather than stranded for 24h.
        calls = self._counting_stream(
            monkeypatch, {"error": "ACL Anthology: network error", "retryable": True}
        )

        assert "error" in await acl.download_pdf(self._DOI)
        assert "error" in await acl.download_pdf(self._DOI)
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_an_unclassified_failure_is_not_negative_cached(self, monkeypatch):
        # A 403 paywall arrives with no `retryable` key at all; a denylist
        # would cache it as a fact about the paper.
        calls = self._counting_stream(monkeypatch, {"error": "ACL Anthology: HTTP 403"})

        await acl.download_pdf(self._DOI)
        await acl.download_pdf(self._DOI)
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_force_refresh_drops_the_negative_entry(self, monkeypatch):
        calls = self._counting_stream(monkeypatch, {"error": "PDF not found", "retryable": False})

        await acl.download_pdf(self._DOI)
        await acl.download_pdf(self._DOI)
        assert len(calls) == 1

        result = await acl.download_pdf(self._DOI, force_refresh=True)
        assert len(calls) == 2
        assert "error" in result

    @pytest.mark.asyncio
    async def test_the_negative_entry_is_keyed_on_the_canonical_doi(self, monkeypatch):
        from academic_tools_mcp import cache

        self._counting_stream(monkeypatch, {"error": "PDF not found", "retryable": False})

        await acl.download_pdf("10.18653/V1/2023.acl-long.1")

        assert (
            cache.get_negative(acl.NAMESPACE, "downloads", acl.canonical_key(self._DOI)) is not None
        )


class TestDownloadPdfSingleFlight:
    """Concurrent callers for one paper share one fetch.

    ACL omits ``sf_key`` — it is PDF-only, so there is no re-entrant getter on
    this ``SingleFlight`` to collide with the bare canonical key.
    """

    _DOI = "10.18653/v1/2023.acl-long.1"

    @pytest.mark.asyncio
    async def test_concurrent_downloads_collapse_to_one_stream(self, monkeypatch):
        from academic_tools_mcp import _pdf_download

        started = 0
        release = asyncio.Event()

        async def fake_stream(client, url, dest, **kwargs):
            nonlocal started
            started += 1
            await release.wait()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4 acl")
            return {"path": str(dest), "size_bytes": 12, "cached": False}

        monkeypatch.setattr(_pdf_download, "stream_to_file", fake_stream)

        tasks = [asyncio.create_task(acl.download_pdf(self._DOI)) for _ in range(5)]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*tasks)

        assert started == 1
        assert all(r["anthology_id"] == "2023.acl-long.1" for r in results)

    @pytest.mark.asyncio
    async def test_followers_get_independent_copies(self, monkeypatch):
        from academic_tools_mcp import _pdf_download

        release = asyncio.Event()

        async def fake_stream(client, url, dest, **kwargs):
            await release.wait()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"%PDF-1.4 acl")
            return {"path": str(dest), "size_bytes": 12, "cached": False}

        monkeypatch.setattr(_pdf_download, "stream_to_file", fake_stream)

        tasks = [asyncio.create_task(acl.download_pdf(self._DOI)) for _ in range(3)]
        await asyncio.sleep(0)
        release.set()
        first, *rest = await asyncio.gather(*tasks)

        # `tools/pipeline` writes `cascaded_invalidated` into what it receives.
        first["cascaded_invalidated"] = ["markdown"]
        assert all("cascaded_invalidated" not in r for r in rest)


# ---------------------------------------------------------------------------
# Startup migration
# ---------------------------------------------------------------------------


class TestMigrateLegacyPdfStems:
    """Re-files PDFs named after the Anthology ID under the canonical key.

    Only ``pdfs/`` ever diverged; markdown and the section index have always
    keyed on ``canonical_key``.
    """

    @staticmethod
    def _pdf_dir():
        from academic_tools_mcp import cache

        d = cache.cache_dir(acl.NAMESPACE, "pdfs")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_renames_a_legacy_stem_to_the_canonical_one(self):
        d = self._pdf_dir()
        (d / "P16-1160.pdf").write_bytes(b"%PDF-1.4 old")

        assert acl.migrate_legacy_pdf_stems() == 1
        assert not (d / "P16-1160.pdf").exists()
        assert (d / "10.18653_v1_p16-1160.pdf").read_bytes() == b"%PDF-1.4 old"

    def test_the_migrated_file_is_where_pdf_path_looks(self):
        d = self._pdf_dir()
        (d / "2023.acl-long.1.pdf").write_bytes(b"%PDF-1.4 old")

        acl.migrate_legacy_pdf_stems()

        assert acl.pdf_path("10.18653/v1/2023.acl-long.1").exists()

    def test_idempotent(self):
        d = self._pdf_dir()
        (d / "P16-1160.pdf").write_bytes(b"%PDF-1.4 old")

        assert acl.migrate_legacy_pdf_stems() == 1
        assert acl.migrate_legacy_pdf_stems() == 0
        assert (d / "10.18653_v1_p16-1160.pdf").exists()

    def test_a_percent_encoded_legacy_stem_round_trips(self):
        d = self._pdf_dir()
        (d / "foo%3Bbar.pdf").write_bytes(b"%PDF-1.4 old")

        acl.migrate_legacy_pdf_stems()

        assert (d / "10.18653_v1_foo%3Bbar.pdf").exists()

    def test_leaves_in_flight_tmp_files_alone(self):
        # An `atomic._new_temp` name still carries its destination's stem;
        # renaming it breaks the writer's `os.replace`.
        d = self._pdf_dir()
        tmp = d / "P16-1160.pdf.abc123.tmp"
        tmp.write_bytes(b"partial")

        assert acl.migrate_legacy_pdf_stems() == 0
        assert tmp.exists()

    def test_a_collision_leaves_both_files(self):
        d = self._pdf_dir()
        (d / "P16-1160.pdf").write_bytes(b"%PDF-1.4 legacy")
        (d / "10.18653_v1_p16-1160.pdf").write_bytes(b"%PDF-1.4 current")

        assert acl.migrate_legacy_pdf_stems() == 0
        assert (d / "P16-1160.pdf").exists()
        assert (d / "10.18653_v1_p16-1160.pdf").read_bytes() == b"%PDF-1.4 current"

    def test_a_missing_directory_is_not_an_error(self):
        # It runs inside the startup lifespan; nothing here may raise.
        assert acl.migrate_legacy_pdf_stems() == 0

    def test_an_unreadable_directory_is_skipped(self):
        d = self._pdf_dir()
        (d / "P16-1160.pdf").write_bytes(b"%PDF-1.4 old")
        d.chmod(0o000)
        try:
            assert acl.migrate_legacy_pdf_stems() == 0
        finally:
            d.chmod(0o755)
