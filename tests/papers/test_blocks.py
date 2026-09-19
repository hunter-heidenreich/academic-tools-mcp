"""Markdown block emitters shared by the markup renderers: ``papers.blocks``."""

import pytest

from academic_tools_mcp.papers import blocks


class TestEscapeHeadings:
    def test_every_line_leading_heading_marker_is_escaped(self):
        assert blocks.escape_headings("# a\nb # c\n## d") == "\\# a\nb # c\n\\## d"

    @pytest.mark.parametrize("line", ["#include <stdio.h>", "#1 result", "#hashtag"])
    def test_a_hash_with_no_following_space_is_left_alone(self, line):
        """Escaping tracks ``sections._HEADING_RE``, which wants ``#`` then whitespace.

        A stray backslash through ``#include`` or a "#1 ranked" claim is corruption
        of agent-visible prose, and neither line could have opened a section.
        """
        assert blocks.escape_headings(line) == line

    def test_seven_hashes_are_left_alone(self):
        """Markdown stops at h6, and so does the heading scan this guards."""
        assert blocks.escape_headings("####### seven") == "####### seven"


class TestSpan:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(None, 1), ("", 1), ("1", 1), ("0", 1), ("-2", 1), ("x", 1), (" 3 ", 3), ("100", 100)],
    )
    def test_values(self, value, expected):
        assert blocks.span(value) == expected

    def test_one_past_the_cap_is_capped(self):
        assert blocks.span(str(blocks.MAX_SPAN + 1)) == blocks.MAX_SPAN


class TestPipeTable:
    def test_no_rows_is_empty(self):
        assert blocks.pipe_table([]) == ""

    def test_all_blank_rows_are_dropped(self):
        assert blocks.pipe_table([[("", 1, 1)]]) == ""

    def test_the_first_row_is_the_header_and_pipes_are_escaped(self):
        assert blocks.pipe_table([[("a|b", 1, 1)], [("c", 1, 1)]]) == ("| a\\|b |\n| --- |\n| c |")

    def test_a_colspan_pads_and_short_rows_widen(self):
        assert blocks.pipe_table([[("h", 2, 1)], [("x", 1, 1), ("y", 1, 1)], [("z", 1, 1)]]) == (
            "| h |  |\n| --- | --- |\n| x | y |\n| z |  |"
        )

    def test_a_rowspan_blanks_the_covered_cells_below(self):
        assert (
            blocks.pipe_table(
                [[("a", 1, 1), ("b", 1, 1)], [("r", 1, 2), ("1", 1, 1)], [("2", 1, 1)]]
            )
            == "| a | b |\n| --- | --- |\n| r | 1 |\n|  | 2 |"
        )
