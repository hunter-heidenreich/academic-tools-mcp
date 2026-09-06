"""Tests for the BM25 search over cached markdown files."""

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from academic_tools_mcp import cache, cache_search, manual, papers, server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_cache():
    """The per-test cache root, already redirected by ``conftest``.

    A second redirect here would only move the corpus somewhere the other
    suites don't look; ``_isolate_cache_root`` is autouse and points
    ``cache._CACHE_ROOT`` at this test's ``tmp_path``, which is what
    ``cache_search`` reads at call time.
    """
    return cache._CACHE_ROOT


def _raise_oserror(*args, **kwargs):
    """Stand-in for a stat/unlink that fails."""
    raise OSError("nope")


def _seed_markdown(root, namespace: str, filename_stem: str, body: str):
    """Write a markdown file under <root>/<namespace>/markdown/<stem>.md."""
    md_dir = root / namespace / "markdown"
    md_dir.mkdir(parents=True, exist_ok=True)
    path = md_dir / f"{filename_stem}.md"
    path.write_text(body)
    return path


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------


class TestContentTokens:
    def test_lowercases_and_drops_stopwords(self):
        # "is" and "you" are stopwords; "all" is deliberately NOT a
        # stopword (it's content-bearing in academic prose).
        assert cache_search._content_tokens("Attention Is All You Need") == {
            "attention",
            "all",
            "need",
        }

    def test_drops_punctuation(self):
        # Brackets, parens, commas all split tokens cleanly. Trailing
        # period on "al." gets stripped because the regex requires the
        # last char of a multi-char token to be alphanumeric — "al"
        # comes back without it.
        assert cache_search._content_tokens("Vaswani et al. (2017), [1]") == {
            "vaswani",
            "et",
            "al",
            "2017",
        }

    def test_preserves_intra_word_hyphens(self):
        # Domain terms with hyphens must survive as single tokens —
        # otherwise "self-attention" can't be queried as a phrase.
        toks = cache_search._content_tokens("self-attention and cross-attention")
        assert "self-attention" in toks
        assert "cross-attention" in toks

    def test_preserves_intra_word_dots(self):
        # Version strings and acronyms with dots stay intact.
        assert "bm25" in cache_search._content_tokens("BM25 ranks documents")
        assert "v1.5" in cache_search._content_tokens("model v1.5 fine-tuned")

    def test_drops_stopwords(self):
        # The classic stopwords are gone but content words survive.
        toks = cache_search._content_tokens("the model is trained on a corpus of papers")
        for stop in ("the", "is", "on", "a", "of"):
            assert stop not in toks
        assert "model" in toks and "trained" in toks and "corpus" in toks

    def test_drops_single_char_tokens(self):
        # "x" alone is noise; "x86" is content.
        toks = cache_search._content_tokens("we run x and y on x86 hardware")
        assert "x" not in toks
        assert "y" not in toks
        assert "x86" in toks

    def test_normalize_folds_diacritics(self):
        # Without normalize the diacritic splits the token (the regex
        # only keeps [a-z0-9-.] runs), so "Gutiérrez" → ["guti", "rrez"].
        assert cache_search._content_tokens("Gutiérrez") == {"guti", "rrez"}
        # With normalize it folds to a single ASCII token.
        assert cache_search._content_tokens("Gutiérrez", normalize=True) == {"gutierrez"}


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------


class TestExtractTitle:
    def test_returns_first_h1(self):
        md = "# Attention Is All You Need\n\n## Abstract\n\nbody\n"
        assert cache_search._extract_title(md) == "Attention Is All You Need"

    def test_returns_first_h2_when_no_h1(self):
        # MinerU output often starts at H2 because the converter uses
        # H1 only for the parent doc; we accept either as the title.
        md = "## Title here\n\n## Section\n\nbody\n"
        assert cache_search._extract_title(md) == "Title here"

    def test_skips_h3_when_no_h1_or_h2(self):
        # An H3-only document has no real title; return None rather
        # than promote a sub-heading that would mislead the agent.
        md = "### Subsection\n\nbody\n"
        assert cache_search._extract_title(md) is None

    def test_returns_none_for_empty(self):
        assert cache_search._extract_title("") is None
        assert cache_search._extract_title("just some prose\n") is None


# ---------------------------------------------------------------------------
# Section attribution
# ---------------------------------------------------------------------------


class TestSectionForOffset:
    """Returns ``(section_index, title)`` — the index is the chainable handle.

    It used to return only a title, which ``get_paper_section`` rejects with
    "Ambiguous section title" whenever a paper repeats a heading (10.9% of a
    real corpus), and it was computed by a separate dialect that lacked the
    empty-section filter, so it could name a section the reader's index had
    already dropped.
    """

    def test_returns_enclosing_h2(self):
        md = "## Intro\n\nfirst\n\n## Methods\n\nsecond chunk here\n"
        idx = md.index("second chunk")
        assert cache_search._section_for_offset(md, idx) == (1, "Methods")

    def test_h3_does_not_open_new_section(self):
        # H3 is a sub-heading; it doesn't change which section we're in.
        md = "## Methods\n\n### Setup\n\ndetails go here\n"
        assert cache_search._section_for_offset(md, md.index("details")) == (0, "Methods")

    def test_a_negative_offset_resolves_to_no_section(self):
        # `search` never produces one, but the (None, None) contract is what
        # lets the caller destructure unconditionally.
        assert cache_search._section_for_offset("# A\n\nbody\n", -1) == (None, None)

    def test_a_document_with_no_sections_resolves_to_no_section(self):
        assert cache_search._section_for_offset("", 0) == (None, None)

    def test_offset_before_first_heading_is_the_preamble(self):
        # Previously returned None, leaving a preamble hit with no navigation
        # at all — even though get_section_content *does* expose that text as
        # section 0. The two now agree.
        md = "Preface text\n\n## First\n\nbody\n"
        assert cache_search._section_for_offset(md, 0) == (0, "Preamble")

    def test_index_is_accepted_by_get_section_content(self):
        # The whole point: whatever index comes back must be chainable.
        md = "## Intro\n\nfirst\n\n## Methods\n\nsecond chunk here\n"
        index, title = cache_search._section_for_offset(md, md.index("second chunk"))
        got = papers.get_section_content(md, index)
        assert "error" not in got
        assert got["title"] == title

    def test_repeated_titles_still_resolve_to_distinct_indices(self):
        # The failure mode this exists to fix: two sections named "Results".
        md = "## Results\n\nalpha here\n\n## Methods\n\nmid\n\n## Results\n\nbeta here\n"
        first = cache_search._section_for_offset(md, md.index("alpha"))
        second = cache_search._section_for_offset(md, md.index("beta"))
        assert first[1] == second[1] == "Results"
        assert first[0] != second[0]
        # Chaining by title is what used to fail.
        assert "error" in papers.get_section_content(md, "Results")
        for index, _title in (first, second):
            assert "error" not in papers.get_section_content(md, index)

    def test_empty_sections_are_skipped_consistently(self):
        # A heading with no body is dropped from the reader's index, so it
        # must not be nameable here either.
        md = "## Empty\n\n## Real\n\nactual body text\n"
        index, title = cache_search._section_for_offset(md, md.index("actual"))
        assert title == "Real"
        assert papers.get_section_content(md, index)["title"] == "Real"


