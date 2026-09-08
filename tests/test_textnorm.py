"""Tests for the Unicode folding helpers (``_textnorm``).

These back the opt-in ``normalize=True`` diacritic-insensitive search on
``find_in_paper`` and ``search_cached_papers``. The position-map invariant
is the load-bearing one: a match found in folded text must map back to the
correct ORIGINAL offsets so ``find_in_paper`` offsets stay aligned with
``get_paper_section``.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
    "ΟΔΥΣΣΕΥΣ",  # word-final Greek sigma: str.lower() is context-sensitive here
    "İX",  # 1→2 lowercase expansion followed by another char
    "½",  # compatibility decomposition to three chars
]

# Deliberately weighted toward the corners: ASCII runs (the fast path), the
# combining marks and expansions that make the transforms length-changing,
# and the sigma whose lowercase depends on its neighbours.
_TEXT = st.text(
    alphabet=st.sampled_from(
        [*"abZ9 .\n", "é", "\u0301", "\u0327", "ﬁ", "İ", "Σ", "ς", "½", "①", "Ａ", "水"]
    ),
    max_size=24,
)


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

    def test_partial_expansion_span_covers_the_whole_original_char(self):
        """A match on only the "f" of a folded "ﬁ" indexes the map to an EMPTY
        original span; ``original_span`` widens it to the character that
        produced the match, so ``find_in_paper`` reports a non-empty ``match``."""
        text = "aﬁb"
        _, index_map = _textnorm.fold_with_map(text)
        assert index_map[1] == index_map[2] == 1  # both halves of "fi"
        lo, hi = _textnorm.original_span(index_map, 1, 2)
        assert text[lo:hi] == "ﬁ"

    def test_span_over_whole_chars_is_unchanged(self):
        text = "a café b"
        folded, index_map = _textnorm.fold_with_map(text)
        start = folded.index("cafe")
        lo, hi = _textnorm.original_span(index_map, start, start + 4)
        assert (lo, hi) == (index_map[start], index_map[start + 4])
        assert text[lo:hi] == "café"

    def test_span_keeps_a_trailing_combining_mark(self):
        """The dropped mark lives past the last mapped char, so only the end
        sentinel reaches it — the widened end must not shadow ``index_map[end]``
        or a match on "cafe" would report "cafe" and lose the accent."""
        text = "cafe\u0301 x"
        folded, index_map = _textnorm.fold_with_map(text)
        assert folded == "cafe x"
        lo, hi = _textnorm.original_span(index_map, 0, 4)
        # The mark is carried through as-is: the slice is the decomposed
        # spelling the document actually holds, not a recomposed one.
        assert text[lo:hi] == "cafe\u0301"

    def test_empty_span_stays_empty(self):
        _, index_map = _textnorm.fold_with_map("abc")
        assert _textnorm.original_span(index_map, 2, 2) == (2, 2)

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
    """``fold`` (whole-string) and the per-original-character index map are
    two separate computations; their *lengths* must stay in lockstep, or every
    offset a consumer reports lands on the wrong character."""

    @pytest.mark.parametrize("text", _TRICKY)
    def test_folded_strings_agree(self, text):
        assert _textnorm.fold(text) == _textnorm.fold_with_map(text)[0]


class TestOffsetMapProperties:
    """The index-map contract, over arbitrary text rather than examples.

    Each property is a way the map can silently misplace a snippet: a wrong
    length indexes past the end or drops the tail, a non-monotonic entry
    reports a backwards span, and an out-of-range entry slices nothing.
    """

    @given(_TEXT)
    def test_map_length_matches_transform(self, text):
        for transformed, index_map in (
            _textnorm.fold_with_map(text),
            _textnorm.lower_with_map(text),
            _textnorm.lower_with_map(text, fold=True),
        ):
            assert len(index_map) == len(transformed) + 1
            assert index_map[-1] == len(text)

    @given(_TEXT)
    def test_map_is_monotonic_and_in_range(self, text):
        for _, index_map in (
            _textnorm.fold_with_map(text),
            _textnorm.lower_with_map(text),
            _textnorm.lower_with_map(text, fold=True),
        ):
            assert all(0 <= i <= len(text) for i in index_map)
            assert index_map == sorted(index_map)

    @given(_TEXT, st.integers(min_value=0), st.integers(min_value=0))
    def test_any_span_maps_to_a_covering_original_slice(self, text, start, length):
        """Every non-empty transformed span maps to a non-empty original span
        that contains every character the span came from — the guarantee
        ``find_in_paper`` relies on when it reports ``match`` and
        ``char_offset`` against the un-folded text."""
        transformed, index_map = _textnorm.lower_with_map(text, fold=True)
        if not transformed:
            return
        start %= len(transformed)
        end = min(len(transformed), start + 1 + length % len(transformed))
        lo, hi = _textnorm.original_span(index_map, start, end)
        assert lo < hi <= len(text)
        assert all(lo <= index_map[i] < hi for i in range(start, end))

    @given(_TEXT)
    def test_output_equals_whole_string_transform(self, text):
        """Per-character mapping never changes *what* the transform emits —
        this is what keeps the tokeniser and the snippet locator on one
        vocabulary."""
        assert _textnorm.fold_with_map(text)[0] == _textnorm.fold(text)
        assert _textnorm.lower_with_map(text)[0] == text.lower()
        assert _textnorm.lower_with_map(text, fold=True)[0] == _textnorm.fold(text).lower()


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

    def test_word_final_sigma_matches_str_lower(self):
        """``str.lower()`` is context-sensitive: a word-final Σ lowercases to
        'ς', not 'σ'. Lowercasing per character emits 'σ' and the search term
        — built by whole-string ``.lower()`` — then matches nothing, so a Greek
        hit comes back with no snippet and no section."""
        text = "ΟΔΥΣΣΕΥΣ"
        lowered, index_map = _textnorm.lower_with_map(text)
        assert lowered == text.lower() == "οδυσσευς"
        assert len(index_map) == len(lowered) + 1
        assert text[index_map[7] : index_map[8]] == "Σ"

    def test_compatibility_expansion_maps_to_one_index(self):
        # "½" → "1⁄2": three transformed chars, all from original index 0.
        lowered, index_map = _textnorm.lower_with_map("½a", fold=True)
        assert lowered == "1\u20442a"
        assert index_map == [0, 0, 0, 1, 2]

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
