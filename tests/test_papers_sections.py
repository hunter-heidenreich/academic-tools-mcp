"""Markdown structure: splitting into sections and reading one back.

Covers ``papers.sections`` — the pure half of the pipeline. No cache, no
subprocess: every test here is text in, dicts out.
"""

from academic_tools_mcp import papers
from academic_tools_mcp.papers import get_section_content, parse_sections

from ._section_fixtures import (
    _H1_MARKDOWN,
    _H2_MARKDOWN,
    _H2_ONLY_MARKDOWN,
    _H2_WITH_MANY_H3S,
    _NO_HEADINGS,
)


class TestParseSectionsH2:
    def test_captures_preamble(self):
        sections = parse_sections(_H2_MARKDOWN)
        assert sections[0]["title"] == "Preamble"
        assert sections[0]["index"] == 0

    def test_counts_all_sections(self):
        sections = parse_sections(_H2_MARKDOWN)
        # Preamble + Introduction + Related Work + Methods + Results + Conclusion
        assert len(sections) == 6

    def test_section_titles(self):
        sections = parse_sections(_H2_MARKDOWN)
        titles = [s["title"] for s in sections]
        assert titles == [
            "Preamble",
            "Introduction",
            "Related Work",
            "Methods",
            "Results",
            "Conclusion",
        ]

    def test_h3_previews(self):
        sections = parse_sections(_H2_MARKDOWN)
        intro = sections[1]
        assert intro["h3s"] == ["Background", "Motivation"]

    def test_methods_h3s(self):
        sections = parse_sections(_H2_MARKDOWN)
        methods = sections[3]
        assert methods["h3s"] == ["Architecture", "Training"]

    def test_section_with_no_h3s(self):
        sections = parse_sections(_H2_MARKDOWN)
        related = sections[2]
        assert related["h3s"] == []

    def test_approx_tokens_positive(self):
        sections = parse_sections(_H2_MARKDOWN)
        for section in sections:
            assert section["approx_tokens"] >= 1

    def test_indices_sequential(self):
        sections = parse_sections(_H2_MARKDOWN)
        for i, section in enumerate(sections):
            assert section["index"] == i

    def test_no_preamble_when_starts_with_h2(self):
        sections = parse_sections(_H2_ONLY_MARKDOWN)
        assert sections[0]["title"] == "First Section"
        assert len(sections) == 2

    def test_empty_input(self):
        sections = parse_sections("")
        assert sections == []


# ---------------------------------------------------------------------------
# parse_sections with H1-based documents (MinerU style)
# ---------------------------------------------------------------------------


class TestParseSectionsH1:
    def test_splits_on_h1(self):
        sections = parse_sections(_H1_MARKDOWN)
        titles = [s["title"] for s in sections]
        assert "1 Introduction" in titles
        assert "3 Model Architecture" in titles
        assert "7 Conclusion" in titles

    def test_title_is_preamble(self):
        """The paper title line becomes the first section (before 'Abstract')."""
        sections = parse_sections(_H1_MARKDOWN)
        assert sections[0]["title"] == "Attention Is All You Need"

    def test_subsections_as_previews(self):
        """H1 subsections (e.g. '# 3.1 ...') are NOT separate sections —
        they're not sub-level (H2). With all-H1 documents, there are no
        sub-headings to preview because everything is the same level."""
        # In the all-H1 document, subsections like "# 3.1" are the same level
        # as "# 3", so they become their own sections, not previews.
        sections = parse_sections(_H1_MARKDOWN)
        # "3 Model Architecture" should have no h3s since 3.1, 3.2 are also H1
        model_arch = [s for s in sections if "Model Architecture" in s["title"]]
        assert len(model_arch) == 1
        assert model_arch[0]["h3s"] == []

    def test_subsections_are_separate(self):
        """With all-H1, subsections like '3.1' and '3.2' are their own sections."""
        sections = parse_sections(_H1_MARKDOWN)
        titles = [s["title"] for s in sections]
        assert "3.1 Encoder and Decoder Stacks" in titles
        assert "3.2 Attention" in titles

    def test_section_count(self):
        sections = parse_sections(_H1_MARKDOWN)
        # Title + Abstract + 1 Intro + 2 Background + 3 Model + 3.1 + 3.2 +
        # 4 Self-Attn + 5 Training + 5.1 Data + 6 Results + 7 Conclusion = 12
        assert len(sections) == 12

    def test_no_headings_is_single_preamble(self):
        sections = parse_sections(_NO_HEADINGS)
        assert len(sections) == 1
        assert sections[0]["title"] == "Preamble"


