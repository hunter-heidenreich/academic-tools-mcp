"""Unit tests for the shared DOI normalizer.

`test_doi_properties.py` pins the invariants that hold for *every* DOI; this
file pins the individual decisions those properties can't express — where the
accepted-spelling boundary sits, and which near-miss forms are deliberately
rejected rather than accidentally unhandled.
"""

import pytest

from academic_tools_mcp import _doi


class TestNormalizeAccepted:
    """The spellings `normalize` documents it accepts."""

    @pytest.mark.parametrize(
        "raw",
        [
            "10.1234/example",
            "  10.1234/example  ",
            "doi:10.1234/example",
            "DOI:10.1234/example",
            "Doi:10.1234/example",
            "doi: 10.1234/example",
            "doi:\t10.1234/example",
            "https://doi.org/10.1234/example",
            "http://doi.org/10.1234/example",
            "https://dx.doi.org/10.1234/example",
            "http://dx.doi.org/10.1234/example",
            "https://www.doi.org/10.1234/example",
            "HTTPS://DOI.ORG/10.1234/example",
            "https://doi.org/10.1234/example?utm_source=x",
            "https://doi.org/10.1234/example#abstract",
            "doi:https://doi.org/10.1234/example",
            "doi: https://dx.doi.org/10.1234/example",
        ],
    )
    def test_collapses_to_bare_form(self, raw):
        assert _doi.normalize(raw) == "10.1234/example"

    def test_case_of_the_suffix_is_preserved(self):
        # `normalize` feeds the *request*; only `canonical` lowercases.
        assert _doi.normalize("https://doi.org/10.1234/Example") == "10.1234/Example"

    def test_suffix_may_contain_slashes(self):
        assert _doi.normalize("https://doi.org/10.18653/v1/2023.acl-long.1") == (
            "10.18653/v1/2023.acl-long.1"
        )


