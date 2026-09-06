"""Navigating a converted paper: the chain from a corpus hit to a section read.

Two failures made that chain unreliable on a real corpus:

- ``search_cached_papers`` returned the section's *title*, and its docstring
  told the agent to chain that into ``get_paper_section``. Titles are not
  unique — roughly one paper in nine repeats a heading — and a repeated title
  is rejected as ambiguous, so the documented workflow dead-ended.
- A document with no headings collapses to one synthetic "Preamble" section,
  reported identically to a paper that genuinely has one section. Every
  100 KB+ single-section paper in a real corpus was this case (theses), where
  blind paging is the worst available reading strategy.
"""

import pytest

from academic_tools_mcp import cache, cache_search, papers
from academic_tools_mcp.tools import pipeline as pipeline_tools
from academic_tools_mcp.tools import search as search_tools

DUPLICATE_TITLES = (
    "# Paper\n\n"
    "## Results\n\nalpha finding about transformers\n\n"
    "## Methods\n\nmiddle content\n\n"
    "## Results\n\nbeta finding about transformers\n"
)
NO_HEADINGS = "plain layout text with no markdown headings at all\n" * 200


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
    md = tmp_path / "manual" / "markdown"
    md.mkdir(parents=True)
    (md / "dup.md").write_text(DUPLICATE_TITLES, encoding="utf-8")
    (md / "flat.md").write_text(NO_HEADINGS, encoding="utf-8")
    return md


class TestSharedBoundaries:
    """All four former dialects now share one implementation."""

    def test_parse_sections_and_reader_agree_on_titles(self):
        spans = papers.section_boundaries(DUPLICATE_TITLES)
        parsed = papers.parse_sections(DUPLICATE_TITLES)
        assert [sp.title for sp in spans] == [p["title"] for p in parsed]

    def test_every_index_is_readable(self):
        for entry in papers.parse_sections(DUPLICATE_TITLES):
            got = papers.get_section_content(DUPLICATE_TITLES, entry["index"])
            assert "error" not in got
            assert got["title"] == entry["title"]

    def test_find_in_markdown_agrees_with_the_index(self):
        hits, _ = papers.find_in_markdown(DUPLICATE_TITLES, "beta finding")
        assert hits
        entry = papers.parse_sections(DUPLICATE_TITLES)[hits[0]["section_index"]]
        assert entry["title"] == hits[0]["section"]

    def test_empty_sections_are_dropped_everywhere(self):
        md = "## Empty\n\n## Real\n\nbody text\n"
        assert [sp.title for sp in papers.section_boundaries(md)] == ["Real"]
        assert papers.get_section_content(md, 0)["title"] == "Real"
        assert papers.section_at_offset(md, md.index("body"))[1] == "Real"


class TestChainingByIndex:
    def test_search_hit_carries_a_chainable_index(self, corpus):
        hits = cache_search.search("transformers")
        hit = next(h for h in hits if h["canonical_id"] == "dup")
        assert hit["section"] == "Results"
        assert isinstance(hit["section_index"], int)

    def test_index_chains_where_the_title_fails(self, corpus):
        hits = cache_search.search("transformers")
        hit = next(h for h in hits if h["canonical_id"] == "dup")

        by_index = papers.get_section_content(DUPLICATE_TITLES, hit["section_index"])
        by_title = papers.get_section_content(DUPLICATE_TITLES, hit["section"])

        assert "error" not in by_index
        assert "Ambiguous section title" in by_title["error"]

    def test_repeated_titles_get_distinct_indices(self):
        first = papers.section_at_offset(DUPLICATE_TITLES, DUPLICATE_TITLES.index("alpha"))
        second = papers.section_at_offset(DUPLICATE_TITLES, DUPLICATE_TITLES.index("beta"))
        assert first[1] == second[1] == "Results"
        assert first[0] != second[0]

    def test_char_offset_is_returned_for_chaining(self, corpus):
        hits = cache_search.search("transformers")
        hit = next(h for h in hits if h["canonical_id"] == "dup")
        assert isinstance(hit["char_offset"], int)

    @pytest.mark.asyncio
    async def test_end_to_end_through_the_tools(self, corpus):
        result = await search_tools.search_cached_papers("transformers")
        hit = next(h for h in result["results"] if h["canonical_id"] == "dup")

        section = await pipeline_tools.get_paper_section("dup", str(hit["section_index"]))

        assert "error" not in section
        assert section["title"] == "Results"


class TestHeadinglessDocumentsAreFlagged:
    @pytest.mark.asyncio
    async def test_flag_is_false_and_a_note_explains(self, corpus):
        result = await pipeline_tools.get_paper_sections("flat")

        assert result["total_sections"] == 1
        assert result["sections_detected"] is False
        assert "no headings" in result["sections_note"].lower()
        assert "find_in_paper" in result["sections_note"]

    @pytest.mark.asyncio
    async def test_a_real_one_section_paper_is_not_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path)
        md = tmp_path / "manual" / "markdown"
        md.mkdir(parents=True)
        (md / "one.md").write_text("## Only Section\n\nreal body text\n", encoding="utf-8")

        result = await pipeline_tools.get_paper_sections("one")

        assert result["total_sections"] == 1
        assert result["sections_detected"] is True
        assert "sections_note" not in result

    @pytest.mark.asyncio
    async def test_multi_section_paper_is_not_flagged(self, corpus):
        result = await pipeline_tools.get_paper_sections("dup")
        assert result["sections_detected"] is True
        assert "sections_note" not in result

    def test_detection_helper_directly(self):
        assert papers.has_detected_sections("## A\n\nbody\n") is True
        assert papers.has_detected_sections("# A\n\nbody\n") is True
        # H3 alone does not open a section, so it is not detection.
        assert papers.has_detected_sections("### Sub\n\nbody\n") is False
        assert papers.has_detected_sections("plain text only\n") is False

    @pytest.mark.asyncio
    async def test_older_cached_indices_are_recomputed_not_guessed(self, corpus):
        # Indices written before this flag existed have no such key. Absent
        # used to read as "not recorded" and default to True, which is a guess
        # — and the wrong one for exactly the papers that need the warning.
        # Re-parsing costs a file read and a regex pass, so the entry is now
        # treated as stale and the real answer computed.
        await pipeline_tools.get_paper_sections("dup")
        key = papers.sections_key("dup")
        payload = cache.get("manual", "sections", key)
        payload.pop("sections_detected", None)
        cache.put("manual", "sections", key, payload)

        result = await pipeline_tools.get_paper_sections("dup")

        assert result["sections_detected"] is True
        assert "sections_note" not in result
        # Recomputed, not defaulted: the key is back in the cache entry.
        assert cache.get("manual", "sections", key)["sections_detected"] is True

    @pytest.mark.asyncio
    async def test_older_index_on_a_headingless_paper_reports_the_truth(self, corpus, tmp_path):
        # The case the default got wrong. A legacy entry on a paper with no
        # headings must not be reported as "sections detected" — that is the
        # reading ``sections_note`` exists to prevent, and defaulting to True
        # produced it silently.
        md = tmp_path / "manual" / "markdown"
        md.mkdir(parents=True, exist_ok=True)
        (md / "plain.md").write_text("Body text with no headings at all.\n", encoding="utf-8")

        await pipeline_tools.get_paper_sections("plain")
        key = papers.sections_key("plain")
        payload = cache.get("manual", "sections", key)
        payload.pop("sections_detected", None)
        cache.put("manual", "sections", key, payload)

        result = await pipeline_tools.get_paper_sections("plain")

        assert result["sections_detected"] is False
        assert "sections_note" in result
