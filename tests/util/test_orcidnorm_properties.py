"""Property tests for ``util/orcidnorm.py``.

The invariant stronger than any example: every spelling of one ORCID collapses to
one cache key, and ``canonical`` is idempotent.
"""

from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp.util import orcidnorm

orcids = st.builds(
    lambda a, b, c, d, check: f"{a:04d}-{b:04d}-{c:04d}-{d:03d}{check}",
    *(st.integers(min_value=0, max_value=9999) for _ in range(3)),
    st.integers(min_value=0, max_value=999),
    st.sampled_from("0123456789Xx"),
)


@given(orcids)
def test_every_accepted_spelling_yields_one_key(orcid: str) -> None:
    key = orcidnorm.canonical(orcid)
    for spelling in (
        f"  {orcid}  ",
        f"orcid:{orcid}",
        f"ORCID:{orcid}",
        f"https://orcid.org/{orcid}",
        f"http://orcid.org/{orcid}",
        f"https://www.orcid.org/{orcid}",
        f"orcid.org/{orcid}",
        f"https://orcid.org/{orcid}/",
    ):
        assert orcidnorm.canonical(spelling) == key


@given(orcids)
def test_canonical_is_idempotent_and_lowercase(orcid: str) -> None:
    key = orcidnorm.canonical(orcid)
    assert key == key.lower()
    assert orcidnorm.canonical(key) == key


@given(st.text(max_size=60))
def test_normalize_is_idempotent_for_any_input(text: str) -> None:
    once = orcidnorm.normalize(text)
    assert orcidnorm.normalize(once) == once


@given(orcids)
def test_the_shape_test_survives_every_spelling(orcid: str) -> None:
    for spelling in (orcid, f"orcid:{orcid}", f"https://orcid.org/{orcid}"):
        assert orcidnorm.looks_like_orcid(spelling)


@given(orcids)
def test_canonical_matches_normalize_lowercased(orcid: str) -> None:
    assert orcidnorm.canonical(orcid) == orcidnorm.normalize(orcid).lower()
