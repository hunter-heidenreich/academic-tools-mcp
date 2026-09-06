import asyncio
import re
import shlex
from pathlib import Path
from unittest.mock import patch

import pytest

from academic_tools_mcp import cache, papers
from academic_tools_mcp.papers import (
    _DEFAULT_FAST_CONVERT_TIMEOUT,
    _DEFAULT_PDF_CONVERT_TIMEOUT,
    _build_converter_command,
    _build_fast_converter_command,
    _resolve_convert_timeout,
    _resolve_fast_convert_timeout,
    convert_pdf,
    get_section_content,
    parse_sections,
)

# ---------------------------------------------------------------------------
# Fixtures: H2-based document (standard markdown)
# ---------------------------------------------------------------------------

_H2_MARKDOWN = """\
Some preamble text before any heading.

This has multiple lines.

## Introduction

This is the introduction section.

It has multiple paragraphs.

### Background

Some background information here.

### Motivation

Why we did this work.

## Related Work

Previous approaches to the problem.

## Methods

### Architecture

The model architecture is described here.

### Training

Training details go here with specifics.

More training details on a second paragraph.

## Results

We achieved state-of-the-art performance.

## Conclusion

In this paper, we presented our approach.
"""

# ---------------------------------------------------------------------------
# Fixtures: H1-based document (MinerU output style)
# ---------------------------------------------------------------------------

_H1_MARKDOWN = """\
# Attention Is All You Need

Ashish Vaswani, Noam Shazeer

# Abstract

The dominant sequence transduction models are based on complex recurrent neural networks.

# 1 Introduction

Recurrent neural networks have been firmly established as state of the art.

Attention mechanisms have become an integral part of sequence modeling.

# 2 Background

The goal of reducing sequential computation forms the foundation of several approaches.

# 3 Model Architecture

Most competitive neural sequence transduction models have an encoder-decoder structure.

# 3.1 Encoder and Decoder Stacks

The encoder is composed of a stack of N=6 identical layers.

# 3.2 Attention

An attention function maps a query and key-value pairs to an output.

# 4 Why Self-Attention

In this section we compare various aspects of self-attention layers.

# 5 Training

We describe the training regime for our models.

# 5.1 Training Data and Batching

We trained on the WMT 2014 English-German dataset.

# 6 Results

Results on machine translation and other tasks.

# 7 Conclusion

In this work, we presented the Transformer.
"""

_NO_HEADINGS = """\
Just a document with no headings at all.

It has some content but no structure.
"""

_H2_ONLY_MARKDOWN = """\
## First Section

Content of first section.

## Second Section

Content of second section.
"""


# ---------------------------------------------------------------------------
# parse_sections with H2-based documents
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# parse_sections regression: more H3 subsections than H2 sections
# ---------------------------------------------------------------------------


_H2_WITH_MANY_H3S = """\
## Title
Preamble-ish.

## Results
### Sub A
text
### Sub B
text
### Sub C
text
### Sub D
text

## Methods
### Method A
text
### Method B
text
### Method C
text

## References
### Refs 1-10
text
### Refs 11-20
text
### Refs 21-30
text
"""


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


# ---------------------------------------------------------------------------
# convert_pdf cache paths (subprocess path is not exercised here)
# ---------------------------------------------------------------------------


