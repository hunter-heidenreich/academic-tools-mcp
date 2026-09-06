"""Property-based tests for the gated open-access download path.

Three invariants that examples had been standing in for. The first is the
`_doi` collapse property applied to this path's *artifact*: every spelling of
one DOI must land on one PDF and one negative-cache verdict, or the same paper
downloads twice and a "no OA copy" verdict is recorded against a key nobody
looks up. The second and third pin the trust boundary — `best_pdf_url` may only
return a URL OpenAlex actually supplied, and nothing reaches disk that does not
begin `%PDF-`.
"""

from __future__ import annotations

import asyncio

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import manual, oa_download
from academic_tools_mcp.providers import openalex

from ._download_fakes import install_stream, mock_stream_response, passthrough_slot
from .test_doi_properties import generic_dois

_SPELLINGS = ("{doi}", "  {doi}  ", "doi:{doi}", "DOI:{doi}", "https://doi.org/{doi}")

# A URL slot is one of: absent, explicitly null (OpenAlex's closed-access
# shape), or a URL. All three occur in real payloads.
_url_slots = st.one_of(st.none(), st.sampled_from(["https://a.example/1.pdf", "https://b/2.pdf"]))


@given(generic_dois)
def test_every_spelling_shares_one_artifact_and_one_verdict(doi: str) -> None:
    """One DOI, one OA PDF and one negative-cache key — however it was typed."""
    target = manual.resolve_target(doi)
    for spelling in _SPELLINGS:
        other = manual.resolve_target(spelling.format(doi=doi))
        assert other["pdf_path"] == target["pdf_path"], spelling
        assert other["canonical"] == target["canonical"], spelling


@given(_url_slots, _url_slots, _url_slots)
def test_best_pdf_url_never_invents_a_url(best: str | None, primary: str | None, oa: str | None):
    """The resolver only ever returns a URL the work itself carried.

    This is the trust boundary stated as a property: `oa_download` fetches
    whatever this returns, so a synthesised or defaulted URL would be an
    arbitrary fetch by another name.
    """
    work = {
        "best_oa_location": {"pdf_url": best},
        "primary_location": {"pdf_url": primary},
        "open_access": {"oa_url": oa},
    }
    result = openalex.best_pdf_url(work)

    expected = best or primary or oa or None
    assert result == expected
    assert result is None or result in {best, primary, oa}


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=25)
@given(
    body=st.binary(min_size=1, max_size=64).filter(lambda b: not b.startswith(b"%PDF-")),
    content_type=st.sampled_from(["application/pdf", "application/octet-stream", ""]),
)
def test_a_non_pdf_body_never_reaches_disk(monkeypatch, body: bytes, content_type: str) -> None:
    """`require_pdf` sniffs the magic bytes before the first byte is written, so
    no publisher interstitial is ever cached as a paper — whatever it claims to
    be in its Content-Type."""
    doi = "10.1234/prop"

    async def fake_get_work(_doi, **_kw):
        return {"best_oa_location": {"pdf_url": "https://pub.example/p.pdf"}}

    monkeypatch.setattr(openalex, "get_work", fake_get_work)
    monkeypatch.setattr(oa_download, "_request_slot", passthrough_slot)
    install_stream(monkeypatch, mock_stream_response(chunks=[body], content_type=content_type))

    result = asyncio.run(oa_download.download_pdf(doi, force_refresh=True))

    dest = manual.resolve_target(doi)["pdf_path"]
    assert "error" in result
    assert result["retryable"] is False
    assert not dest.exists()
    assert not list(dest.parent.glob("*.tmp"))