# ---------------------------------------------------------------------------
# Snippet extraction
# ---------------------------------------------------------------------------


class TestExtractSnippet:
    def test_centers_on_query_term(self):
        body = "lorem ipsum " * 50 + "variational dropout " + "lorem " * 50
        snippet, offset = cache_search._extract_snippet(body, {"variational", "dropout"})
        # The phrase must appear in the snippet, not just somewhere in
        # the doc — that's the whole point of centering.
        assert "variational dropout" in snippet
        assert offset is not None

    def test_prefers_cooccurring_terms(self):
        # Two regions: one has just "dropout", the other has both
        # "variational" and "dropout" close together. The cooccurring
        # region should win.
        body = (
            "padding " * 100
            + "dropout regularisation works "
            + "padding " * 200
            + "variational dropout helps inference "
            + "padding " * 100
        )
        snippet, _ = cache_search._extract_snippet(body, {"variational", "dropout"})
        assert "variational dropout" in snippet

    def test_no_terms_falls_back_to_the_head_at_offset_zero(self):
        # Unreachable from `search` (an empty MATCH short-circuits first), but
        # the direct contract is still "head of document, offset 0".
        snippet, offset = cache_search._extract_snippet("# Title\n\nBody text.", set())
        assert offset == 0
        assert snippet.startswith("# Title")

    def test_a_repeated_term_leaves_the_sliding_window_correctly(self):
        # The window slides over the hits keeping a per-term count; a term
        # repeated inside one window must be decremented, not dropped, or the
        # window forgets it is still present and mis-scores the next position.
        gap = "..." * 200
        markdown = (
            "alpha alpha alpha " * 5 + gap + " delta " + gap + " alpha alpha beta gamma " + gap
        )
        snippet, offset = cache_search._extract_snippet(
            markdown, {"alpha", "beta", "gamma", "delta"}
        )
        assert offset is not None
        # The cluster carrying three terms wins over the denser alpha-only run
        # and over the lone "delta" the window must drop on its way past.
        assert "beta" in snippet and "gamma" in snippet

    def test_falls_back_to_head_when_no_match(self):
        body = "introduction " * 50
        snippet, offset = cache_search._extract_snippet(body, {"missing"})
        assert offset is None
        # Returns a slice from the document head, not an empty string.
        assert "introduction" in snippet

    def test_word_boundary_match(self):
        # "drop" must NOT match inside "dropout" — otherwise short
        # query terms accidentally hit substrings everywhere.
        body = "we use dropout heavily in training"
        _, offset = cache_search._extract_snippet(body, {"drop"})
        # No word-boundary match → fallback to head, offset is None.
        assert offset is None

    def test_normalize_locates_accented_term_at_original_offset(self):
        # The folded query term "gutierrez" must locate the accented
        # occurrence and report an offset into the ORIGINAL markdown.
        body = "padding " * 20 + "Work by Gutiérrez here " + "padding " * 20
        _, offset = cache_search._extract_snippet(body, {"gutierrez"}, normalize=True)
        assert offset is not None
        assert body[offset : offset + len("Gutiérrez")] == "Gutiérrez"


# ---------------------------------------------------------------------------
# Filename → canonical inversion
# ---------------------------------------------------------------------------


class TestFilenameToCanonical:
    def test_arxiv_new_style_passes_through(self):
        # New-style arXiv IDs have no slashes, so no inversion needed.
        assert cache_search._filename_to_canonical("arxiv", "2301.00001") == "2301.00001"

    def test_arxiv_old_style_restores_slash(self):
        # Old-style IDs like hep-th/9901001 are stored with the slash
        # converted to underscore; we must restore the slash so
        # get_paper_metadata still finds them.
        assert cache_search._filename_to_canonical("arxiv", "hep-th_9901001") == "hep-th/9901001"

    def test_arxiv_old_style_non_physics_restores_slash(self):
        # Old-style IDs are NOT limited to the hyphenated physics archives.
        # cs/, math/, stat/, etc. take the same archive/NNNNNNN shape and
        # must round-trip too — a hardcoded prefix list silently dropped them.
        assert cache_search._filename_to_canonical("arxiv", "cs_0501001") == "cs/0501001"
        assert cache_search._filename_to_canonical("arxiv", "math_0309136") == "math/0309136"

    def test_arxiv_old_style_with_subject_class_restores_slash(self):
        # Subject-class form, lowercased by canonical_arxiv_id:
        # "math.GT/0309136" → canonical "math.gt/0309136" → stem
        # "math.gt_0309136" on disk → must invert back.
        assert cache_search._filename_to_canonical("arxiv", "math.gt_0309136") == "math.gt/0309136"

    def test_biorxiv_restores_single_slash(self):
        assert (
            cache_search._filename_to_canonical("biorxiv", "10.1101_2024.01.01.123")
            == "10.1101/2024.01.01.123"
        )

    def test_acl_anthology_restores_two_slashes(self):
        # ACL DOIs always start with 10.18653/v1/ — both slashes
        # become underscores on disk and must come back.
        assert (
            cache_search._filename_to_canonical("acl_anthology", "10.18653_v1_2023.acl-long.1")
            == "10.18653/v1/2023.acl-long.1"
        )

    def test_manual_freeform_label_passes_through(self):
        # A label that isn't DOI-shaped has no slash to restore.
        assert (
            cache_search._filename_to_canonical("manual", "my-imported-paper")
            == "my-imported-paper"
        )

    def test_manual_publisher_doi_restores_slash(self):
        # resolve_target sends every non-arXiv/bioRxiv/ACL DOI to the manual
        # namespace, so most manual stems are publisher DOIs. The registrant is
        # digits only, so the first "_" after it is unambiguously the slash —
        # without restoring it the hit's canonical_id chains nowhere.
        assert (
            cache_search._filename_to_canonical("manual", "10.1038_s41586-021-03819-2")
            == "10.1038/s41586-021-03819-2"
        )

    def test_manual_only_the_registrant_slash_is_restored(self):
        # A suffix underscore is left alone: only the slash the registrant
        # prefix introduced is decidable.
        assert cache_search._filename_to_canonical("manual", "10.1234_a_b") == "10.1234/a_b"

    def test_percent_escapes_are_decoded(self):
        # safe_stem percent-encodes anything outside [A-Za-z0-9.-]; the
        # inversion must decode it or the id doesn't round-trip.
        stem = papers.safe_stem("10.1002/(sici)1097-0258")
        assert cache_search._filename_to_canonical("manual", stem) == "10.1002/(sici)1097-0258"

    def test_a_literal_percent_is_not_read_as_an_escape(self):
        # safe_stem writes a literal "%" as "%25", so one unquote is its exact
        # inverse and can't manufacture an escape that was never there.
        stem = papers.safe_stem("10.1234/a%2fb")
        assert cache_search._filename_to_canonical("manual", stem) == "10.1234/a%2fb"

    def test_arxiv_old_style_keeps_its_version(self):
        # canonical_arxiv_id deliberately keeps the version, so a versioned
        # old-style stem occurs and must invert like any other.
        assert cache_search._filename_to_canonical("arxiv", "hep-th_9901001v2") == (
            "hep-th/9901001v2"
        )

    def test_a_namespace_repair_that_does_not_apply_passes_through(self):
        # A bioRxiv stem that does not carry the registrant prefix has no
        # decidable slash to restore.
        assert cache_search._filename_to_canonical("biorxiv", "weird_stem") == "weird_stem"

    def test_an_unknown_namespace_passes_through(self):
        assert cache_search._filename_to_canonical("openalex", "10.1234_x") == "10.1234_x"

    @pytest.mark.parametrize(
        "identifier",
        [
            "hep-th/9901001",
            "hep-th/9901001v2",
            "HEP-TH/9901001",
            "math.GT/0309136",
            "cond-mat.stat-mech/0501001",
            "2301.00001",
            "2301.00001v2",
            "10.1101/2024.01.01.123",
            "10.18653/v1/2023.acl-long.1",
            "10.1038/s41586-021-03819-2",
            "my-imported-paper",
        ],
    )
    def test_round_trips_through_the_namespace_the_router_assigns(self, identifier):
        """The composed contract: a hit's canonical_id chains back into the tools.

        The namespace comes from ``resolve_target``, not from the test — a
        shape asserted against a namespace the router never assigns is a green
        test over a dead branch.
        """
        target = manual.resolve_target(identifier)
        stem = papers.safe_stem(target["canonical"])
        assert (
            cache_search._filename_to_canonical(target["namespace"], stem) == (target["canonical"])
        )

    def test_round_trips_every_namespace(self):
        # The property that matters: safe_stem -> _filename_to_canonical is
        # identity for the identifier shapes each namespace actually stores.
        for ns, canonical in (
            ("arxiv", "2301.00001"),
            ("arxiv", "hep-th/9901001"),
            ("biorxiv", "10.1101/2024.01.01.123"),
            ("acl_anthology", "10.18653/v1/2023.acl-long.1"),
            ("manual", "10.1038/s41586-021-03819-2"),
            ("manual", "my-imported-paper"),
        ):
            assert cache_search._filename_to_canonical(ns, papers.safe_stem(canonical)) == canonical