class TestConvertPdfCachePaths:
    """When the markdown is already cached, convert_pdf must never invoke
    the slow subprocess — even if the sections cache is missing or stale.
    """

    @pytest.fixture
    def isolated_cache(self, tmp_path, monkeypatch):
        # Redirect the cache root so each test runs against a clean filesystem
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        return tmp_path

    @pytest.fixture
    def fail_if_subprocess(self, monkeypatch):
        # Any attempt to spawn a subprocess in this test is a bug
        async def _fail(*args, **kwargs):
            raise AssertionError("convert_pdf should not invoke the subprocess on this path")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)

    def _seed_markdown(self, namespace, canonical, body):
        md_path = papers.markdown_path(namespace, canonical)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(body)
        return md_path

    @pytest.mark.asyncio
    async def test_uses_cached_sections_when_checksum_matches(
        self, isolated_cache, fail_if_subprocess
    ):
        ns, canonical = "test", "doc-1"
        md_path = self._seed_markdown(ns, canonical, "## A\n\nx\n\n## B\n\ny\n")
        sections = papers.parse_sections(md_path.read_text())
        cache.put(
            ns,
            "sections",
            papers.sections_key(canonical),
            {
                "sections": sections,
                "markdown_checksum": papers.markdown_checksum(md_path),
            },
        )

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert result["cached"] is True
        assert result["sections"] == sections

    @pytest.mark.asyncio
    async def test_reparses_when_sections_cache_missing(self, isolated_cache, fail_if_subprocess):
        # The bug fix: markdown exists, sections cache missing -> re-parse,
        # do NOT re-run the subprocess (which would also overwrite markdown).
        ns, canonical = "test", "doc-2"
        self._seed_markdown(ns, canonical, "## Intro\n\nhi\n\n## Methods\n\nstuff\n")

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert result["cached"] is True
        titles = [s["title"] for s in result["sections"]]
        assert titles == ["Intro", "Methods"]

        # And the sections cache is now populated for next time
        refreshed = cache.get(ns, "sections", papers.sections_key(canonical))
        assert refreshed is not None
        assert refreshed["sections"] == result["sections"]

    @pytest.mark.asyncio
    async def test_reparses_when_checksum_stale(self, isolated_cache, fail_if_subprocess):
        # Markdown was edited externally so the cached checksum no longer matches.
        ns, canonical = "test", "doc-3"
        self._seed_markdown(ns, canonical, "## Old\n\nold body\n")
        cache.put(
            ns,
            "sections",
            papers.sections_key(canonical),
            {
                "sections": [{"index": 0, "title": "Old", "h3s": [], "approx_tokens": 1}],
                "markdown_checksum": "deadbeef",  # deliberately wrong
            },
        )

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert result["cached"] is True
        # Re-parsed from current markdown, not the stale cache
        assert [s["title"] for s in result["sections"]] == ["Old"]

        refreshed = cache.get(ns, "sections", papers.sections_key(canonical))
        assert refreshed["markdown_checksum"] != "deadbeef"

    @pytest.mark.asyncio
    async def test_reparses_when_checksum_missing(self, isolated_cache, fail_if_subprocess):
        # A sections cache entry written before the checksum field existed
        # (or by any path that didn't persist one) must be treated as stale,
        # not valid — otherwise external edits to the markdown go undetected.
        ns, canonical = "test", "doc-5"
        md_path = self._seed_markdown(ns, canonical, "## Fresh\n\nbody\n")
        cache.put(
            ns,
            "sections",
            papers.sections_key(canonical),
            {
                "sections": [{"index": 0, "title": "Stale", "h3s": [], "approx_tokens": 1}],
                "markdown_checksum": None,
            },
        )

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert [s["title"] for s in result["sections"]] == ["Fresh"]

        refreshed = cache.get(ns, "sections", papers.sections_key(canonical))
        assert refreshed["markdown_checksum"] == papers.markdown_checksum(md_path)

    @pytest.mark.asyncio
    async def test_errors_when_neither_markdown_nor_pdf_exists(self, isolated_cache):
        ns, canonical = "test", "doc-4"
        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert "error" in result
        assert "PDF not found" in result["error"]

    @pytest.mark.asyncio
    async def test_force_refresh_drops_markdown_and_sections(self, isolated_cache):
        # force_refresh must blow away the cached markdown AND the
        # sections cache, so the next call falls through to "PDF not
        # found" (no PDF here) — proving both halves were cleared.
        ns, canonical = "test", "doc-force-refresh"
        md_path = self._seed_markdown(ns, canonical, "## A\n\nbody\n")
        cache.put(
            ns,
            "sections",
            papers.sections_key(canonical),
            {
                "sections": [{"index": 0, "title": "A", "h3s": [], "approx_tokens": 1}],
                "markdown_checksum": papers.markdown_checksum(md_path),
            },
        )

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical, force_refresh=True)
        assert "error" in result
        assert not md_path.exists(), "force_refresh should unlink the markdown"
        assert cache.get(ns, "sections", papers.sections_key(canonical)) is None, (
            "force_refresh should invalidate the sections cache"
        )

    @pytest.mark.asyncio
    async def test_concurrent_callers_reparse_only_once(
        self, isolated_cache, fail_if_subprocess, monkeypatch
    ):
        # Two concurrent callers on the same paper with no sections cache
        # must serialise via the per-paper lock: only the first re-parses,
        # the second sees the freshly written cache entry. Without the lock
        # both would re-parse and race to write.
        ns, canonical = "test", "concurrent-1"
        self._seed_markdown(ns, canonical, "## A\n\nx\n\n## B\n\ny\n")

        # Reset the lock dict so this test starts from a clean slate
        # regardless of test ordering.
        from collections import OrderedDict

        monkeypatch.setattr(papers, "_section_locks", OrderedDict())

        parse_calls = 0
        real_parse = papers.parse_sections

        def counting_parse(markdown):
            nonlocal parse_calls
            parse_calls += 1
            return real_parse(markdown)

        monkeypatch.setattr(papers, "parse_sections", counting_parse)

        results = await asyncio.gather(
            convert_pdf(Path("/nonexistent.pdf"), ns, canonical),
            convert_pdf(Path("/nonexistent.pdf"), ns, canonical),
            convert_pdf(Path("/nonexistent.pdf"), ns, canonical),
        )

        assert all(r.get("cached") is True for r in results)
        titles = [s["title"] for s in results[0]["sections"]]
        assert titles == ["A", "B"]
        assert parse_calls == 1, (
            f"expected exactly one re-parse under the per-paper lock, got {parse_calls}"
        )

    @pytest.mark.asyncio
    async def test_markdown_unlinked_between_exists_and_lock_is_a_miss(
        self, isolated_cache, monkeypatch
    ):
        # TOCTOU regression: convert_pdf checks md_path.exists() *before*
        # acquiring the per-paper lock, then reads inside it. A concurrent
        # unlink (force_refresh, or the download_pdf cascade which doesn't take
        # the lock) in that gap must NOT raise FileNotFoundError — it must be
        # treated as a cache miss and fall through cleanly.
        ns, canonical = "test", "toctou-1"
        md_path = self._seed_markdown(ns, canonical, "## A\n\nbody\n")

        from collections import OrderedDict

        monkeypatch.setattr(papers, "_section_locks", OrderedDict())

        # Hold the paper's lock so the convert_pdf task blocks after its outer
        # exists() check (which sees the file) and before its read.
        lock = papers.sections_lock(ns, canonical)
        await lock.acquire()

        task = asyncio.create_task(convert_pdf(Path("/nonexistent.pdf"), ns, canonical))
        # Let the task run up to the lock acquisition.
        await asyncio.sleep(0)

        # Simulate the unsynchronised cascade delete, then release the lock.
        md_path.unlink()
        lock.release()

        # Pre-fix this raised FileNotFoundError; post-fix it's a clean miss.
        result = await task
        assert "error" in result
        assert "PDF not found" in result["error"]


