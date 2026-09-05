"""Property-based tests for the shared DOI normalization.

The invariant these pin is the reason `_doi` is single-homed: every spelling
of one DOI that a publisher, reference manager or agent might emit has to
collapse to the *same* cache key. Examples cover the spellings someone thought
of; hypothesis covers the suffixes nobody did.
"""

from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp import _doi

# Registrant codes are `10.` followed by four or more digits; the suffix is
# near-freeform but may not contain whitespace, and `?`/`#` would be read as
# the start of a query string or fragment by the URL form.
_SUFFIX_ALPHABET = st.characters(
    min_codepoint=33,
    max_codepoint=0x2FFF,
    blacklist_characters="?# \t\n\r",
    blacklist_categories=("Cs", "Cc", "Zs"),
)

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
        f"HTTPS://DX.DOI.ORG/{doi}",
        f"https://doi.org/{doi}?utm_source=x",
        f"https://doi.org/{doi}#abstract",
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
