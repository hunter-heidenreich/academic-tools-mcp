import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from academic_tools_mcp import cache, papers
from academic_tools_mcp.papers import (
    _build_converter_command,
    _detect_heading_levels,
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
# _detect_heading_levels
# ---------------------------------------------------------------------------


class TestDetectHeadingLevels:
    def test_h2_document(self):
        lines = _H2_MARKDOWN.split("\n")
        section_level, sub_level = _detect_heading_levels(lines)
        assert section_level == 2
        assert sub_level == 3

    def test_h1_document(self):
        lines = _H1_MARKDOWN.split("\n")
        section_level, sub_level = _detect_heading_levels(lines)
        assert section_level == 1
        assert sub_level == 2

    def test_no_headings_defaults(self):
        lines = _NO_HEADINGS.split("\n")
        section_level, sub_level = _detect_heading_levels(lines)
        assert section_level == 2
        assert sub_level == 3

    def test_empty_defaults(self):
        section_level, sub_level = _detect_heading_levels([])
        assert section_level == 2
        assert sub_level == 3


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
            _H2_MARKDOWN, "Methods",
            offset=first["next_offset"],
            max_chars=20,
        )
        assert second["offset"] == 20
        full = get_section_content(_H2_MARKDOWN, "Methods")
        assert full["content"].startswith(first["content"] + second["content"])

    def test_offset_at_total_chars_returns_empty_no_more(self):
        full = get_section_content(_H2_MARKDOWN, "Methods")
        end = get_section_content(
            _H2_MARKDOWN, "Methods",
            offset=full["total_chars"],
        )
        assert end["chars_returned"] == 0
        assert end["content"] == ""
        assert end["has_more"] is False
        assert end["next_offset"] is None

    def test_offset_beyond_section_errors(self):
        full = get_section_content(_H2_MARKDOWN, "Methods")
        result = get_section_content(
            _H2_MARKDOWN, "Methods",
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
        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
        return tmp_path

    @pytest.fixture
    def fail_if_subprocess(self, monkeypatch):
        # Any attempt to spawn a subprocess in this test is a bug
        async def _fail(*args, **kwargs):
            raise AssertionError("convert_pdf should not invoke the subprocess on this path")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail)

    def _seed_markdown(self, namespace, canonical, body):
        md_path = papers._markdown_path(namespace, canonical)
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
        cache.put(ns, "sections", papers._sections_key(canonical), {
            "sections": sections,
            "markdown_checksum": papers._markdown_checksum(md_path),
        })

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert result["cached"] is True
        assert result["sections"] == sections

    @pytest.mark.asyncio
    async def test_reparses_when_sections_cache_missing(
        self, isolated_cache, fail_if_subprocess
    ):
        # The bug fix: markdown exists, sections cache missing -> re-parse,
        # do NOT re-run the subprocess (which would also overwrite markdown).
        ns, canonical = "test", "doc-2"
        self._seed_markdown(ns, canonical, "## Intro\n\nhi\n\n## Methods\n\nstuff\n")

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert result["cached"] is True
        titles = [s["title"] for s in result["sections"]]
        assert titles == ["Intro", "Methods"]

        # And the sections cache is now populated for next time
        refreshed = cache.get(ns, "sections", papers._sections_key(canonical))
        assert refreshed is not None
        assert refreshed["sections"] == result["sections"]

    @pytest.mark.asyncio
    async def test_reparses_when_checksum_stale(
        self, isolated_cache, fail_if_subprocess
    ):
        # Markdown was edited externally so the cached checksum no longer matches.
        ns, canonical = "test", "doc-3"
        self._seed_markdown(ns, canonical, "## Old\n\nold body\n")
        cache.put(ns, "sections", papers._sections_key(canonical), {
            "sections": [{"index": 0, "title": "Old", "h3s": [], "approx_tokens": 1}],
            "markdown_checksum": "deadbeef",  # deliberately wrong
        })

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert result["cached"] is True
        # Re-parsed from current markdown, not the stale cache
        assert [s["title"] for s in result["sections"]] == ["Old"]

        refreshed = cache.get(ns, "sections", papers._sections_key(canonical))
        assert refreshed["markdown_checksum"] != "deadbeef"

    @pytest.mark.asyncio
    async def test_reparses_when_checksum_missing(
        self, isolated_cache, fail_if_subprocess
    ):
        # A sections cache entry written before the checksum field existed
        # (or by any path that didn't persist one) must be treated as stale,
        # not valid — otherwise external edits to the markdown go undetected.
        ns, canonical = "test", "doc-5"
        md_path = self._seed_markdown(ns, canonical, "## Fresh\n\nbody\n")
        cache.put(ns, "sections", papers._sections_key(canonical), {
            "sections": [{"index": 0, "title": "Stale", "h3s": [], "approx_tokens": 1}],
            "markdown_checksum": None,
        })

        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert [s["title"] for s in result["sections"]] == ["Fresh"]

        refreshed = cache.get(ns, "sections", papers._sections_key(canonical))
        assert refreshed["markdown_checksum"] == papers._markdown_checksum(md_path)

    @pytest.mark.asyncio
    async def test_errors_when_neither_markdown_nor_pdf_exists(self, isolated_cache):
        ns, canonical = "test", "doc-4"
        result = await convert_pdf(Path("/nonexistent.pdf"), ns, canonical)
        assert "error" in result
        assert "PDF not found" in result["error"]


class TestConvertPdfSubprocessFailures:
    """The subprocess path must turn every failure into an {error, ...} dict;
    nothing should bubble as a raw exception.
    """

    @pytest.fixture
    def isolated_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / "cache")
        return tmp_path

    @pytest.fixture
    def real_pdf(self, tmp_path):
        # convert_pdf needs the PDF to exist before spawning; the bytes don't
        # matter because we mock the subprocess.
        pdf = tmp_path / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4 stub")
        return pdf

    @pytest.mark.asyncio
    async def test_spawn_failure_returns_error_dict(
        self, isolated_cache, real_pdf, monkeypatch
    ):
        async def _spawn_fail(*args, **kwargs):
            raise FileNotFoundError("bash: not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn_fail)
        result = await convert_pdf(real_pdf, "test", "spawn-fail-1")
        assert "error" in result
        assert "Could not start" in result["error"]
        assert result["retryable"] is False
        assert "pdf_size_mb" in result

    @pytest.mark.asyncio
    async def test_binary_output_does_not_crash(
        self, isolated_cache, real_pdf, monkeypatch
    ):
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
        assert cmd == 'mineru -p "/a/b.pdf" -o "/tmp/out"'

    def test_named_marker_backend(self):
        with self._env(PDF_CONVERTER="marker"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert cmd == 'marker_single "/a/b.pdf" --output_dir "/tmp/out"'

    def test_custom_command_template(self):
        custom = 'my-tool convert --src "{input}" --dst "{output_dir}"'
        with self._env(PDF_CONVERTER=custom):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert cmd == 'my-tool convert --src "/a/b.pdf" --dst "/tmp/out"'

    def test_venv_activation(self):
        with self._env(PDF_CONVERTER="mineru", PDF_CONVERTER_VENV="~/.venvs/mineru"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert 'source' in cmd
        assert '.venvs/mineru/bin/activate' in cmd
        assert cmd.endswith('mineru -p "/a/b.pdf" -o "/tmp/out"')

    def test_no_venv_by_default(self):
        with self._env(PDF_CONVERTER="marker"):
            cmd = _build_converter_command(Path("/a/b.pdf"), Path("/tmp/out"))
        assert "source" not in cmd
        assert "activate" not in cmd
