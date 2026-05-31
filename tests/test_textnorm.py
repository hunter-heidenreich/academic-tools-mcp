"""Tests for the Unicode folding helpers (``_textnorm``).

These back the opt-in ``normalize=True`` diacritic-insensitive search on
``find_in_paper`` and ``search_cached_papers``. The position-map invariant
is the load-bearing one: a match found in folded text must map back to the
correct ORIGINAL offsets so ``find_in_paper`` offsets stay aligned with
``get_paper_section``.
"""

from __future__ import annotations

from academic_tools_mcp import _textnorm


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
