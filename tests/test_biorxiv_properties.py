"""Property-based tests for the bioRxiv/medRxiv identifier layer.

Four invariants, each with a concrete failure the module has to be safe from:

* one paper, one cache key — `doinorm` is single-homed for this reason, and
  biorxiv layers two rules of its own on top (a content URL, and the version
  suffix), so the rule has to be restated here;
* a spelling the shape test misses is not merely unfetchable — `manual` files
  it under its own namespace with a key that is already biorxiv's, and the same
  paper then caches, downloads and converts twice;
* a version suffix names no distinct record. bioRxiv mints one DOI for all
  versions and the details API is only asked for the whole set, so a suffixed
  key is one upstream rejects ("DOI not recognizable") — reported as a
  well-formed empty collection, i.e. negative-cached as a definitive miss;
* the version rule is bioRxiv's alone. `_normalize_doi` sees every other DOI in
  the repo through `is_biorxiv_doi`, and must not rewrite any of them.
"""

from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp import cache_search, manual
from academic_tools_mcp.providers import biorxiv
from academic_tools_mcp.store import stems
from academic_tools_mcp.util import doinorm

from .test_doi_properties import dois

# bioRxiv's own suffix grammar: a submission date and a number, plus the legacy
# bare-number form. Freeform `dois` suffixes are deliberately *not* used here —
# one ending in `v3` or `.full` is a rendering tail by this module's rule, so it
# is not a distinct identifier and can't stand in for one.
biorxiv_suffixes = st.one_of(
    st.builds(
        lambda y, m, d, n: f"{y}.{m:02d}.{d:02d}.{n}",
        st.integers(min_value=2013, max_value=2035),
        st.integers(min_value=1, max_value=12),
        st.integers(min_value=1, max_value=28),
        st.integers(min_value=1, max_value=99999999).map(str),
    ),
    st.integers(min_value=1, max_value=999999999).map(str),
)
biorxiv_dois = biorxiv_suffixes.map(lambda s: biorxiv.DOI_PREFIX + s)

# Every rendering of one paper an agent, a reference manager or a pasted
# citation might carry. `.full-text`, `.abstract` and `.supplementary-material`
# are real bioRxiv page tails, not invented ones.
_HOSTS = ["biorxiv.org", "www.biorxiv.org", "medrxiv.org", "www.medrxiv.org"]
_SCHEMES = ["https://", "http://", ""]
_TAILS = ["", "/", ".full", ".full.pdf", ".abstract", ".full-text", ".supplementary-material"]


@st.composite
def spellings(draw: st.DrawFn) -> tuple[str, str]:
    """A (spelling, bare DOI) pair — the spelling names the same paper."""
    doi = draw(biorxiv_dois)
    form = draw(st.sampled_from(["bare", "prefixed", "doi_url", "content_url"]))
    version = draw(st.integers(min_value=1, max_value=99))

    if form == "bare":
        return draw(st.sampled_from([doi, f"{doi}v{version}"])), doi
    if form == "prefixed":
        return draw(st.sampled_from([f"doi:{doi}", f"DOI:{doi}", f"Doi: {doi}"])), doi
    if form == "doi_url":
        host = draw(st.sampled_from(["doi.org", "dx.doi.org", "www.doi.org"]))
        return f"{draw(st.sampled_from(['https://', 'http://']))}{host}/{doi}", doi

    url = f"{draw(st.sampled_from(_SCHEMES))}{draw(st.sampled_from(_HOSTS))}/content/{doi}"
    url += f"v{version}" if draw(st.booleans()) else ""
    return url + draw(st.sampled_from(_TAILS)), doi


@given(spellings())
def test_every_spelling_of_one_paper_yields_one_key(pair: tuple[str, str]) -> None:
    spelling, doi = pair
    assert biorxiv.canonical_key(spelling) == doi.lower()


@given(spellings())
def test_every_spelling_routes_to_biorxiv(pair: tuple[str, str]) -> None:
    """The shape test and the router agree, so no caller re-derives the shape."""
    spelling, doi = pair
    assert biorxiv.is_biorxiv_doi(spelling) is True
    target = manual.resolve_target(spelling)
    assert target["namespace"] == biorxiv.NAMESPACE
    assert target["canonical"] == doi.lower()
    assert manual.resolve_metadata_source(spelling) == "biorxiv"


@given(spellings())
def test_canonical_key_is_normalize_lowercased(pair: tuple[str, str]) -> None:
    """One policy, not two: `canonical_key` folds case and does nothing else."""
    spelling, _ = pair
    assert biorxiv.canonical_key(spelling) == biorxiv._normalize_doi(spelling).lower()


@given(st.one_of(spellings().map(lambda pair: pair[0]), dois, st.text(max_size=60)))
def test_normalization_is_idempotent(identifier: str) -> None:
    """A second pass is a no-op, for any input at all.

    `manual.resolve_target` composes the two — it calls `canonical_key` on
    `doinorm.normalize`'s output — so a spelling that moves twice keys separately
    from its own output.
    """
    once = biorxiv._normalize_doi(identifier)
    assert biorxiv._normalize_doi(once) == once
    key = biorxiv.canonical_key(identifier)
    assert biorxiv.canonical_key(key) == key


@given(dois)
def test_a_foreign_doi_is_never_rewritten(doi: str) -> None:
    """The version rule is gated on the prefix.

    Ungated, it would trim a `v2` or `.full` tail off an unrelated registrant's
    DOI — and `_normalize_doi` sees every DOI in the repo, because
    `manual.resolve_target` asks `is_biorxiv_doi` about all of them.
    """
    if doi.startswith(biorxiv.DOI_PREFIX):
        return
    assert biorxiv._normalize_doi(doi) == doinorm.normalize(doi)
    assert biorxiv.is_biorxiv_doi(doi) is False


@given(biorxiv_dois)
def test_every_artifact_keys_on_the_canonical_doi(doi: str) -> None:
    """PDF, markdown and section index share one stem.

    It is what `tools/pipeline`'s force_refresh cascade relies on to reach all
    three, and what `cache_search` inverts.
    """
    canonical = biorxiv.canonical_key(doi)
    stem = stems.safe_stem(canonical)
    assert biorxiv.pdf_path(doi).stem == stem
    assert stems.markdown_path(biorxiv.NAMESPACE, canonical).stem == stem
    assert stems.sections_key(canonical) == stem


@given(biorxiv_dois)
def test_a_stored_stem_inverts_to_an_identifier_the_router_accepts(doi: str) -> None:
    """`search_cached_papers` reports a `canonical_id` an agent can chain with.

    This is the only consumer of the exported `DOI_PREFIX`: a second spelling
    of it would let the router and the stem inverter disagree.
    """
    canonical = biorxiv.canonical_key(doi)
    stem = biorxiv.pdf_path(doi).stem
    recovered = cache_search._filename_to_canonical(biorxiv.NAMESPACE, stem)
    assert recovered == canonical
    target = manual.resolve_target(recovered)
    assert target["namespace"] == biorxiv.NAMESPACE
    assert target["canonical"] == canonical
