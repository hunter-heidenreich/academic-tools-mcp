"""Property-based tests for the shared DOI normalization.

The invariant these pin is the reason `_doi` is single-homed: every spelling
of one DOI that a publisher, reference manager or agent might emit has to
collapse to the *same* cache key. Examples cover the spellings someone thought
of; hypothesis covers the suffixes nobody did.
"""

from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp import _doi, manual

# Registrant codes are `10.` followed by four or more digits; the suffix is
# near-freeform but may not contain whitespace. `?`/`#` are excluded because
# the bare and URL spellings *deliberately* diverge on them — a bare DOI keeps
# them, a URL cuts at them. That documented asymmetry is pinned by example in
# `test_doi.py`; folding it in here would only weaken this strategy.
# The `.filter` is not redundant with the categories: `str.strip()` and `\S`
# both go by `str.isspace()`, which is True for U+2028 (category Zl) and U+2029
# (Zp) as well as Zs. A suffix carrying one is stripped away by `normalize` and
# is not a DOI — a hole the category list alone leaves open.
_SUFFIX_ALPHABET = st.characters(
    min_codepoint=33,
    max_codepoint=0x2FFF,
    blacklist_characters="?#",
    blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"),
).filter(lambda c: not c.isspace())

dois = st.builds(
    lambda registrant, suffix: f"10.{registrant}/{suffix}",
    st.integers(min_value=1000, max_value=99999999).map(str),
    st.text(alphabet=_SUFFIX_ALPHABET, min_size=1, max_size=40),
)


@given(dois)
def test_every_accepted_spelling_yields_one_key(doi: str) -> None:
    """All the input forms `normalize` documents collapse to one canonical key."""
    key = _doi.canonical(doi)
    for spelling in (
        f"  {doi}  ",
        f"doi:{doi}",
        f"DOI:{doi}",
        f"doi: {doi}",
        f"https://doi.org/{doi}",
        f"http://doi.org/{doi}",
        f"https://dx.doi.org/{doi}",
        f"https://www.doi.org/{doi}",
        f"HTTPS://DX.DOI.ORG/{doi}",
        f"https://doi.org/{doi}?utm_source=x",
        f"https://doi.org/{doi}#abstract",
        # The prefix must be stripped *before* the URL handling, or this
        # nested form (which occurs in the wild) keys separately.
        f"doi:https://doi.org/{doi}",
        f"doi: https://dx.doi.org/{doi}",
    ):
        assert _doi.canonical(spelling) == key, spelling


@given(dois)
def test_canonical_is_idempotent_and_lowercase(doi: str) -> None:
    """A key fed back through `canonical` is unchanged — cache keys are stable."""
    key = _doi.canonical(doi)
    assert key == key.lower()
    assert _doi.canonical(key) == key


@given(dois)
def test_doi_shape_survives_every_spelling(doi: str) -> None:
    """`looks_like_doi` agrees across spellings, so dispatch can't disagree with caching."""
    assert _doi.looks_like_doi(doi)
    assert _doi.looks_like_doi(f"https://doi.org/{doi}")
    assert _doi.looks_like_doi(f"doi: {doi}")


# `dois` may generate any registrant, including the ones a provider owns:
# `10.1101/x` is a bioRxiv DOI and routes there, not to OpenAlex. Excluded from
# the dispatch property, whose subject is the *shape* test, not the roster.
_PROVIDER_REGISTRANTS = ("10.1101/", "10.18653/")
generic_dois = dois.filter(lambda d: not d.startswith(_PROVIDER_REGISTRANTS))


@given(generic_dois)
def test_dispatch_uses_the_same_shape_test_as_the_cache_key(doi: str) -> None:
    """`resolve_metadata_source` routes on `looks_like_doi`, not a forked regex.

    A second copy of the DOI pattern lets dispatch and caching disagree about
    what a DOI is — one spelling routed to OpenAlex, another rejected outright.
    """
    for spelling in (doi, f"https://doi.org/{doi}", f"doi: {doi}", f"  {doi}  "):
        assert manual.resolve_metadata_source(spelling) == "openalex", spelling


@given(st.text(max_size=60))
def test_normalize_is_idempotent_for_any_input(text: str) -> None:
    """Feeding `normalize` its own output changes nothing — for DOIs and non-DOIs alike.

    Without this, a form that survives one pass but not two (a repeated `doi:`
    prefix) keys separately from its own normalized output.
    """
    once = _doi.normalize(text)
    assert _doi.normalize(once) == once


@given(dois)
def test_canonical_matches_normalize_lowercased(doi: str) -> None:
    """`canonical` is exactly `normalize` + `lower` — no second normalization policy."""
    for spelling in (doi, f"https://doi.org/{doi}", f"doi:{doi}", f"  {doi}  "):
        assert _doi.canonical(spelling) == _doi.normalize(spelling).lower()