class TestSectionLocksLRU:
    """The per-paper section lock dict is bounded so a long-running
    session that touches thousands of papers doesn't accumulate Locks
    forever. Eviction is FIFO and skips currently-held locks.
    """

    @pytest.fixture(autouse=True)
    def _reset_locks(self, monkeypatch):
        from collections import OrderedDict

        monkeypatch.setattr(papers, "_section_locks", OrderedDict())

    def test_unbounded_below_cap(self, monkeypatch):
        monkeypatch.setattr(papers, "_SECTION_LOCKS_MAX", 100)
        for i in range(50):
            papers.sections_lock("test", f"paper-{i}")
        assert len(papers._section_locks) == 50

    def test_evicts_oldest_when_cap_exceeded(self, monkeypatch):
        monkeypatch.setattr(papers, "_SECTION_LOCKS_MAX", 5)
        for i in range(10):
            papers.sections_lock("test", f"paper-{i}")
        assert len(papers._section_locks) == 5
        # Newest five survive; oldest five evicted.
        survivors = set(papers._section_locks)
        assert survivors == {("test", f"paper-{i}") for i in range(5, 10)}

    def test_touch_promotes_to_end(self, monkeypatch):
        monkeypatch.setattr(papers, "_SECTION_LOCKS_MAX", 3)
        papers.sections_lock("test", "a")
        papers.sections_lock("test", "b")
        papers.sections_lock("test", "c")
        # Touch "a" so it moves to the end of the LRU order.
        papers.sections_lock("test", "a")
        # Adding "d" should now evict "b" (the new oldest), not "a".
        papers.sections_lock("test", "d")
        keys = list(papers._section_locks.keys())
        assert ("test", "b") not in keys
        assert ("test", "a") in keys

    @pytest.mark.asyncio
    async def test_held_lock_is_not_evicted(self, monkeypatch):
        # If the oldest lock is held when we try to evict, we skip it
        # and evict the next free one instead — dropping a held lock
        # would let a racing caller bypass mutual exclusion.
        monkeypatch.setattr(papers, "_SECTION_LOCKS_MAX", 2)
        held = papers.sections_lock("test", "held")
        await held.acquire()
        try:
            papers.sections_lock("test", "free-1")
            papers.sections_lock("test", "free-2")
            keys = set(papers._section_locks.keys())
            # "held" must still be present; one of the free ones got evicted.
            assert ("test", "held") in keys
        finally:
            held.release()

    @pytest.mark.asyncio
    async def test_all_locks_held_bails_over_cap(self, monkeypatch):
        # When every lock is held, eviction must bail (go over cap) rather
        # than spin — and must do so without dropping a held lock.
        monkeypatch.setattr(papers, "_SECTION_LOCKS_MAX", 2)
        a = papers.sections_lock("test", "a")
        b = papers.sections_lock("test", "b")
        await a.acquire()
        await b.acquire()
        try:
            papers.sections_lock("test", "c")  # over cap, but a/b are held
            keys = set(papers._section_locks.keys())
            assert ("test", "a") in keys
            assert ("test", "b") in keys
            assert ("test", "c") in keys  # added; nothing evictable
        finally:
            a.release()
            b.release()

    def test_returns_same_lock_for_same_key(self):
        lock1 = papers.sections_lock("test", "same")
        lock2 = papers.sections_lock("test", "same")
        assert lock1 is lock2