# ---------------------------------------------------------------------------
# End-to-end search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_empty_cache_returns_empty(self, isolated_cache):
        assert cache_search.search("anything") == []

    def test_no_match_returns_empty(self, isolated_cache):
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Paper\n\n## Abstract\n\nThis is about cats and dogs.\n",
        )
        assert cache_search.search("variational dropout") == []

    def test_query_with_only_stopwords_returns_empty(self, isolated_cache):
        # "the and is" all get filtered before BM25 runs.
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Paper\n\nbody with content.\n",
        )
        assert cache_search.search("the and is") == []

    def test_ranks_relevant_doc_first(self, isolated_cache):
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "1706.03762",
            "# Attention Is All You Need\n\n"
            "## Abstract\n\n"
            "We propose the Transformer, a model based solely on attention "
            "mechanisms. Attention attention attention transformer.\n",
        )
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "1409.0473",
            "# Translation by Aligning\n\n"
            "## Abstract\n\n"
            "We propose a sequence-to-sequence model.\n",
        )
        hits = cache_search.search("attention transformer")
        assert len(hits) >= 1
        assert hits[0]["canonical_id"] == "1706.03762"
        assert hits[0]["title"] == "Attention Is All You Need"

    def test_normalize_retrieves_accented_doc(self, isolated_cache):
        # A doc whose only relevant term is accented ("Gutiérrez") is
        # invisible to an unaccented query by default, but surfaces with
        # normalize=True (query and doc both fold to "gutierrez").
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00002",
            "# Survey\n\n## Refs\n\nMethod introduced by Gutiérrez et al.\n",
        )
        assert cache_search.search("gutierrez") == []
        hits = cache_search.search("gutierrez", normalize=True)
        assert len(hits) == 1
        assert hits[0]["canonical_id"] == "2301.00002"
        assert hits[0]["score"] > 0

    def test_response_shape(self, isolated_cache):
        # Lock in the contract documented in the tool description so
        # an agent can branch on it without feature-detecting. We give
        # the body enough volume that the snippet centre is solidly
        # inside the Methods section, not in the title heading.
        body = (
            "# Some Paper\n\n"
            "## Introduction\n\nbackground prose here.\n\n"
            "## Methods\n\n" + "The transformer applies attention everywhere. " * 5 + "\n"
        )
        _seed_markdown(isolated_cache, "arxiv", "1706.03762", body)
        hits = cache_search.search("transformer attention")
        assert len(hits) == 1
        h = hits[0]
        assert set(h.keys()) == {
            "namespace",
            "canonical_id",
            "score",
            "title",
            "snippet",
            "section",
            "section_index",
            "char_offset",
            "char_count",
        }
        assert h["namespace"] == "arxiv"
        assert h["canonical_id"] == "1706.03762"
        assert h["score"] > 0
        assert h["section"] == "Methods"
        assert h["char_count"] > 0

    def test_top_k_caps_results(self, isolated_cache):
        for i in range(5):
            _seed_markdown(
                isolated_cache,
                "arxiv",
                f"230{i}.00001",
                f"# Paper {i}\n\n## Abstract\n\nattention is the topic.\n",
            )
        hits = cache_search.search("attention", top_k=2)
        assert len(hits) == 2

    def test_namespace_filter(self, isolated_cache):
        # Only the manual hit should come back when namespace="manual".
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Arxiv paper\n\nattention mechanism here.\n",
        )
        _seed_markdown(
            isolated_cache,
            "manual",
            "my-paper",
            "# Manual paper\n\nattention mechanism here.\n",
        )
        hits = cache_search.search("attention", namespace="manual")
        assert len(hits) == 1
        assert hits[0]["namespace"] == "manual"
        assert hits[0]["canonical_id"] == "my-paper"

    def test_acl_canonical_id_restored_in_results(self, isolated_cache):
        # Filename → canonical inversion must run on the way out so the
        # agent can pass canonical_id back into get_paper_metadata.
        _seed_markdown(
            isolated_cache,
            "acl_anthology",
            "10.18653_v1_2023.acl-long.1",
            "# Some ACL paper\n\nattention.\n",
        )
        hits = cache_search.search("attention")
        assert hits[0]["canonical_id"] == "10.18653/v1/2023.acl-long.1"

    def test_zero_score_hits_dropped(self, isolated_cache):
        # An empty markdown file shouldn't surface as a phantom hit.
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.99999",
            "",
        )
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "1706.03762",
            "# Real paper\n\nattention everywhere.\n",
        )
        hits = cache_search.search("attention")
        assert all(h["score"] > 0 for h in hits)
        assert all(h["canonical_id"] != "2301.99999" for h in hits)

    def test_top_k_zero_returns_empty(self, isolated_cache):
        # top_k=0 means "give me none" — it must not silently return one hit.
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# x\n\nattention.\n")
        assert cache_search.search("attention", top_k=0) == []

    def test_top_k_negative_returns_empty(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# x\n\nattention.\n")
        assert cache_search.search("attention", top_k=-5) == []

    def test_tie_break_is_deterministic_by_stem(self, isolated_cache):
        # Identical content → identical BM25 score. Pre-fix, equal-scored hits
        # fell back to entry insertion order, which drifts as new files are
        # appended to the incremental index. Seed b, c first (so the index
        # holds them in that order), then add a which is appended LAST — the
        # output must still be sorted by (namespace, stem), not insertion order.
        content = "# Doc\n\n## Abstract\n\nattention transformer model.\n"
        _seed_markdown(isolated_cache, "arxiv", "b", content)
        _seed_markdown(isolated_cache, "arxiv", "c", content)
        cache_search.search("attention")  # builds index with entries [b, c]
        _seed_markdown(isolated_cache, "arxiv", "a", content)  # appended at end
        hits = cache_search.search("attention")
        assert [h["canonical_id"] for h in hits] == ["a", "b", "c"]

    def test_top_k_clamped_to_max(self, isolated_cache):
        # Even an absurd top_k must not leak more than the documented cap.
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# x\n\nattention.\n",
        )
        # Doesn't crash; the clamp on _MAX_TOP_K is internal.
        hits = cache_search.search("attention", top_k=99999)
        assert len(hits) <= cache_search._MAX_TOP_K

    def test_a_hit_the_snippet_scan_cannot_locate_reports_no_section(self, isolated_cache):
        """FTS5 and the snippet scan disagree about "_", and the hit says so.

        ``unicode61`` treats "_" as a separator, so "attention_model" indexes
        as two tokens and the query matches — but Python's word boundary counts
        "_" as a word character, so the boundary scan finds nothing. The hit is
        real; it just comes back uncentred, and must report that honestly
        rather than naming whichever section happens to hold offset 0.
        """
        _seed_markdown(
            isolated_cache, "arxiv", "p", "# T\n\n## Body\n\nthe attention_model was used.\n"
        )

        (hit,) = cache_search.search("attention")

        assert hit["char_offset"] is None
        assert hit["section"] is None
        assert hit["section_index"] is None
        assert hit["score"] > 0

    @staticmethod
    def _break_reads_of(monkeypatch, filename):
        """Make ``read_text`` raise for one markdown file, leaving others alone."""
        original_read = Path.read_text

        def selective_read(self, *args, **kwargs):
            if self.name == filename:
                raise OSError("vanished")
            return original_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", selective_read)

    def test_unreadable_file_does_not_fail_the_refresh(self, isolated_cache, monkeypatch):
        # A file that cannot be read at index time is flagged, not fatal.
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# Real\n\nattention here.\n")
        _seed_markdown(isolated_cache, "arxiv", "ghost", "attention too\n")

        self._break_reads_of(monkeypatch, "ghost.md")
        hits = cache_search.search("attention")

        assert [h["canonical_id"] for h in hits] == ["2301.00001"]

    def test_a_winner_that_vanishes_before_the_snippet_read_is_skipped(
        self, isolated_cache, monkeypatch
    ):
        """A concurrent eviction between the MATCH and the snippet re-read.

        The patch goes in *after* the index is warm on purpose: installed
        earlier, the refresh flags the file unreadable and deletes its
        postings, so it never reaches the winner loop this covers at all.
        """
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# Real\n\nattention here.\n")
        _seed_markdown(isolated_cache, "arxiv", "ghost", "# Ghost\n\nattention too.\n")
        assert len(cache_search.search("attention")) == 2

        self._break_reads_of(monkeypatch, "ghost.md")
        hits = cache_search.search("attention")

        assert [h["canonical_id"] for h in hits] == ["2301.00001"]

    def test_a_long_document_matching_a_universal_term_still_scores_above_zero(
        self, isolated_cache
    ):
        """The documented invariant: every returned hit scores strictly above zero.

        FTS5 clamps a degenerate IDF (a term in every document) to 1e-6, then
        the BM25 length normalisation scales it *down* — a long document lands
        near 5e-08, which any fixed number of decimal places reports as 0.0.
        """
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "long",
            "# Long\n\ncommon " + " ".join(f"w{j}" for j in range(40000)),
        )
        for i in range(40):
            _seed_markdown(isolated_cache, "arxiv", f"tiny{i}", f"# T{i}\n\ncommon tiny\n")

        hits = cache_search.search("common", top_k=50)

        assert len(hits) == 41
        assert all(h["score"] > 0 for h in hits)
        assert [h for h in hits if h["canonical_id"] == "long"]

    def test_scores_are_corpus_global_not_per_namespace(self, isolated_cache):
        # `namespace` selects which documents come back, not how they rank.
        _seed_markdown(isolated_cache, "arxiv", "a", "# A\n\nattention model here.\n")
        _seed_markdown(isolated_cache, "manual", "b", "# B\n\nattention attention model.\n")

        unfiltered = {h["canonical_id"]: h["score"] for h in cache_search.search("attention")}
        filtered = cache_search.search("attention", namespace="manual")

        assert [h["canonical_id"] for h in filtered] == ["b"]
        assert filtered[0]["score"] == unfiltered["b"]

    def test_top_k_boundary_and_clamp(self, isolated_cache):
        # Exactly at the cap must pass; one past it must clamp. A one-document
        # corpus makes both assertions vacuous.
        cap = cache_search._MAX_TOP_K
        for i in range(cap + 5):
            _seed_markdown(isolated_cache, "arxiv", f"p{i:03d}", f"# P{i}\n\nattention model.\n")

        assert len(cache_search.search("attention", top_k=cap)) == cap
        assert len(cache_search.search("attention", top_k=cap + 1)) == cap