class TestNormalizeRejected:
    """Near-miss forms that pass through untouched.

    Each is a deliberate non-goal: `normalize` returns a string it does not
    recognise unchanged (bar the strip), and `looks_like_doi` then reports it
    is not a DOI. Pinning them stops a future "helpful" widening from being
    silent.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "info:doi/10.1234/example",  # legacy OpenURL URN form
            "DOI 10.1234/example",  # space, no colon
            "(10.1234/example)",  # wrapped in prose punctuation
            "https://doi.org/",  # no DOI path at all
            "https://example.com/10.1234/example",  # not a DOI resolver
        ],
    )
    def test_passes_through_and_is_not_a_doi(self, raw):
        assert _doi.normalize(raw) == raw.strip()
        assert _doi.looks_like_doi(raw) is False

    def test_trailing_sentence_period_is_kept(self):
        # A '.' is legal in a DOI suffix, so it cannot be stripped on
        # suspicion; the caller pasted it and the caller owns it.
        assert _doi.normalize("https://doi.org/10.1234/example.") == "10.1234/example."


class TestBareDoiIsVerbatim:
    """A bare DOI keeps `?` and `#`; the URL form has to cut at them.

    Both are legal DOI suffix characters, so truncating a bare DOI there would
    silently key a *different* paper. In a URL they are unresolvable unless
    percent-encoded, so a literal one is a query string, not part of the DOI.
    This asymmetry is the documented policy, not an oversight.
    """

    def test_bare_query_is_part_of_the_doi(self):
        assert _doi.normalize("10.1234/example?utm=1") == "10.1234/example?utm=1"

    def test_bare_fragment_is_part_of_the_doi(self):
        assert _doi.normalize("10.1234/ex#frag") == "10.1234/ex#frag"

    def test_url_query_is_discarded(self):
        assert _doi.normalize("https://doi.org/10.1234/example?utm=1") == "10.1234/example"

    def test_percent_encoded_url_suffix_survives(self):
        # The escape hatch for a DOI that really does contain '#'.
        assert _doi.normalize("https://doi.org/10.1234/ex%23frag") == "10.1234/ex%23frag"


class TestRegistrantBoundary:
    """`10.` + four-or-more digits. Exactly at the boundary must pass."""

    def test_three_digit_registrant_is_not_a_doi(self):
        assert _doi.looks_like_doi("10.123/x") is False
        assert _doi.normalize("https://doi.org/10.123/x") == "https://doi.org/10.123/x"

    def test_four_digit_registrant_is_a_doi(self):
        assert _doi.looks_like_doi("10.1234/x") is True
        assert _doi.normalize("https://doi.org/10.1234/x") == "10.1234/x"

    def test_long_registrant_is_a_doi(self):
        assert _doi.looks_like_doi("10.48550/arXiv.2301.00001") is True

    def test_non_numeric_registrant_is_not_a_doi(self):
        assert _doi.looks_like_doi("10.abcd/x") is False


class TestLooksLikeDoi:
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "my-paper-2024",
            "2301.00001",  # arXiv ID
            "hep-th/9901001",  # old-style arXiv ID
            "10.1234",  # registrant only, no separator
            "10.1234/",  # empty suffix
            "10.1234/ x",  # whitespace in the suffix
            "doi:",
        ],
    )
    def test_negative(self, raw):
        assert _doi.looks_like_doi(raw) is False

    def test_empty_string_normalizes_to_empty(self):
        assert _doi.normalize("") == ""
        assert _doi.canonical("") == ""


class TestCanonical:
    def test_lowercases(self):
        assert _doi.canonical("10.1234/ABC") == "10.1234/abc"

    def test_lowercases_through_every_wrapper_form(self):
        assert _doi.canonical("DOI:https://DX.DOI.ORG/10.1234/ABC") == "10.1234/abc"


class TestIdempotence:
    """`normalize` is idempotent, which is what lets `canonical` be stable."""

    @pytest.mark.parametrize(
        "raw",
        [
            "10.1234/example",
            "https://doi.org/10.1234/example?x=1",
            "doi: https://dx.doi.org/10.1234/example",
            "doi:doi:10.1234/example",
            "my-paper-2024",
            "",
        ],
    )
    def test_normalize_is_idempotent(self, raw):
        once = _doi.normalize(raw)
        assert _doi.normalize(once) == once

    def test_repeated_prefix_is_fully_stripped(self):
        # A single-pass strip would leave "doi:10.1234/x", which then fails the
        # DOI regex and keys separately from its own output.
        assert _doi.normalize("doi:DOI: 10.1234/x") == "10.1234/x"


# ---------------------------------------------------------------------------
# Provider wrapper delegation
# ---------------------------------------------------------------------------

# `openalex`, `crossref` and `opencitations` each expose a `_normalize_doi` /
# `canonical_doi` pair that is pure delegation — the indirection exists so the
# tool layer imports a provider symbol, not `_doi` directly. What matters is
# that they *delegate*; re-deriving `_doi`'s behaviour once per provider says
# nothing extra and rots into three copies of the same expectations.
#
# `biorxiv` and `acl_anthology` are deliberately absent: they layer real policy
# on top of `_doi.normalize` (a content-URL form, an Anthology prefix), so
# equality with `_doi` is not their contract. Their own tests cover them.
_DELEGATING_PROVIDERS = ["openalex", "crossref", "opencitations"]

_SPELLINGS = [
    "10.1038/Nature12373",
    "  10.1038/nature12373  ",
    "doi:10.1038/nature12373",
    "DOI: 10.1038/nature12373",
    "https://doi.org/10.1038/Nature12373",
    "http://doi.org/10.1038/nature12373",
    "https://dx.doi.org/10.1038/nature12373",
    "not-a-doi",
]


@pytest.fixture(params=_DELEGATING_PROVIDERS)
def provider(request):
    from importlib import import_module

    return import_module(f"academic_tools_mcp.providers.{request.param}")


@pytest.mark.parametrize("raw", _SPELLINGS)
def test_provider_normalize_delegates_to_doi(provider, raw):
    assert provider._normalize_doi(raw) == _doi.normalize(raw)


@pytest.mark.parametrize("raw", _SPELLINGS)
def test_provider_canonical_delegates_to_doi(provider, raw):
    assert provider.canonical_doi(raw) == _doi.canonical(raw)


def test_delegation_anchors_on_a_real_value(provider):
    """One concrete expectation, so the delegation tests can't all pass vacuously."""
    assert provider.canonical_doi("https://doi.org/10.1038/Nature12373") == "10.1038/nature12373"