class TestConvertPdfSubprocessFailures:
    """The subprocess path must turn every failure into an {error, ...} dict;
    nothing should bubble as a raw exception.
    """

    @pytest.fixture
    def isolated_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        return tmp_path

    @pytest.fixture
    def real_pdf(self, tmp_path):
        # convert_pdf needs the PDF to exist before spawning; the bytes don't
        # matter because we mock the subprocess.
        pdf = tmp_path / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")
        return pdf

    @pytest.mark.asyncio
    async def test_spawn_failure_returns_error_dict(self, isolated_cache, real_pdf, monkeypatch):
        async def _spawn_fail(*args, **kwargs):
            raise FileNotFoundError("bash: not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_fail)
        result = await convert_pdf(real_pdf, "test", "spawn-fail-1")
        assert "error" in result
        assert "Could not start" in result["error"]
        assert result["retryable"] is False
        assert "pdf_size_mb" in result

    @pytest.mark.asyncio
    async def test_timeout_kills_process_group_and_returns_error(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        # FakeProc whose communicate() never finishes — exactly the
        # failure mode the timeout exists to bound.
        killed_pgids: list[int] = []

        class HangingProc:
            pid = 424242
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            async def wait(self):
                # Pretend the SIGKILL took effect immediately.
                self.returncode = -9
                return -9

        async def _fake_spawn(*args, **kwargs):
            assert kwargs.get("start_new_session") is True, (
                "convert_pdf must spawn with start_new_session=True so the "
                "whole process tree can be signalled on timeout"
            )
            return HangingProc()

        def _fake_getpgid(pid):
            return pid

        def _fake_killpg(pgid, sig):
            killed_pgids.append(pgid)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        monkeypatch.setattr("academic_tools_mcp.papers.os.getpgid", _fake_getpgid)
        monkeypatch.setattr("academic_tools_mcp.papers.os.killpg", _fake_killpg)
        # Force a tiny timeout via env so the test runs fast.
        monkeypatch.setattr(
            "academic_tools_mcp.papers.config.get",
            lambda key: "0.05" if key == "PDF_CONVERT_TIMEOUT" else None,
        )

        result = await convert_pdf(real_pdf, "test", "timeout-1")

        assert "error" in result
        assert "timed out" in result["error"].lower()
        assert result["retryable"] is False
        assert result["timed_out"] is True
        assert result["timeout_seconds"] == pytest.approx(0.05)
        assert "pdf_size_mb" in result
        assert killed_pgids == [HangingProc.pid], (
            "timeout path must SIGKILL the converter's process group"
        )

    @pytest.mark.asyncio
    async def test_second_caller_gets_busy_while_one_in_flight(self, isolated_cache, monkeypatch):
        # Server runs at most one PDF conversion at a time. The second
        # caller, while another conversion is mid-flight, must get a
        # structured `busy` error — NOT queue, NOT spawn its own
        # subprocess. The first caller's run is unaffected.
        pdf_a = isolated_cache / "a.pdf"
        pdf_a.write_bytes(b"%PDF-1.4 stub a")
        pdf_b = isolated_cache / "b.pdf"
        pdf_b.write_bytes(b"%PDF-1.4 stub b")

        # Reset the global lock + state so we don't inherit anything
        # from another test that ran in this loop.
        monkeypatch.setattr(papers, "_global_convert_lock", asyncio.Lock())
        monkeypatch.setattr(papers, "_current_conversion", None)

        spawn_count = 0
        spawn_started = asyncio.Event()
        release_subprocess = asyncio.Event()

        class HangingProc:
            pid = 313131
            returncode = None

            async def communicate(self):
                # Wait until the test releases us, then return success-ish.
                # We won't actually parse anything because returncode!=0
                # is set below to short-circuit the post-processing.
                await release_subprocess.wait()
                self.returncode = 1
                return b"converter aborted by test", b""

            async def wait(self):
                self.returncode = -9
                return -9

        async def fake_spawn(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            spawn_started.set()
            return HangingProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)

        # First caller — start it as a background task so we can run
        # the second caller while the first one is still in the lock.
        task_a = asyncio.create_task(convert_pdf(pdf_a, "test", "paper-a"))

        # Wait until the first task has actually entered the subprocess
        # block (i.e. has acquired the global lock). spawn_started fires
        # from inside `async with _global_convert_lock`.
        await spawn_started.wait()

        # Second caller — different paper. Must get busy without spawning.
        result_b = await convert_pdf(pdf_b, "test", "paper-b")
        assert result_b.get("busy") is True
        assert result_b.get("retryable") is True
        assert "already in progress" in result_b["error"]
        assert result_b["in_progress"]["canonical"] == "paper-a"
        assert result_b["in_progress"]["namespace"] == "test"
        assert result_b["in_progress"]["elapsed_seconds"] >= 0
        assert "pdf_size_mb" in result_b

        # Third caller, same paper as the in-flight one — still busy.
        # We deliberately do not collapse same-paper requests; the second
        # caller could observe a half-written cache, so making them retry
        # after the first one finishes is the safe answer.
        result_a2 = await convert_pdf(pdf_a, "test", "paper-a")
        assert result_a2.get("busy") is True

        # Only the first caller ever spawned a subprocess.
        assert spawn_count == 1, (
            f"expected exactly one subprocess spawn under the global "
            f"convert lock, got {spawn_count}"
        )

        # Let the first caller finish so the test doesn't leak the task.
        release_subprocess.set()
        result_a = await task_a
        assert "error" in result_a  # converter exited 1 by design

        # Lock is released — a fresh caller can now proceed (would spawn
        # again if we let it). Just confirm the gate is open.
        assert papers._global_convert_lock.locked() is False
        assert papers._current_conversion is None

    @pytest.mark.asyncio
    async def test_binary_output_does_not_crash(self, isolated_cache, real_pdf, monkeypatch):
        # A converter that crashes can dump binary garbage on stdout.
        # The non-zero exit handler used to call .decode() with strict UTF-8
        # and raise UnicodeDecodeError on those bytes.
        binary_garbage = b"\xff\xfe\xfd boom \xc3\x28 invalid utf-8 \x00\x01"

        class FakeProc:
            returncode = 1

            async def communicate(self):
                return binary_garbage, b""

        async def _fake_spawn(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "binary-out-1")
        assert "error" in result
        assert "exit 1" in result["error"]
        assert result["retryable"] is False


class TestConvertPdfTempDirCleanup:
    """The extraction dir must be removed on every exit path — success *and*
    all four failure paths — so failed conversions don't leak it. The dir is
    a private ``tempfile.mkdtemp`` created by ``papers._make_extraction_dir``;
    these tests monkeypatch that helper to a known path so the fake subprocess
    can populate it and the assertions can check it was cleaned up.
    """

    @pytest.fixture
    def isolated_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        return tmp_path

    @pytest.fixture
    def real_pdf(self, tmp_path):
        pdf = tmp_path / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")
        return pdf

    @pytest.fixture
    def extract_dir(self, tmp_path, monkeypatch):
        # Replace the mkdtemp-based helper with a deterministic dir, populated
        # with a non-.md leftover so the assertion proves the finally removes
        # real content rather than just an absent path.
        d = tmp_path / "extract"

        def _make(canonical):
            d.mkdir(parents=True, exist_ok=True)
            (d / "images").mkdir(exist_ok=True)
            (d / "images" / "fig.png").write_bytes(b"\x89PNG fake")
            return d

        monkeypatch.setattr(papers, "_make_extraction_dir", _make)
        return d

    @pytest.mark.asyncio
    async def test_cleanup_on_spawn_failure(
        self, isolated_cache, real_pdf, extract_dir, monkeypatch
    ):
        async def _spawn_fail(*args, **kwargs):
            raise FileNotFoundError("bash: not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_fail)
        result = await convert_pdf(real_pdf, "test", "tmp-spawn-fail")
        assert "error" in result
        assert not extract_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_timeout(self, isolated_cache, real_pdf, extract_dir, monkeypatch):
        class HangingProc:
            pid = 525252
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            async def wait(self):
                self.returncode = -9
                return -9

        async def _fake_spawn(*args, **kwargs):
            return HangingProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        monkeypatch.setattr("academic_tools_mcp.papers.os.getpgid", lambda pid: pid)
        monkeypatch.setattr("academic_tools_mcp.papers.os.killpg", lambda pgid, sig: None)
        monkeypatch.setattr(
            "academic_tools_mcp.papers.config.get",
            lambda key: "0.05" if key == "PDF_CONVERT_TIMEOUT" else None,
        )
        result = await convert_pdf(real_pdf, "test", "tmp-timeout")
        assert result.get("timed_out") is True
        assert not extract_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_nonzero_exit(
        self, isolated_cache, real_pdf, extract_dir, monkeypatch
    ):
        class FakeProc:
            returncode = 1

            async def communicate(self):
                return b"converter blew up", b""

        async def _fake_spawn(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "tmp-nonzero")
        assert "exit 1" in result["error"]
        assert not extract_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_no_markdown(self, isolated_cache, real_pdf, extract_dir, monkeypatch):
        # Converter "succeeds" (exit 0) but emits no .md file (only the
        # leftover the fixture seeded), so the glob finds nothing.
        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def _fake_spawn(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "tmp-no-md")
        assert "no markdown output" in result["error"]
        assert not extract_dir.exists()

    @pytest.mark.asyncio
    async def test_cleanup_on_success(self, isolated_cache, real_pdf, extract_dir, monkeypatch):
        # A converter that "produces" markdown: write a .md into the extraction
        # dir the moment convert_pdf spawns it, mimicking a real run.
        async def _fake_spawn(*args, **kwargs):
            (extract_dir / f"{real_pdf.stem}.md").write_text("## Intro\n\nHello world.")

            class FakeProc:
                returncode = 0

                async def communicate(self):
                    return b"", b""

            return FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "tmp-success")
        assert result.get("cached") is False
        assert result["sections"]
        assert not extract_dir.exists()


# ---------------------------------------------------------------------------
# _build_converter_command
# ---------------------------------------------------------------------------


class TestBuildConverterCommand:
    """Tests for the configurable PDF converter command builder."""

    def _env(self, **overrides):
        """Return a config.get mock that returns overrides or None."""

        def _get(key):
            return overrides.get(key)

        return patch("academic_tools_mcp.papers.config.get", side_effect=_get)

    def test_default_is_mineru(self):
        with self._env():
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        # Paths with no shell-special chars shlex.quote to themselves (no quotes).
        assert cmd == "mineru -p /a/b.pdf -o /tmp/out"

    def test_named_marker_backend(self):
        with self._env(PDF_CONVERTER="marker"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert cmd == "marker_single /a/b.pdf --output_dir /tmp/out"

    def test_custom_command_template(self):
        # Custom templates use BARE placeholders (values arrive shell-quoted).
        custom = "my-tool convert --src {input} --dst {output_dir}"
        with self._env(PDF_CONVERTER=custom):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert cmd == "my-tool convert --src /a/b.pdf --dst /tmp/out"

    def test_venv_activation(self):
        with self._env(PDF_CONVERTER="mineru", PDF_CONVERTER_VENV="~/.venvs/mineru"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert "source" in cmd
        assert ".venvs/mineru/bin/activate" in cmd
        assert cmd.endswith("mineru -p /a/b.pdf -o /tmp/out")

    def test_no_venv_by_default(self):
        with self._env(PDF_CONVERTER="marker"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert "source" not in cmd
        assert "activate" not in cmd


# ---------------------------------------------------------------------------
# Shell-quoting safety (command injection hardening)
# ---------------------------------------------------------------------------


class TestConverterCommandQuoting:
    """A canonical-derived path containing shell metacharacters must be
    shell-quoted before it reaches ``bash -c``, so it can't be interpreted
    as a command. Manual double-quoting around ``{input}`` did not protect
    against ``$()`` / backticks / embedded quotes.
    """

    def _env(self, **overrides):
        def _get(key):
            return overrides.get(key)

        return patch("academic_tools_mcp.papers.config.get", side_effect=_get)

    # A path that, unquoted (or merely double-quoted), would run a command
    # and break out of the surrounding quotes.
    HOSTILE = Path('/cache/pdfs/x"$(touch pwned)`id`.pdf')

    def test_full_command_quotes_hostile_input(self):
        with self._env():
            cmd = _build_converter_command(self.HOSTILE, Path("/tmp/out"))
        # The path survives as exactly one shell token — so bash treats it as a
        # single literal argument and never command-substitutes the $()/`id`.
        assert str(self.HOSTILE) in shlex.split(cmd)
        # The quoted form is what's embedded (the $() is inert inside it).
        assert shlex.quote(str(self.HOSTILE)) in cmd

    def test_full_command_quotes_output_dir(self):
        with self._env():
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/o$(x)"))
        assert "/tmp/o$(x)" in shlex.split(cmd)
        assert shlex.quote("/tmp/o$(x)") in cmd

    def test_venv_path_is_quoted(self):
        with self._env(PDF_CONVERTER="mineru", PDF_CONVERTER_VENV="/opt/v env"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        # The space-containing venv path is quoted so `source` gets one arg.
        assert shlex.quote("/opt/v env/bin/activate") in cmd

    def test_fast_command_quotes_hostile_input(self):
        with self._env():
            cmd = _build_fast_converter_command(self.HOSTILE)
        assert str(self.HOSTILE) in shlex.split(cmd)
        assert shlex.quote(str(self.HOSTILE)) in cmd


class TestSafeStem:
    """``safe_stem`` maps a canonical id to a filesystem/shell-safe stem."""

    def test_strips_shell_metacharacters(self):
        stem = papers.safe_stem('x"$(touch pwned)`id`;rm -rf /|y')
        # Unsafe characters are percent-encoded rather than collapsed, so the
        # stem stays injective — but nothing shell-significant survives.
        assert re.fullmatch(r"[A-Za-z0-9._%-]+", stem)
        for bad in ('"', "$", "(", ")", "`", ";", "|", " ", "/"):
            assert bad not in stem

    def test_is_injective_for_identifiers_that_used_to_collide(self):
        # "a b" and "a_b" both mapped to "a_b", so importing one silently
        # overwrote the other's cached PDF.
        assert papers.safe_stem("a b") != papers.safe_stem("a_b")
        assert papers.safe_stem("some:label") != papers.safe_stem("some_label")

    def test_percent_is_not_double_encoded(self):
        assert papers.safe_stem("x%y") == "x%25y"

    def test_encoding_its_own_output_would_double_encode(self):
        # safe_stem is deliberately NOT idempotent — "%" is itself encoded.
        # This is why the migration sweep gates on _needs_stem_migration
        # rather than comparing safe_stem(stem) to stem.
        assert papers.safe_stem("a%20b") == "a%2520b"

    def test_needs_stem_migration_only_for_legacy_names(self):
        assert papers._needs_stem_migration("10.1016_s1-6(03)02831-9") is True
        assert papers._needs_stem_migration("my paper") is True
        # Already-safe and already-migrated names are left alone.
        assert papers._needs_stem_migration("2301.00001") is False
        assert papers._needs_stem_migration("10.1101_2021.01.01.123") is False
        assert papers._needs_stem_migration("a%20b") is False
        # "~" is unreserved, so `quote` passes it through: it is safe_stem
        # output, not a legacy name. Read as legacy, the sweep re-encodes the
        # "%" beside it and orphans the file it just renamed.
        assert papers._needs_stem_migration("notes~draft%202024") is False

    def test_safe_stem_output_is_never_seen_as_legacy(self):
        """Invariant: the migration gate accepts everything safe_stem emits.

        Otherwise startup renames a correctly-written file to a stem the path
        builder never produces, and the paper reports "not converted yet".
        """
        for canonical in [
            "notes~draft 2024",
            "10.1038/s41586-024-00001-1",
            "hep-th/9901001v2",
            'x"$(touch pwned)`id`;rm -rf /|y',
            "Grüße/Übung 1",
        ]:
            stem = papers.safe_stem(canonical)
            assert papers._needs_stem_migration(stem) is False

    def test_keeps_normal_doi_and_arxiv_ids(self):
        # Normal identifiers are unchanged except for the legacy / -> _ map,
        # so existing cache filenames don't churn.
        assert papers.safe_stem("10.1101/2021.01.01.123") == "10.1101_2021.01.01.123"
        assert papers.safe_stem("2301.00001v2") == "2301.00001v2"
        assert papers.safe_stem("hep-th/9901001") == "hep-th_9901001"


# ---------------------------------------------------------------------------
# _resolve_convert_timeout
# ---------------------------------------------------------------------------


class TestResolveConvertTimeout:
    """PDF_CONVERT_TIMEOUT parsing — bad input must never raise."""

    def _env(self, value):
        def _get(key):
            return value if key == "PDF_CONVERT_TIMEOUT" else None

        return patch("academic_tools_mcp.papers.config.get", side_effect=_get)

    def test_unset_uses_default(self):
        with self._env(None):
            assert _resolve_convert_timeout() == _DEFAULT_PDF_CONVERT_TIMEOUT

    def test_explicit_seconds(self):
        with self._env("600"):
            assert _resolve_convert_timeout() == 600.0

    def test_float_seconds(self):
        with self._env("90.5"):
            assert _resolve_convert_timeout() == 90.5

    def test_zero_disables(self):
        with self._env("0"):
            assert _resolve_convert_timeout() is None

    def test_negative_disables(self):
        with self._env("-1"):
            assert _resolve_convert_timeout() is None

    def test_word_disables(self):
        for word in ("none", "off", "disabled", "NONE", "Off"):
            with self._env(word):
                assert _resolve_convert_timeout() is None, word

    def test_garbage_falls_back_to_default(self):
        with self._env("not-a-number"):
            assert _resolve_convert_timeout() == _DEFAULT_PDF_CONVERT_TIMEOUT

    def test_non_finite_falls_back_to_default(self):
        """``float("nan")`` is neither ``> 0`` nor ``<= 0``.

        It slipped past both branches and reached ``asyncio.wait_for`` as a
        deadline no elapsed time can ever satisfy.
        """
        for word in ("nan", "inf", "-inf"):
            with self._env(word):
                assert _resolve_convert_timeout() == _DEFAULT_PDF_CONVERT_TIMEOUT, word


# ---------------------------------------------------------------------------
# _build_fast_converter_command
# ---------------------------------------------------------------------------


class TestBuildFastConverterCommand:
    """The fast extractor command builder mirrors the full one but emits to
    stdout and has no output-dir. {python} expands to the server interpreter.
    """

    def _env(self, **overrides):
        def _get(key):
            return overrides.get(key)

        return patch("academic_tools_mcp.papers.config.get", side_effect=_get)

    def test_default_is_pdftotext(self):
        with self._env():
            cmd = _build_fast_converter_command(Path("/a/b.pdf"))
        assert cmd == "pdftotext -layout /a/b.pdf -"

    def test_named_pymupdf_uses_server_interpreter(self):
        import shlex
        import sys

        with self._env(PDF_FAST_CONVERTER="pymupdf"):
            cmd = _build_fast_converter_command(Path("/a/b.pdf"))
        assert cmd == (
            f"{shlex.quote(sys.executable)} -m academic_tools_mcp._fast_extract /a/b.pdf"
        )

    def test_custom_command_template(self):
        # Bare placeholder per the shell-quoted-substitution contract.
        custom = "my-extractor --src {input}"
        with self._env(PDF_FAST_CONVERTER=custom):
            cmd = _build_fast_converter_command(Path("/a/b.pdf"))
        assert cmd == "my-extractor --src /a/b.pdf"


# ---------------------------------------------------------------------------
# _resolve_fast_convert_timeout
# ---------------------------------------------------------------------------


class TestResolveFastConvertTimeout:
    """PDF_FAST_CONVERT_TIMEOUT parsing — same rules as the full timeout."""

    def _env(self, value):
        def _get(key):
            return value if key == "PDF_FAST_CONVERT_TIMEOUT" else None

        return patch("academic_tools_mcp.papers.config.get", side_effect=_get)

    def test_unset_uses_default(self):
        with self._env(None):
            assert _resolve_fast_convert_timeout() == _DEFAULT_FAST_CONVERT_TIMEOUT

    def test_explicit_seconds(self):
        with self._env("30"):
            assert _resolve_fast_convert_timeout() == 30.0

    def test_word_disables(self):
        with self._env("none"):
            assert _resolve_fast_convert_timeout() is None

    def test_garbage_falls_back_to_default(self):
        with self._env("nope"):
            assert _resolve_fast_convert_timeout() == _DEFAULT_FAST_CONVERT_TIMEOUT


# ---------------------------------------------------------------------------
# convert_pdf(mode="fast")
# ---------------------------------------------------------------------------


class TestConvertPdfFastMode:
    """Fast mode: lightweight stdout-capturing extraction that runs outside
    the global conversion lock and tags its output conversion_mode='fast'.
    """

    @pytest.fixture
    def isolated_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        return tmp_path

    @pytest.fixture
    def real_pdf(self, tmp_path):
        pdf = tmp_path / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")
        return pdf

    @staticmethod
    def _stdout_proc(text: bytes, returncode: int = 0, stderr: bytes = b""):
        class FakeProc:
            pid = 909090

            def __init__(self):
                self.returncode = returncode

            async def communicate(self):
                return text, stderr

            async def wait(self):
                self.returncode = -9
                return -9

        return FakeProc

    @pytest.mark.asyncio
    async def test_fast_extraction_caches_markdown_and_sections(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        proc_cls = self._stdout_proc(b"## Intro\n\nHello from pdftotext.")

        async def _fake_spawn(*args, **kwargs):
            return proc_cls()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "fast-1", mode="fast")

        assert result["cached"] is False
        assert result["conversion_mode"] == "fast"
        assert result["sections"]

        # Markdown landed in the cache and the section index carries the
        # checksum plus the conversion_mode tag.
        md_path = papers.markdown_path("test", "fast-1")
        assert md_path.exists()
        cached = cache.get("test", "sections", papers.sections_key("fast-1"))
        assert cached["conversion_mode"] == "fast"
        assert cached["markdown_checksum"] == papers.markdown_checksum(md_path)

    @pytest.mark.asyncio
    async def test_fast_mode_runs_outside_global_lock(self, isolated_cache, real_pdf, monkeypatch):
        # Hold the global convert lock — a full conversion would get `busy`.
        # Fast mode must succeed anyway because it never takes the lock.
        monkeypatch.setattr(papers, "_global_convert_lock", asyncio.Lock())
        monkeypatch.setattr(papers, "_current_conversion", None)
        await papers._global_convert_lock.acquire()
        try:
            proc_cls = self._stdout_proc(b"Plain extracted text.")

            async def _fake_spawn(*args, **kwargs):
                return proc_cls()

            monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
            result = await convert_pdf(real_pdf, "test", "fast-nolock", mode="fast")

            assert result.get("busy") is not True
            assert result["conversion_mode"] == "fast"
            assert result["cached"] is False
        finally:
            papers._global_convert_lock.release()

    @pytest.mark.asyncio
    async def test_fast_mode_cached_markdown_skips_subprocess(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        # Seed the markdown cache, then assert the subprocess is never spawned.
        md_path = papers.markdown_path("test", "fast-cached")
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("## Cached\n\nAlready converted.")

        async def _fail(*args, **kwargs):
            raise AssertionError("fast mode must not spawn when markdown is cached")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)
        result = await convert_pdf(real_pdf, "test", "fast-cached", mode="fast")
        assert result["cached"] is True
        assert result["sections"]

    @pytest.mark.asyncio
    async def test_fast_convert_cached_preserves_full_conversion_mode(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        # _convert_fast's cached re-check fires when a racing writer creates
        # the markdown after convert_pdf's top-level cache miss. In that window
        # it must NOT relabel a previously FULL-converted paper as degraded
        # "fast". Call _convert_fast directly to exercise exactly that branch.
        ns, canonical = "test", "fast-preserve-full"
        md_path = papers.markdown_path(ns, canonical)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("## Intro\n\nFull-quality body.")
        cache.put(
            ns,
            "sections",
            papers.sections_key(canonical),
            {
                "sections": papers.parse_sections(md_path.read_text()),
                "markdown_checksum": papers.markdown_checksum(md_path),
                "conversion_mode": "full",
            },
        )

        async def _fail(*args, **kwargs):
            raise AssertionError("must not spawn when markdown is cached")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)
        result = await papers._convert_fast(real_pdf, ns, canonical, 0.1)
        assert result["cached"] is True
        assert result["conversion_mode"] == "full"
        # And the recorded mode in the sections cache stays "full".
        cached = cache.get(ns, "sections", papers.sections_key(canonical))
        assert cached["conversion_mode"] == "full"

    @pytest.mark.asyncio
    async def test_fast_mode_nonzero_exit_returns_error(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        proc_cls = self._stdout_proc(b"", returncode=2, stderr=b"pdftotext: boom")

        async def _fake_spawn(*args, **kwargs):
            return proc_cls()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "fast-fail", mode="fast")
        assert "error" in result
        assert "exit 2" in result["error"]
        assert result["retryable"] is False
        assert result["conversion_mode"] == "fast"

    @pytest.mark.asyncio
    async def test_fast_mode_empty_output_returns_error(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        # Exit 0 but whitespace-only stdout (e.g. a scanned image-only PDF).
        proc_cls = self._stdout_proc(b"   \n\f\n  ", returncode=0)

        async def _fake_spawn(*args, **kwargs):
            return proc_cls()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        result = await convert_pdf(real_pdf, "test", "fast-empty", mode="fast")
        assert "error" in result
        assert "no text" in result["error"]
        assert result["conversion_mode"] == "fast"
        # Nothing should have been cached.
        assert not papers.markdown_path("test", "fast-empty").exists()

    @pytest.mark.asyncio
    async def test_fast_mode_spawn_failure_returns_error(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        async def _spawn_fail(*args, **kwargs):
            raise FileNotFoundError("pdftotext: not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_fail)
        result = await convert_pdf(real_pdf, "test", "fast-spawn", mode="fast")
        assert "error" in result
        assert "Could not start" in result["error"]
        assert result["retryable"] is False
        assert result["conversion_mode"] == "fast"

    @pytest.mark.asyncio
    async def test_fast_mode_timeout_kills_and_returns_error(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        killed_pgids: list[int] = []

        class HangingProc:
            pid = 717171
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            async def wait(self):
                self.returncode = -9
                return -9

        async def _fake_spawn(*args, **kwargs):
            assert kwargs.get("start_new_session") is True
            return HangingProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        monkeypatch.setattr("academic_tools_mcp.papers.os.getpgid", lambda pid: pid)
        monkeypatch.setattr(
            "academic_tools_mcp.papers.os.killpg",
            lambda pgid, sig: killed_pgids.append(pgid),
        )
        monkeypatch.setattr(
            "academic_tools_mcp.papers.config.get",
            lambda key: "0.05" if key == "PDF_FAST_CONVERT_TIMEOUT" else None,
        )
        result = await convert_pdf(real_pdf, "test", "fast-timeout", mode="fast")
        assert result.get("timed_out") is True
        assert result["conversion_mode"] == "fast"
        assert result["retryable"] is False
        assert killed_pgids == [HangingProc.pid]


class TestMigrateLegacyStems:
    """Renames cached files written under the pre-``safe_stem`` filename rules.

    Without this a DOI carrying parentheses or a manual label with a space
    would be silently orphaned by the new scheme: the paper reports "not
    converted yet" and re-runs a conversion that can take tens of minutes.
    """

    def _seed(self, tmp_path, namespace, entity, names):
        d = tmp_path / namespace / entity
        d.mkdir(parents=True)
        suffix = ".pdf" if entity == "pdfs" else ".md"
        for n in names:
            (d / (n + suffix)).write_text("x")
        return d

    def test_renames_legacy_names(self, tmp_path):
        d = self._seed(tmp_path, "manual", "markdown", ["10.1016_s0304-3975(03)00229-9"])

        moved = papers.migrate_legacy_stems()

        assert moved == 1
        assert (d / "10.1016_s0304-3975%2803%2900229-9.md").exists()
        assert not (d / "10.1016_s0304-3975(03)00229-9.md").exists()

    def test_leaves_ordinary_identifiers_alone(self, tmp_path):
        names = ["2301.00001", "10.1101_2021.01.01.123", "hep-th_9901001", "P16-1160"]
        d = self._seed(tmp_path, "arxiv", "pdfs", names)

        assert papers.migrate_legacy_stems() == 0
        assert sorted(p.stem for p in d.iterdir()) == sorted(names)

    def test_is_idempotent(self, tmp_path):
        self._seed(tmp_path, "manual", "pdfs", ["my paper"])

        first = papers.migrate_legacy_stems()
        second = papers.migrate_legacy_stems()

        assert (first, second) == (1, 0), "a second sweep must not re-encode"
        assert (tmp_path / "manual" / "pdfs" / "my%20paper.pdf").exists()

    def test_does_not_clobber_an_existing_target(self, tmp_path):
        d = self._seed(tmp_path, "manual", "pdfs", ["a b", "a%20b"])

        assert papers.migrate_legacy_stems() == 0, "must not overwrite a real file"
        assert (d / "a b.pdf").exists() and (d / "a%20b.pdf").exists()

    def test_missing_cache_root_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "does-not-exist")
        assert papers.migrate_legacy_stems() == 0

    def test_ignores_unrelated_entities(self, tmp_path):
        self._seed(tmp_path, "openalex", "works", ["some(thing)"])
        assert papers.migrate_legacy_stems() == 0


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
