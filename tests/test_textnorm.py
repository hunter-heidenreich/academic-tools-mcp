"""Tests for the Unicode folding helpers (``_textnorm``).

These back the opt-in ``normalize=True`` diacritic-insensitive search on
``find_in_paper`` and ``search_cached_papers``. The position-map invariant
is the load-bearing one: a match found in folded text must map back to the
correct ORIGINAL offsets so ``find_in_paper`` offsets stay aligned with
``get_paper_section``.
"""

from __future__ import annotations

import pytest

from academic_tools_mcp import _textnorm

# Strings that exercise the tricky corners of NFKD folding: stacked combining
# marks on one base, precomposed glyphs that decompose, ligatures (1→N), and
# non-letter compatibility decompositions (circled / fullwidth / superscript).
_TRICKY = [
    "",
    "transformer",
    "café",
    "é̀",  # base + acute + grave (two combining marks on one base)
    "ế",  # Vietnamese precomposed → base + two combining marks
    "ab́",  # trailing combining mark after a base char
    "́ab",  # leading combining mark
    "ﬁle",  # ligature mid-word
    "①",  # circled digit one → "1"
    "Ａ",  # fullwidth A → "A"
    "²",  # superscript two → "2"
]


class TestFold:
    def test_strips_common_diacritics(self):
        assert _textnorm.fold("café") == "cafe"
        assert _textnorm.fold("naïve") == "naive"
        assert _textnorm.fold("Gutiérrez") == "Gutierrez"

    def test_ligature_expands(self):
        # U+FB01 LATIN SMALL LIGATURE FI → "fi"
        assert _textnorm.fold("ﬁ") == "fi"

    def test_preserves_case(self):
        # Folding is independent of case-folding.
        assert _textnorm.fold("Café") == "Cafe"

    def test_empty_and_lone_combining_mark(self):
        assert _textnorm.fold("") == ""
        # A lone combining acute accent folds to nothing.
        assert _textnorm.fold("́") == ""

    def test_ascii_unchanged(self):
        assert _textnorm.fold("transformer") == "transformer"


class TestFoldWithMap:
    def test_map_length_and_end_sentinel(self):
        folded, index_map = _textnorm.fold_with_map("café")
        assert folded == "cafe"
        assert len(index_map) == len(folded) + 1
        assert index_map[-1] == len("café")

    def test_offset_round_trip(self):
        text = "a café b"
        folded, index_map = _textnorm.fold_with_map(text)
        start = folded.index("cafe")
        end = start + len("cafe")
        orig_start, orig_end = index_map[start], index_map[end]
        assert text[orig_start:orig_end] == "café"

    def test_ligature_maps_to_single_original_index(self):
        text = "aﬁb"
        folded, index_map = _textnorm.fold_with_map(text)
        assert folded == "afib"
        # Both folded chars from the ligature map to original index 1.
        start = folded.index("fi")  # == 1
        end = start + len("fi")
        assert index_map[start] == 1
        # The whole original ligature char is recovered, not half of it.
        assert text[index_map[start] : index_map[end]] == "ﬁ"

    def test_leading_combining_mark_skipped(self):
        folded, index_map = _textnorm.fold_with_map("́ab")
        assert folded == "ab"
        assert index_map[0] == 1  # first folded char comes from original idx 1

    def test_empty_input(self):
        assert _textnorm.fold_with_map("") == ("", [0])

    def test_all_combining_input(self):
        assert _textnorm.fold_with_map("́̀") == ("", [2])

    def test_trailing_combining_mark(self):
        # A combining mark after a base char folds away; the base maps to
        # its own index and the end sentinel jumps past the dropped mark.
        folded, index_map = _textnorm.fold_with_map("ab́")
        assert folded == "ab"
        assert index_map == [0, 1, 3]

    def test_match_at_end_of_folded_text(self):
        # A match ending at the very end of the folded text must round-trip
        # through the end sentinel back to the end of the original string.
        text = "a café"
        folded, index_map = _textnorm.fold_with_map(text)
        start = folded.index("cafe")
        end = start + len("cafe")  # == len(folded)
        assert end == len(folded)
        assert text[index_map[start] : index_map[end]] == "café"


class TestFoldEquivalence:
    """``fold`` (whole-string NFKD) and ``fold_with_map`` (per-char NFKD) are
    two separate implementations; their folded output MUST stay identical, or
    ``search_cached_papers`` would tokenise (``fold``) and extract snippets
    (``fold_with_map``) against divergent text."""

    @pytest.mark.parametrize("text", _TRICKY)
    def test_folded_strings_agree(self, text):
        assert _textnorm.fold(text) == _textnorm.fold_with_map(text)[0]


class TestLowerWithMap:
    """``lower_with_map`` lowercases (optionally NFKD-folding first) while
    tracking an index map back to ORIGINAL offsets. The snippet locator in
    ``cache_search`` searches the lowered/folded text but must slice the
    ORIGINAL markdown, so length-changing lowercase mappings (U+0130) and
    folding expansions (ligatures) both have to round-trip."""

    def test_basic_lowercase_round_trip(self):
        text = "Hello WORLD"
        lowered, index_map = _textnorm.lower_with_map(text)
        assert lowered == "hello world"
        assert len(index_map) == len(lowered) + 1
        assert index_map[-1] == len(text)
        start = lowered.index("world")
        end = start + len("world")
        assert text[index_map[start] : index_map[end]] == "WORLD"

    def test_expanding_lowercase_keeps_offsets_aligned(self):
        # U+0130 'İ'.lower() == 'i' + combining dot (2 chars), so the lowered
        # string is LONGER than the original. The char AFTER it must still map
        # back to its true original index — this is exactly the drift that
        # corrupted snippet/section offsets on the default search path.
        text = "İX"  # 'İ' at original index 0, 'X' at original index 1
        lowered, index_map = _textnorm.lower_with_map(text)
        assert len(lowered) == 3  # 'i', combining dot, 'x'
        assert index_map[lowered.index("x")] == 1

    def test_fold_and_lower_together(self):
        text = "CafÉ"
        lowered, index_map = _textnorm.lower_with_map(text, fold=True)
        assert lowered == "cafe"
        start = lowered.index("cafe")
        assert text[index_map[start] : index_map[start + 4]] == "CafÉ"

    def test_empty_input(self):
        assert _textnorm.lower_with_map("") == ("", [0])
        assert _textnorm.lower_with_map("", fold=True) == ("", [0])

    @pytest.mark.parametrize("text", _TRICKY)
    def test_equivalence_with_whole_string_transforms(self, text):
        # Per-char transform must equal the whole-string transform, or the
        # tokeniser (fold().lower()) and the snippet locator (lower_with_map)
        # would disagree on the BM25 vocabulary.
        assert _textnorm.lower_with_map(text, fold=True)[0] == _textnorm.fold(text).lower()
        assert _textnorm.lower_with_map(text, fold=False)[0] == text.lower()