class TestParseSectionsH3HeavyDocument:
    """Regression: a count-based heuristic flipped to H3-as-section once H3s
    outnumbered H2s, flattening the outline. Sections must follow the H1/H2
    boundaries regardless of how many H3s a section contains."""

    def test_section_titles(self):
        sections = parse_sections(_H2_WITH_MANY_H3S)
        assert [s["title"] for s in sections] == [
            "Title",
            "Results",
            "Methods",
            "References",
        ]

    def test_h3s_grouped_under_parent(self):
        sections = parse_sections(_H2_WITH_MANY_H3S)
        results = next(s for s in sections if s["title"] == "Results")
        assert results["h3s"] == ["Sub A", "Sub B", "Sub C", "Sub D"]
        refs = next(s for s in sections if s["title"] == "References")
        assert refs["h3s"] == ["Refs 1-10", "Refs 11-20", "Refs 21-30"]

    def test_h3s_never_promoted_to_sections(self):
        sections = parse_sections(_H2_WITH_MANY_H3S)
        titles = {s["title"] for s in sections}
        assert "Sub A" not in titles
        assert "Refs 1-10" not in titles


# ---------------------------------------------------------------------------
# get_section_content (works with both H1 and H2 documents)
# ---------------------------------------------------------------------------


class TestGetSectionContent:
    def test_get_by_index(self):
        result = get_section_content(_H2_MARKDOWN, 1)
        assert result["title"] == "Introduction"
        assert "introduction section" in result["content"]
        assert result["approx_tokens"] >= 1

    def test_get_by_title(self):
        result = get_section_content(_H2_MARKDOWN, "Methods")
        assert result["title"] == "Methods"
        assert "Architecture" in result["content"]

    def test_get_by_partial_title(self):
        result = get_section_content(_H2_MARKDOWN, "intro")
        assert result["title"] == "Introduction"

    def test_case_insensitive_title(self):
        result = get_section_content(_H2_MARKDOWN, "CONCLUSION")
        assert result["title"] == "Conclusion"

    def test_index_out_of_range(self):
        result = get_section_content(_H2_MARKDOWN, 99)
        assert "error" in result

    def test_no_title_match(self):
        result = get_section_content(_H2_MARKDOWN, "Nonexistent")
        assert "error" in result
        assert "Available" in result["error"]

    def test_ambiguous_title(self):
        # "Re" matches both "Related Work" and "Results"
        result = get_section_content(_H2_MARKDOWN, "Re")
        assert "error" in result
        assert "Ambiguous" in result["error"]

    def test_ascii_query_finds_accented_title(self):
        """An agent typing the ASCII spelling of an accented heading still
        lands on it — the exact pass misses, the diacritic-folded pass hits."""
        md = "## Résumé\n\nUn résumé.\n\n## Methods\n\nBody.\n"
        result = get_section_content(md, "Resume")
        assert result["title"] == "Résumé"

    def test_accented_query_finds_ascii_title(self):
        md = "## Resume\n\nA summary.\n\n## Methods\n\nBody.\n"
        result = get_section_content(md, "Résumé")
        assert result["title"] == "Resume"

    def test_exact_match_wins_over_folded(self):
        """Folding runs only when the exact pass finds nothing, so a paper
        carrying both spellings resolves instead of erroring as ambiguous."""
        md = "## Resume\n\nA.\n\n## Résumé\n\nB.\n"
        assert get_section_content(md, "Resume")["title"] == "Resume"
        assert get_section_content(md, "Résumé")["title"] == "Résumé"

    def test_folded_pass_can_still_be_ambiguous(self):
        md = "## Résumé\n\nA.\n\n## Resumé\n\nB.\n"
        result = get_section_content(md, "Resume")
        assert "Ambiguous" in result["error"]

    def test_folded_miss_still_lists_available_titles(self):
        md = "## Résumé\n\nA.\n"
        result = get_section_content(md, "Nonexistent")
        assert "Available" in result["error"]

    def test_preamble_by_index(self):
        result = get_section_content(_H2_MARKDOWN, 0)
        assert result["title"] == "Preamble"
        assert "preamble text" in result["content"]

    def test_section_includes_h3_content(self):
        result = get_section_content(_H2_MARKDOWN, "Methods")
        assert "### Architecture" in result["content"]
        assert "### Training" in result["content"]
        assert "Training details" in result["content"]

    def test_h1_document_get_by_title(self):
        result = get_section_content(_H1_MARKDOWN, "Introduction")
        assert result["title"] == "1 Introduction"
        assert "state of the art" in result["content"]

    def test_h1_document_get_by_index(self):
        result = get_section_content(_H1_MARKDOWN, 0)
        assert result["title"] == "Attention Is All You Need"

    def test_h2_only_get_by_index_zero(self):
        result = get_section_content(_H2_ONLY_MARKDOWN, 0)
        assert result["title"] == "First Section"

    # -- Pagination tests --

    def test_default_returns_full_section_in_one_slice(self):
        result = get_section_content(_H2_MARKDOWN, "Methods")
        assert result["offset"] == 0
        assert result["has_more"] is False
        assert result["next_offset"] is None
        assert result["chars_returned"] == result["total_chars"]
        assert "index" in result

    def test_small_max_chars_returns_first_slice(self):
        result = get_section_content(_H2_MARKDOWN, "Methods", max_chars=20)
        assert result["offset"] == 0
        assert result["chars_returned"] == 20
        assert len(result["content"]) == 20
        assert result["has_more"] is True
        assert result["next_offset"] == 20
        assert result["total_chars"] > 20

    def test_pagination_continuation_is_contiguous(self):
        first = get_section_content(_H2_MARKDOWN, "Methods", max_chars=20)
        second = get_section_content(
            _H2_MARKDOWN,
            "Methods",
            offset=first["next_offset"],
            max_chars=20,
        )
        assert second["offset"] == 20
        full = get_section_content(_H2_MARKDOWN, "Methods")
        assert full["content"].startswith(first["content"] + second["content"])

    def test_offset_at_total_chars_returns_empty_no_more(self):
        full = get_section_content(_H2_MARKDOWN, "Methods")
        end = get_section_content(
            _H2_MARKDOWN,
            "Methods",
            offset=full["total_chars"],
        )
        assert end["chars_returned"] == 0
        assert end["content"] == ""
        assert end["has_more"] is False
        assert end["next_offset"] is None

    def test_offset_beyond_section_errors(self):
        full = get_section_content(_H2_MARKDOWN, "Methods")
        result = get_section_content(
            _H2_MARKDOWN,
            "Methods",
            offset=full["total_chars"] + 100,
        )
        assert "error" in result

    def test_negative_offset_errors(self):
        result = get_section_content(_H2_MARKDOWN, "Methods", offset=-1)
        assert "error" in result

    def test_zero_or_negative_max_chars_errors(self):
        for bad in (0, -1):
            result = get_section_content(_H2_MARKDOWN, "Methods", max_chars=bad)
            assert "error" in result

    def test_approx_tokens_reflects_full_section_not_slice(self):
        full = get_section_content(_H2_MARKDOWN, "Methods")
        sliced = get_section_content(_H2_MARKDOWN, "Methods", max_chars=20)
        assert sliced["approx_tokens"] == full["approx_tokens"]
        assert sliced["total_chars"] == full["total_chars"]

    def test_resolved_index_returned_for_title_lookup(self):
        # _H2_MARKDOWN: Preamble, Introduction, Methods, ...
        result = get_section_content(_H2_MARKDOWN, "Methods")
        same_by_index = get_section_content(_H2_MARKDOWN, result["index"])
        assert result["title"] == same_by_index["title"]
        assert result["content"] == same_by_index["content"]