# ---------------------------------------------------------------------------
# MCP tool wiring
# ---------------------------------------------------------------------------


class TestSearchCachedPapersTool:
    @pytest.mark.asyncio
    async def test_tool_returns_documented_envelope(self, isolated_cache):
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "1706.03762",
            "# Attention Is All You Need\n\nThe transformer model.\n",
        )
        result = await server.search_cached_papers("transformer")
        assert result["query"] == "transformer"
        assert result["result_count"] == 1
        assert isinstance(result["results"], list)
        assert result["results"][0]["canonical_id"] == "1706.03762"

    @pytest.mark.asyncio
    async def test_tool_empty_corpus(self, isolated_cache):
        result = await server.search_cached_papers("anything")
        assert result["result_count"] == 0
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_tool_namespace_filter(self, isolated_cache):
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Arxiv\n\ntransformer model.\n",
        )
        _seed_markdown(
            isolated_cache,
            "manual",
            "my-paper",
            "# Manual\n\ntransformer model.\n",
        )
        result = await server.search_cached_papers("transformer", namespace="manual")
        assert result["result_count"] == 1
        assert result["results"][0]["namespace"] == "manual"


# ---------------------------------------------------------------------------
# Persistent incremental index
# ---------------------------------------------------------------------------


class TestIncrementalIndex:
    """The SQLite FTS5 index: incremental refresh, pruning, self-healing.

    These assert observable behaviour — what gets re-read, what the index
    contains, what survives corruption — rather than the previous JSON
    index's internals, so a future format change doesn't break them again.
    """

    def _count_markdown_reads(self, monkeypatch):
        """Record every markdown file read, so re-reads are countable.

        The old suite counted ``_content_tokens`` calls; FTS5 tokenises inside
        SQLite, so the observable cost is the file read.
        """
        seen: list[str] = []
        real = Path.read_text

        def counting(self, *a, **kw):
            if self.suffix == ".md":
                seen.append(self.stem)
            return real(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", counting)
        return seen

    def _index_rows(self):
        con = cache_search._connect()
        try:
            return {(r["ns"], r["stem"]) for r in con.execute("SELECT ns, stem FROM files")}
        finally:
            con.close()

    def test_index_is_created_and_populated(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention model\n")

        hits = cache_search.search("attention")

        assert len(hits) == 1
        assert cache_search._index_path().exists()
        assert self._index_rows() == {("arxiv", "2301.00001")}

    def test_unchanged_files_are_not_reread(self, isolated_cache, monkeypatch):
        body = "# Paper\n\n## Abstract\n\n" + "attention transformer " * 50
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", body)
        cache_search.search("attention")  # build

        seen = self._count_markdown_reads(monkeypatch)
        cache_search.search("attention")

        # Only the winner is re-read, to extract its snippet — never for
        # re-indexing.
        assert seen.count("2301.00001") == 1

    def test_content_change_is_picked_up(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention\n")
        assert cache_search.search("diffusion") == []

        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\ndiffusion model\n")

        assert len(cache_search.search("diffusion")) == 1

    def test_unchanged_sibling_not_reindexed(self, isolated_cache, monkeypatch):
        _seed_markdown(isolated_cache, "arxiv", "stable", "# A\n\nattention alpha\n")
        _seed_markdown(isolated_cache, "arxiv", "churn", "# B\n\nattention beta\n")
        cache_search.search("attention")

        _seed_markdown(isolated_cache, "arxiv", "churn", "# B\n\nattention gamma\n")
        seen = self._count_markdown_reads(monkeypatch)
        cache_search.search("zzzznomatch")

        assert "churn" in seen
        assert "stable" not in seen

    def test_deletion_pruning(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "keep", "# A\n\nattention alpha\n")
        _seed_markdown(isolated_cache, "arxiv", "gone", "# B\n\nattention beta\n")
        cache_search.search("attention")
        assert self._index_rows() == {("arxiv", "keep"), ("arxiv", "gone")}

        (isolated_cache / "arxiv" / "markdown" / "gone.md").unlink()
        hits = cache_search.search("attention")

        assert [h["canonical_id"] for h in hits] == ["keep"]
        assert self._index_rows() == {("arxiv", "keep")}

    def test_namespace_filter_restricts_results(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "a", "# A\n\nshared model term\n")
        _seed_markdown(isolated_cache, "manual", "b", "# B\n\nshared model term\n")

        hits = cache_search.search("model", namespace="manual")

        assert [h["namespace"] for h in hits] == ["manual"]

    def test_corrupt_index_self_heals(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention model\n")
        cache_search.search("attention")

        cache_search._index_path().write_bytes(b"this is not a database at all")

        # Derived state: discard and rebuild rather than failing every search.
        hits = cache_search.search("attention")
        assert len(hits) == 1

    def test_schema_version_mismatch_rebuilds(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention model\n")
        cache_search.search("attention")

        con = cache_search._connect()
        with con:
            con.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        con.close()

        hits = cache_search.search("attention")
        assert len(hits) == 1
        con = cache_search._connect()
        version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        con.close()
        assert int(version) == cache_search._SCHEMA_VERSION

    def test_force_refresh_reindexes_despite_stale_signal(self, isolated_cache, monkeypatch):
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention model\n")
        cache_search.search("attention")

        seen = self._count_markdown_reads(monkeypatch)
        cache_search.search("zzzznomatch", force_refresh=True)

        assert "2301.00001" in seen

    def test_normalize_and_default_share_one_index(self, isolated_cache, monkeypatch):
        _seed_markdown(isolated_cache, "arxiv", "2301.00002", "# S\n\nGutiérrez method\n")
        cache_search.search("gutierrez", normalize=True)

        seen = self._count_markdown_reads(monkeypatch)
        cache_search.search("gutierrez", normalize=True)
        cache_search.search("method")

        # Flipping the mode must not rebuild: both tables are populated in
        # the same pass, so only snippet re-reads happen.
        assert seen.count("2301.00002") <= 2

    def test_concurrent_searches_no_corruption(self, isolated_cache):
        for i in range(8):
            _seed_markdown(
                isolated_cache,
                "arxiv",
                f"230{i}.00001",
                f"# Paper {i}\n\n## Abstract\n\nattention transformer model.\n",
            )

        def run():
            return cache_search.search("attention transformer")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [f.result() for f in [pool.submit(run) for _ in range(8)]]

        assert all(len(r) == len(results[0]) for r in results)
        assert len(self._index_rows()) == 8

    def test_index_dir_not_walked(self, isolated_cache):
        # Asserted against the walker the refresh actually runs: the reserved
        # dir holds the database, not markdown, so indexing it would index the
        # index.
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention\n")
        cache_search.search("attention")
        assert cache_search._index_path().exists()
        walked = cache_search._scan_markdown()
        assert all(ns != cache_search._INDEX_DIRNAME for ns, _, _, _ in walked)
        assert all(ns != cache_search._INDEX_DIRNAME for ns, _ in self._index_rows())

    def test_legacy_json_index_is_swept_away(self, isolated_cache):
        legacy = cache_search._legacy_index_path()
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('{"version": 1, "entries": {}}')
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention\n")

        cache_search.search("attention")

        assert not legacy.exists(), "the replaced 193 MB JSON index should be removed"


class TestIndexFailuresAreNotSilent:
    """A gap in the index must reach the agent, never look like "no results".

    ``unindexable`` exists because a paper absent from the index is invisible
    to BM25 but silently so. The same argument applies to an index that cannot
    be read at all.
    """

    def test_a_transient_read_failure_is_retried_not_frozen(self, isolated_cache, monkeypatch):
        """A file untouched since a failed read must still be picked up later.

        Recording the stat that succeeded alongside the failure would freeze
        it: the ``(mtime, size)`` signal would match forever, so a lock that
        cleared or a chmod (which leaves mtime alone) would never be noticed.
        """
        path = _seed_markdown(isolated_cache, "arxiv", "locked", "# L\n\nattention here.\n")
        original_read = Path.read_text
        broken = True

        def selective_read(self, *args, **kwargs):
            if broken and self.name == "locked.md":
                raise OSError("locked")
            return original_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", selective_read)
        assert cache_search.search("attention") == []
        assert [r["reason"] for r in cache_search.unindexable()] == ["unreadable"]

        broken = False  # the lock cleared; the file itself never changed
        assert path.stat().st_mtime_ns == path.stat().st_mtime_ns

        hits = cache_search.search("attention")
        assert [h["canonical_id"] for h in hits] == ["locked"]
        assert cache_search.unindexable() == []

    def test_a_real_io_failure_reports_the_unreadable_reason(self, isolated_cache, monkeypatch):
        # The engine's own `unreadable` path, not a monkeypatched `unindexable`.
        _seed_markdown(isolated_cache, "arxiv", "ghost", "# G\n\nattention.\n")
        original_read = Path.read_text

        def selective_read(self, *args, **kwargs):
            if self.name == "ghost.md":
                raise OSError("vanished")
            return original_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", selective_read)
        cache_search.search("attention")

        assert cache_search.unindexable() == [
            {"namespace": "arxiv", "stem": "ghost", "reason": "unreadable"}
        ]

    def test_unindexable_without_refresh_does_not_walk_the_corpus(
        self, isolated_cache, monkeypatch
    ):
        """``refresh=False`` is a contract, not an optimisation.

        ``search_cached_papers`` calls ``search`` (which refreshes) and then
        reads the flags in the same hop; refreshing again would double the
        corpus walk on every tool call.
        """
        _seed_markdown(isolated_cache, "arxiv", "p", "# P\n\nattention.\n")
        cache_search.search("attention")

        called = False

        def _boom(**kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(cache_search, "_refresh_index", _boom)
        assert cache_search.unindexable(refresh=False) == []
        assert not called

    @staticmethod
    def _break_the_index(isolated_cache):
        """Leave the schema version current but drop the table it describes.

        A realistic inconsistency, and the one shape `_connect` cannot heal:
        the file is a valid database, so there is nothing to discard, and the
        meta row asserts the tables exist.
        """
        _seed_markdown(isolated_cache, "arxiv", "p", "# P\n\nattention.\n")
        assert len(cache_search.search("attention")) == 1
        con = cache_search._connect()
        with con:
            con.execute("DROP TABLE fts")
        con.close()

    def test_an_unreadable_index_raises_rather_than_reporting_no_hits(self, isolated_cache):
        """A broken index must not look like an empty corpus.

        Every query word is a quoted phrase, so FTS5 has no syntax error left
        to raise here — what a blanket catch swallows is an operational
        failure, answered to the agent as a confident "no paper mentions this".
        """
        self._break_the_index(isolated_cache)

        with pytest.raises(sqlite3.OperationalError):
            cache_search.search("attention")

    @pytest.mark.asyncio
    async def test_the_tool_reports_an_unreadable_index_as_an_error(self, isolated_cache):
        self._break_the_index(isolated_cache)

        result = await server.search_cached_papers("attention")

        assert "error" in result
        assert result["retryable"] is True
        assert "force_refresh" in result["suggestion"]
        assert "result_count" not in result

    def test_a_non_integer_schema_version_rebuilds(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "p", "# P\n\nattention.\n")
        cache_search.search("attention")

        con = cache_search._connect()
        with con:
            con.execute("UPDATE meta SET value = 'not a version' WHERE key = 'schema_version'")
        con.close()

        assert len(cache_search.search("attention")) == 1

    @pytest.mark.asyncio
    async def test_a_query_that_searched_nothing_reports_no_gaps(self, isolated_cache):
        """An all-stopword query short-circuits before the refresh, so it has
        no diagnostic to offer — and must not invent one from a cold index."""
        _seed_markdown(isolated_cache, "manual", "punctuation", "# ---\n\n... !!!\n")

        result = await server.search_cached_papers("the and of")

        assert result["result_count"] == 0
        assert "unindexable_count" not in result
        # No corpus was walked, so the punctuation-only paper is not yet known
        # to be unindexable — reporting it would be inventing a diagnostic.
        con = cache_search._connect()
        try:
            assert con.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        finally:
            con.close()


class TestTheCorpusWalkSurvivesIO:
    """The walk degrades around what it cannot read; it never fails a search."""

    def test_a_cache_root_that_does_not_exist_is_an_empty_corpus(self, monkeypatch):
        monkeypatch.setattr(cache, "_CACHE_ROOT", cache._CACHE_ROOT / "never-created")
        assert cache_search._scan_markdown() == []
        assert cache_search.search("attention") == []

    def test_a_stray_file_at_the_cache_root_is_not_a_namespace(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "p", "# P\n\nattention.\n")
        (isolated_cache / "README").write_text("not a namespace")

        assert [ns for ns, _, _, _ in cache_search._scan_markdown()] == ["arxiv"]

    def test_an_unreadable_cache_root_is_an_empty_corpus(self, isolated_cache, monkeypatch):
        _seed_markdown(isolated_cache, "arxiv", "p", "# P\n\nattention.\n")
        real_scandir = os.scandir

        def guarded(path):
            if Path(path) == cache._CACHE_ROOT:
                raise PermissionError("no")
            return real_scandir(path)

        monkeypatch.setattr(cache_search.os, "scandir", guarded)
        assert cache_search._scan_markdown() == []

    def test_an_unreadable_namespace_is_skipped_not_fatal(self, isolated_cache, monkeypatch):
        _seed_markdown(isolated_cache, "arxiv", "p", "# P\n\nattention.\n")
        _seed_markdown(isolated_cache, "manual", "q", "# Q\n\nattention.\n")
        real_scandir = os.scandir

        def guarded(path):
            if Path(path).parent.name == "manual":
                raise PermissionError("no")
            return real_scandir(path)

        monkeypatch.setattr(cache_search.os, "scandir", guarded)
        assert [ns for ns, _, _, _ in cache_search._scan_markdown()] == ["arxiv"]

    def test_a_file_that_cannot_be_statted_is_skipped(self, isolated_cache, monkeypatch):
        _seed_markdown(isolated_cache, "arxiv", "p", "# P\n\nattention.\n")
        _seed_markdown(isolated_cache, "arxiv", "ghost", "# G\n\nattention.\n")
        real_scandir = os.scandir

        class _UnstattableEntry:
            """A DirEntry whose stat() fails — the real one is read-only."""

            def __init__(self, entry):
                self._entry = entry
                self.name = entry.name
                self.path = entry.path

            def is_dir(self):
                return self._entry.is_dir()

            def stat(self):
                raise OSError("nope")

        def guarded(path):
            return [_UnstattableEntry(e) if e.name == "ghost.md" else e for e in real_scandir(path)]

        monkeypatch.setattr(cache_search.os, "scandir", guarded)
        assert [Path(p).stem for _, p, _, _ in cache_search._scan_markdown()] == ["p"]

    def test_a_non_markdown_file_is_ignored(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "p", "# P\n\nattention.\n")
        (isolated_cache / "arxiv" / "markdown" / "notes.txt").write_text("attention")

        assert [Path(p).stem for _, p, _, _ in cache_search._scan_markdown()] == ["p"]

    def test_a_legacy_index_that_cannot_be_deleted_is_left_alone(self, isolated_cache, monkeypatch):
        legacy = cache_search._legacy_index_path()
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("{}")
        monkeypatch.setattr(Path, "unlink", _raise_oserror)
        _seed_markdown(isolated_cache, "arxiv", "p", "# P\n\nattention.\n")

        # Best-effort: the search still succeeds.
        assert len(cache_search.search("attention")) == 1


class TestConcurrentRefreshUnderChurn:
    def test_searches_interleaved_with_writes_leave_a_consistent_index(self, isolated_cache):
        """The interleaving ``_INDEX_LOCK`` exists for: concurrent *writers*.

        Eight identical searches over a static corpus never contend; this adds
        and removes files underneath them while they run.
        """
        for i in range(6):
            _seed_markdown(isolated_cache, "arxiv", f"base{i}", f"# B{i}\n\nattention model.\n")

        def churn(i):
            path = _seed_markdown(
                isolated_cache, "manual", f"churn{i}", f"# C{i}\n\nattention model.\n"
            )
            hits = cache_search.search("attention", top_k=50)
            path.unlink()
            return hits

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [f.result() for f in [pool.submit(churn, i) for i in range(8)]]

        assert all(isinstance(r, list) for r in results)
        # Whatever the interleaving, the settled index names exactly the files
        # still on disk.
        cache_search.search("attention")
        con = cache_search._connect()
        try:
            rows = {(r["ns"], r["stem"]) for r in con.execute("SELECT ns, stem FROM files")}
        finally:
            con.close()
        assert rows == {("arxiv", f"base{i}") for i in range(6)}


class TestSnippetOffsetUnderLowercaseExpansion:
    def test_default_path_offset_survives_expanding_lowercase(self, isolated_cache):
        # U+0130 'İ'.lower() == 'i' + combining dot (2 chars), so markdown.lower()
        # is LONGER than the original. On the default (normalize=False) path the
        # match offset is taken in the lowered string but used to slice the
        # ORIGINAL markdown and to attribute a section — every preceding 'İ'
        # drifts that offset one char to the right. A big İ block before the
        # query term pushes the snippet/section off the real match entirely.
        body = (
            "# Title\n\n"
            "## Intro\n\n" + ("İ" * 300) + "\n\n"
            "## Methods\n\nThe transformer architecture is described here.\n\n"
            "## Results\n\nUnrelated closing prose about evaluation.\n"
        )
        _seed_markdown(isolated_cache, "manual", "p", body)
        hits = cache_search.search("transformer")
        assert len(hits) == 1
        assert hits[0]["section"] == "Methods"
        assert "transformer" in hits[0]["snippet"].lower()

    def test_normalize_path_offset_aligned(self, isolated_cache):
        # Regression guard: the same recipe under normalize=True must stay
        # correct (it routes through the folded index map).
        body = (
            "# Title\n\n"
            "## Intro\n\n" + ("İ" * 300) + "\n\n"
            "## Methods\n\nThe transformer architecture is described here.\n\n"
            "## Results\n\nUnrelated closing prose about evaluation.\n"
        )
        _seed_markdown(isolated_cache, "manual", "p", body)
        hits = cache_search.search("transformer", normalize=True)
        assert len(hits) == 1
        assert hits[0]["section"] == "Methods"
        assert "transformer" in hits[0]["snippet"].lower()


# ---------------------------------------------------------------------------
# In-memory index memo (avoid re-parsing index.json when nothing changed)
# ---------------------------------------------------------------------------


class TestIndexReuse:
    """The index is not rebuilt when nothing changed.

    The JSON index guarded this with an in-memory memo keyed on the file's
    stat signature — a whole mechanism that existed because parsing 193 MB
    on every query was untenable. SQLite has no equivalent cost, so the
    property is now simply that unchanged documents are not re-read.
    """

    def test_unchanged_corpus_triggers_no_reindex(self, isolated_cache, monkeypatch):
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention model\n")
        cache_search.search("attention")

        indexed: list[int] = []
        real = cache_search._index_document
        monkeypatch.setattr(
            cache_search,
            "_index_document",
            lambda con, rowid, text: (indexed.append(rowid), real(con, rowid, text))[1],
        )
        cache_search.search("attention")

        assert indexed == [], "an unchanged corpus must not be re-indexed"

    def test_corpus_edit_triggers_reindex(self, isolated_cache, monkeypatch):
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention model\n")
        cache_search.search("attention")

        indexed: list[int] = []
        real = cache_search._index_document
        monkeypatch.setattr(
            cache_search,
            "_index_document",
            lambda con, rowid, text: (indexed.append(rowid), real(con, rowid, text))[1],
        )
        _seed_markdown(isolated_cache, "arxiv", "2301.00002", "# Q\n\nattention again\n")
        cache_search.search("attention")

        assert len(indexed) == 1, "only the new document should be indexed"


class TestQueryTokenizationMatchesTheIndex:
    """The query and the documents must be tokenised by the same tokenizer.

    Regression: after the move to FTS5 the documents were tokenised by SQLite
    while the query still went through ``_content_tokens`` — an ASCII-only regex
    written back when this module did its own indexing. It split "Gutiérrez"
    into ``guti OR rrez``, which matched nothing, even though the document had
    indexed cleanly as a single token.
    """

    @pytest.fixture
    def accented(self, isolated_cache):
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00002",
            "# Survey\n\n## Refs\n\nMethod introduced by Gutiérrez et al.\n",
        )
        return isolated_cache

    def test_accented_query_finds_accented_document(self, accented):
        hits = cache_search.search("Gutiérrez")
        assert [h["canonical_id"] for h in hits] == ["2301.00002"]

    def test_unaccented_query_needs_normalize(self, accented):
        # The documented contract, unchanged: folding is opt-in.
        assert cache_search.search("gutierrez") == []
        assert [h["canonical_id"] for h in cache_search.search("gutierrez", normalize=True)] == [
            "2301.00002"
        ]

    def test_accented_query_also_works_under_normalize(self, accented):
        assert len(cache_search.search("Gutiérrez", normalize=True)) == 1

    @pytest.mark.parametrize("query", ["NOT", "OR", "*", "-", "a:b", 'quote"inside', "( )", "^"])
    def test_fts_syntax_in_a_query_never_raises(self, accented, query):
        # Every term is quoted, so operators are matched literally rather
        # than parsed — an unquoted one would make FTS5 reject the whole
        # expression.
        assert isinstance(cache_search.search(query), list)

    def test_multiword_query_ors_its_terms(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "a", "# A\n\nattention only here\n")
        _seed_markdown(isolated_cache, "arxiv", "b", "# B\n\ntransformer only here\n")
        found = {h["canonical_id"] for h in cache_search.search("attention transformer")}
        assert found == {"a", "b"}

    def test_empty_and_whitespace_queries_return_nothing(self, accented):
        for query in ("", "   ", "\n\t"):
            assert cache_search.search(query) == []


class TestNonLatinQueryReachesTheIndex:
    """A non-Latin query must reach FTS5 rather than being gated out.

    Follow-on regression from the same root cause as
    ``TestQueryTokenizationMatchesTheIndex``: that fix rebuilt the MATCH
    expression from raw words, but ``search`` still *gated* on
    ``_content_tokens(query)`` being non-empty. A query of purely non-Latin words
    tokenises to ``[]`` under that ASCII regex, so the search returned early
    and reported nothing — even though the document had indexed the term and
    a raw ``MATCH`` against the very same index found it.
    """

    @pytest.fixture
    def mixed(self, isolated_cache):
        # An ordinary English paper that happens to carry non-Latin terms —
        # the realistic case. The document itself indexes fine, so there is
        # no ``unindexable`` diagnostic to warn the agent either.
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00003",
            "# Attention\n\n## Methods\n\nWe trained it.\n\n"
            "## Results\n\nJapanese: 注意力機構. Russian: Нейронные сети.\n",
        )
        return isolated_cache

    def test_document_is_indexed_not_skipped(self, mixed):
        cache_search.search("attention")
        assert cache_search.unindexable() == []

    @pytest.mark.parametrize("query", ["注意力機構", "Нейронные"])
    def test_non_latin_query_finds_the_document(self, mixed, query):
        assert [h["canonical_id"] for h in cache_search.search(query)] == ["2301.00003"]

    @pytest.mark.parametrize("query", ["注意力機構", "Нейронные"])
    def test_non_latin_hit_is_chainable_into_get_paper_section(self, mixed, query):
        # The whole point of #54's section_index: a hit that resolves to the
        # document head with section None is a dead end for the agent.
        (hit,) = cache_search.search(query)
        assert hit["section"] == "Results"
        assert hit["section_index"] is not None
        assert hit["char_offset"] > 0

    def test_accented_hit_is_chainable_too(self, isolated_cache):
        # ``_content_tokens`` mangles "Gutiérrez" into guti/rrez, neither of which
        # appears in the text, so snippet centring found nothing and the hit
        # came back with no section.
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "a",
            "# Paper\n\n## Intro\n\nnothing.\n\n## Results\n\nBy Ana Gutiérrez.\n",
        )
        (hit,) = cache_search.search("Gutiérrez")
        assert hit["section"] == "Results"
        assert hit["char_offset"] > 0


class TestStopwordsStayOutOfTheMatchExpression:
    """Stopwords must be filtered from the query, not just from ``_content_tokens``.

    FTS5 indexes the *raw* markdown under ``unicode61``, which strips neither
    stopwords nor single characters. Once the MATCH expression was built from
    raw words, "the" became a real search term matching essentially every
    document in the corpus.
    """

    @pytest.fixture
    def corpus(self, isolated_cache):
        _seed_markdown(isolated_cache, "arxiv", "relevant", "# A\n\nThe transformer model.\n")
        _seed_markdown(isolated_cache, "arxiv", "irrelevant", "# B\n\nThe cat sat on the mat.\n")
        return isolated_cache

    def test_stopword_does_not_drag_in_unrelated_documents(self, corpus):
        found = {h["canonical_id"] for h in cache_search.search("the transformer")}
        assert found == {"relevant"}

    def test_an_all_stopword_query_matches_nothing(self, corpus):
        assert cache_search.search("the and of") == []

    def test_single_characters_are_dropped(self, corpus):
        assert cache_search.search("a") == []
        assert cache_search.search("x") == []

    def test_stopword_filtering_survives_into_the_match_expression(self):
        assert cache_search._fts_query("the transformer") == '"transformer"'
        assert cache_search._fts_query("the") == ""

    def test_a_quote_in_a_word_is_doubled_not_dropped(self):
        # Inside a quoted phrase only `"` is special to FTS5, and doubling is
        # how it is escaped — an unbalanced one would make the whole
        # expression a syntax error.
        assert cache_search._fts_query('quote"inside') == '"quote""inside"'

    def test_a_repeated_word_contributes_one_term(self):
        assert cache_search._fts_query("attention attention") == '"attention"'
        assert cache_search._fts_query("attention model attention") == ('"attention" OR "model"')

    def test_a_nul_splits_a_query_the_way_the_tokeniser_does(self):
        # sqlite3 cannot bind a string carrying a NUL at all, and `unicode61`
        # treats it as a separator, so the query is split on it.
        assert cache_search._fts_query("attention\x00model") == '"attention" OR "model"'

    def test_empty_match_expression_short_circuits_before_indexing(
        self, isolated_cache, monkeypatch
    ):
        # An empty MATCH expression is a syntax error to FTS5, and there is
        # no reason to walk the corpus to discover the query was empty.
        called = False

        def _boom(**kwargs):
            nonlocal called
            called = True

        monkeypatch.setattr(cache_search, "_refresh_index", _boom)
        assert cache_search.search("the") == []
        assert not called
