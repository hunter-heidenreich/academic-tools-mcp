"""Unit tests for ``util/orcidnorm.py``.

Four spellings reach us and only ``https://orcid.org/...`` resolves upstream, so
folding them to one key is what stops one person occupying four cache entries.
"""

import pytest

from academic_tools_mcp.util import orcidnorm

ORCID = "0000-0001-6187-6610"


class TestNormalizeAccepted:
    @pytest.mark.parametrize(
        "spelling",
        [
            ORCID,
            f"  {ORCID}  ",
            f"orcid:{ORCID}",
            f"ORCID:{ORCID}",
            f"orcid: {ORCID}",
            f"https://orcid.org/{ORCID}",
            f"http://orcid.org/{ORCID}",
            f"https://www.orcid.org/{ORCID}",
            f"HTTPS://ORCID.ORG/{ORCID}",
            f"orcid.org/{ORCID}",
            f"https://orcid.org/{ORCID}/",
            f"orcid:https://orcid.org/{ORCID}",
        ],
    )
    def test_folds_to_the_bare_form(self, spelling):
        assert orcidnorm.normalize(spelling) == ORCID

    def test_a_query_or_fragment_is_cut_from_a_url(self):
        assert orcidnorm.normalize(f"https://orcid.org/{ORCID}?lang=en") == ORCID
        assert orcidnorm.normalize(f"https://orcid.org/{ORCID}#bio") == ORCID


class TestNormalizeRejected:
    @pytest.mark.parametrize(
        "text",
        ["", "   ", "not-an-orcid", "A5023888391", "0000-0001-6187", "0000-0001-6187-66103"],
    )
    def test_unrecognised_input_is_returned_stripped(self, text):
        result = orcidnorm.normalize(text)
        assert result == text.strip()
        assert not orcidnorm.looks_like_orcid(text)


class TestCheckCharacter:
    """The last character may be `X`; OpenAlex resolves it in either case."""

    def test_an_x_checksum_is_orcid_shaped(self):
        assert orcidnorm.looks_like_orcid("0000-0003-3293-488X")
        assert orcidnorm.looks_like_orcid("0000-0003-3293-488x")

    def test_the_two_cases_are_one_key(self):
        assert orcidnorm.canonical("0000-0003-3293-488X") == "0000-0003-3293-488x"

    def test_x_is_only_legal_in_the_last_position(self):
        assert not orcidnorm.looks_like_orcid("0000-0003-329X-4881")


class TestCanonical:
    def test_lowercases_the_normalized_form(self):
        assert orcidnorm.canonical(f"HTTPS://ORCID.ORG/{ORCID}") == ORCID

    def test_every_spelling_is_one_key(self):
        keys = {
            orcidnorm.canonical(s)
            for s in (
                ORCID,
                f"orcid:{ORCID}",
                f"https://orcid.org/{ORCID}",
                f"http://orcid.org/{ORCID}",
            )
        }
        assert keys == {ORCID}


class TestLooksLikeOrcid:
    @pytest.mark.parametrize("spelling", [ORCID, f"orcid:{ORCID}", f"https://orcid.org/{ORCID}"])
    def test_accepts_every_spelling(self, spelling):
        assert orcidnorm.looks_like_orcid(spelling)

    def test_rejects_a_doi(self):
        assert not orcidnorm.looks_like_orcid("10.1234/example")