class TestApproxTokensAgreeAcrossTools:
    """``get_paper_sections`` and ``get_paper_section`` reported different
    ``approx_tokens`` for the same section.

    ``parse_sections`` measured the *unstripped* line join, so every estimate
    was inflated by the section's surrounding blank lines, and
    ``get_paper_sections``' ``total_approx_tokens`` summed the inflated
    variant. ``get_section_content`` measured the stripped text it actually
    returns. Existing tests only asserted the value was positive.
    """

    MARKDOWN = (
        "# Paper\n\n"
        "## Introduction\n\n\n\n"
        "Some introductory prose that runs on for a little while.\n\n\n\n"
        "## Methods\n\n"
        "We did the following things in a reasonably long paragraph.\n\n\n\n\n"
        "## Results\n\n"
        "It worked.\n\n\n"
    )

    def test_every_section_agrees(self):
        sections = papers.parse_sections(self.MARKDOWN)
        assert sections, "fixture produced no sections"
        for entry in sections:
            content = papers.get_section_content(self.MARKDOWN, entry["index"])
            assert entry["approx_tokens"] == content["approx_tokens"], (
                f"section {entry['index']} ({entry['title']!r}) disagrees"
            )

    def test_agrees_when_looked_up_by_title(self):
        sections = papers.parse_sections(self.MARKDOWN)
        for entry in sections:
            content = papers.get_section_content(self.MARKDOWN, entry["title"])
            assert entry["approx_tokens"] == content["approx_tokens"]

    def test_index_total_matches_the_sum_of_the_parts(self):
        sections = papers.parse_sections(self.MARKDOWN)
        index_total = sum(s["approx_tokens"] for s in sections)
        read_total = sum(
            papers.get_section_content(self.MARKDOWN, s["index"])["approx_tokens"] for s in sections
        )
        assert index_total == read_total

    def test_trailing_blank_lines_do_not_inflate_the_estimate(self):
        lean = "## A\n\nbody text here\n"
        padded = "## A\n\nbody text here\n\n\n\n\n\n\n\n\n\n"
        assert (
            papers.parse_sections(lean)[0]["approx_tokens"]
            == papers.parse_sections(padded)[0]["approx_tokens"]
        )


