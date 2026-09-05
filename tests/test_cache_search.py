"""Tests for the BM25 search over cached markdown files."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from academic_tools_mcp import cache, cache_search, papers, server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the cache root to a fresh tmp dir for each test.

    cache_search reads cache._CACHE_ROOT directly, so monkeypatching
    that single attribute is enough to sandbox the whole search.
    """
    monkeypatch.setattr(cache, "_CACHE_ROOT", tmp_path / ".cache")
    return tmp_path / ".cache"


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


class TestTokenize:
    def test_lowercases_and_drops_stopwords(self):
        # "is" and "you" are stopwords; "all" is deliberately NOT a
        # stopword (it's content-bearing in academic prose).
        assert cache_search._tokenize("Attention Is All You Need") == [
            "attention",
            "all",
            "need",
        ]

    def test_drops_punctuation(self):
        # Brackets, parens, commas all split tokens cleanly. Trailing
        # period on "al." gets stripped because the regex requires the
        # last char of a multi-char token to be alphanumeric — "al"
        # comes back without it.
        assert cache_search._tokenize("Vaswani et al. (2017), [1]") == [
            "vaswani",
            "et",
            "al",
            "2017",
        ]

    def test_preserves_intra_word_hyphens(self):
        # Domain terms with hyphens must survive as single tokens —
        # otherwise "self-attention" can't be queried as a phrase.
        toks = cache_search._tokenize("self-attention and cross-attention")
        assert "self-attention" in toks
        assert "cross-attention" in toks

    def test_preserves_intra_word_dots(self):
        # Version strings and acronyms with dots stay intact.
        assert "bm25" in cache_search._tokenize("BM25 ranks documents")
        assert "v1.5" in cache_search._tokenize("model v1.5 fine-tuned")

    def test_drops_stopwords(self):
        # The classic stopwords are gone but content words survive.
        toks = cache_search._tokenize("the model is trained on a corpus of papers")
        for stop in ("the", "is", "on", "a", "of"):
            assert stop not in toks
        assert "model" in toks and "trained" in toks and "corpus" in toks

    def test_drops_single_char_tokens(self):
        # "x" alone is noise; "x86" is content.
        toks = cache_search._tokenize("we run x and y on x86 hardware")
        assert "x" not in toks
        assert "y" not in toks
        assert "x86" in toks

    def test_normalize_folds_diacritics(self):
        # Without normalize the diacritic splits the token (the regex
        # only keeps [a-z0-9-.] runs), so "Gutiérrez" → ["guti", "rrez"].
        assert cache_search._tokenize("Gutiérrez") == ["guti", "rrez"]
        # With normalize it folds to a single ASCII token.
        assert cache_search._tokenize("Gutiérrez", normalize=True) == [
            "gutierrez",
        ]


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
        snippet, offset = cache_search._extract_snippet(body, {"drop"})
        # No word-boundary match → fallback to head, offset is None.
        assert offset is None

    def test_normalize_locates_accented_term_at_original_offset(self):
        # The folded query term "gutierrez" must locate the accented
        # occurrence and report an offset into the ORIGINAL markdown.
        body = "padding " * 20 + "Work by Gutiérrez here " + "padding " * 20
        snippet, offset = cache_search._extract_snippet(body, {"gutierrez"}, normalize=True)
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

    def test_manual_passes_through(self):
        # Manual canonical IDs are arbitrary user input; we don't
        # try to restore slashes, so the filename stem comes back as-is.
        assert (
            cache_search._filename_to_canonical("manual", "my-imported-paper")
            == "my-imported-paper"
        )


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

    def test_handles_unreadable_file(self, isolated_cache, monkeypatch):
        # A file vanishing mid-walk (concurrent eviction, etc.) must
        # not fail the whole search — skip and continue.
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Real\n\nattention here.\n",
        )
        ghost = _seed_markdown(
            isolated_cache,
            "arxiv",
            "ghost",
            "doesn't matter\n",
        )

        original_read = type(ghost).read_text

        def selective_read(self, *args, **kwargs):
            if self.name == "ghost.md":
                raise OSError("vanished")
            return original_read(self, *args, **kwargs)

        monkeypatch.setattr(type(ghost), "read_text", selective_read)
        hits = cache_search.search("attention")
        # The real paper still surfaces; the ghost is skipped silently.
        assert any(h["canonical_id"] == "2301.00001" for h in hits)


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

        The old suite counted ``_tokenize`` calls; FTS5 tokenises inside
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
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention\n")
        cache_search.search("attention")
        walked = list(cache_search._iter_markdown_files(None))
        assert all(ns != cache_search._INDEX_DIRNAME for ns, _ in walked)

    def test_legacy_json_index_is_swept_away(self, isolated_cache):
        legacy = cache_search._legacy_index_path()
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text('{"version": 1, "entries": {}}')
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", "# P\n\nattention\n")

        cache_search.search("attention")

        assert not legacy.exists(), "the replaced 193 MB JSON index should be removed"


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
    while the query still went through ``_tokenize`` — an ASCII-only regex
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
    ``_tokenize(query)`` being non-empty. A query of purely non-Latin words
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
        # ``_tokenize`` mangles "Gutiérrez" into guti/rrez, neither of which
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
    """Stopwords must be filtered from the query, not just from ``_tokenize``.

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