class TestFirstSectionHeading:
    """The single home for 'which heading levels are title-level', so the
    corpus index and the section index cannot disagree.
    """

    def test_finds_an_h1(self):
        assert papers.first_section_heading("# Attention\n\nbody\n") == "Attention"

    def test_finds_an_h2(self):
        assert papers.first_section_heading("## Attention\n\nbody\n") == "Attention"

    def test_skips_preamble_prose(self):
        assert papers.first_section_heading("some prose\n\n## Real\n\nb\n") == "Real"

    def test_an_h3_is_not_title_level(self):
        assert papers.first_section_heading("### Sub\n\nbody\n") is None

    def test_a_headingless_document_has_none(self):
        assert papers.first_section_heading("just prose\n") is None

    def test_the_first_of_several_wins(self):
        assert papers.first_section_heading("# A\n\nx\n\n# B\n\ny\n") == "A"


class TestSectionAtOffsetPastTheLastSection:
    def test_an_offset_in_a_trailing_empty_section_resolves_to_the_last_real_one(self):
        # "# B" opens a section with no body, so it is filtered out and the
        # offset lands past every surviving span.
        markdown = "# A\n\nbody\n\n# B\n\n\n"
        offset = markdown.index("# B")
        assert papers.section_at_offset(markdown, offset) == (0, "A")

    def test_an_offset_past_the_end_of_the_document_still_resolves(self):
        markdown = "# A\n\nbody\n\n# B\n\n\n"
        index, title = papers.section_at_offset(markdown, len(markdown) * 10)
        assert (index, title) == (0, "A")
        assert "error" not in papers.get_section_content(markdown, index)
