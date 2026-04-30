"""Tests for the BM25 search over cached markdown files."""

import pytest

from academic_tools_mcp import cache, cache_search, server


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
            "attention", "all", "need",
        ]

    def test_drops_punctuation(self):
        # Brackets, parens, commas all split tokens cleanly. Trailing
        # period on "al." gets stripped because the regex requires the
        # last char of a multi-char token to be alphanumeric — "al"
        # comes back without it.
        assert cache_search._tokenize("Vaswani et al. (2017), [1]") == [
            "vaswani", "et", "al", "2017",
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
        toks = cache_search._tokenize(
            "the model is trained on a corpus of papers"
        )
        for stop in ("the", "is", "on", "a", "of"):
            assert stop not in toks
        assert "model" in toks and "trained" in toks and "corpus" in toks

    def test_drops_single_char_tokens(self):
        # "x" alone is noise; "x86" is content.
        toks = cache_search._tokenize("we run x and y on x86 hardware")
        assert "x" not in toks
        assert "y" not in toks
        assert "x86" in toks


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
    def test_returns_enclosing_h2(self):
        md = "## Intro\n\nfirst\n\n## Methods\n\nsecond chunk here\n"
        # Offset inside "second chunk" must attribute to Methods.
        idx = md.index("second chunk")
        assert cache_search._section_for_offset(md, idx) == "Methods"

    def test_h3_does_not_open_new_section(self):
        # H3 is a sub-heading; it doesn't change which section we're in.
        md = "## Methods\n\n### Setup\n\ndetails go here\n"
        idx = md.index("details")
        assert cache_search._section_for_offset(md, idx) == "Methods"

    def test_offset_before_first_heading(self):
        # Content before any heading isn't attributed to a section.
        md = "Preface text\n\n## First\n\nbody\n"
        assert cache_search._section_for_offset(md, 0) is None


# ---------------------------------------------------------------------------
# Snippet extraction
# ---------------------------------------------------------------------------


class TestExtractSnippet:
    def test_centers_on_query_term(self):
        body = "lorem ipsum " * 50 + "variational dropout " + "lorem " * 50
        snippet, offset = cache_search._extract_snippet(
            body, {"variational", "dropout"}
        )
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
        snippet, _ = cache_search._extract_snippet(
            body, {"variational", "dropout"}
        )
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


# ---------------------------------------------------------------------------
# Filename → canonical inversion
# ---------------------------------------------------------------------------


class TestFilenameToCanonical:
    def test_arxiv_new_style_passes_through(self):
        # New-style arXiv IDs have no slashes, so no inversion needed.
        assert (
            cache_search._filename_to_canonical("arxiv", "2301.00001")
            == "2301.00001"
        )

    def test_arxiv_old_style_restores_slash(self):
        # Old-style IDs like hep-th/9901001 are stored with the slash
        # converted to underscore; we must restore the slash so
        # get_paper_metadata still finds them.
        assert (
            cache_search._filename_to_canonical("arxiv", "hep-th_9901001")
            == "hep-th/9901001"
        )

    def test_biorxiv_restores_single_slash(self):
        assert (
            cache_search._filename_to_canonical(
                "biorxiv", "10.1101_2024.01.01.123"
            )
            == "10.1101/2024.01.01.123"
        )

    def test_acl_anthology_restores_two_slashes(self):
        # ACL DOIs always start with 10.18653/v1/ — both slashes
        # become underscores on disk and must come back.
        assert (
            cache_search._filename_to_canonical(
                "acl_anthology", "10.18653_v1_2023.acl-long.1"
            )
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
            isolated_cache, "arxiv", "2301.00001",
            "# Paper\n\n## Abstract\n\nThis is about cats and dogs.\n",
        )
        assert cache_search.search("variational dropout") == []

    def test_query_with_only_stopwords_returns_empty(self, isolated_cache):
        # "the and is" all get filtered before BM25 runs.
        _seed_markdown(
            isolated_cache, "arxiv", "2301.00001",
            "# Paper\n\nbody with content.\n",
        )
        assert cache_search.search("the and is") == []

    def test_ranks_relevant_doc_first(self, isolated_cache):
        _seed_markdown(
            isolated_cache, "arxiv", "1706.03762",
            "# Attention Is All You Need\n\n"
            "## Abstract\n\n"
            "We propose the Transformer, a model based solely on attention "
            "mechanisms. Attention attention attention transformer.\n",
        )
        _seed_markdown(
            isolated_cache, "arxiv", "1409.0473",
            "# Translation by Aligning\n\n"
            "## Abstract\n\n"
            "We propose a sequence-to-sequence model.\n",
        )
        hits = cache_search.search("attention transformer")
        assert len(hits) >= 1
        assert hits[0]["canonical_id"] == "1706.03762"
        assert hits[0]["title"] == "Attention Is All You Need"

    def test_response_shape(self, isolated_cache):
        # Lock in the contract documented in the tool description so
        # an agent can branch on it without feature-detecting. We give
        # the body enough volume that the snippet centre is solidly
        # inside the Methods section, not in the title heading.
        body = (
            "# Some Paper\n\n"
            "## Introduction\n\nbackground prose here.\n\n"
            "## Methods\n\n"
            + "The transformer applies attention everywhere. " * 5
            + "\n"
        )
        _seed_markdown(isolated_cache, "arxiv", "1706.03762", body)
        hits = cache_search.search("transformer attention")
        assert len(hits) == 1
        h = hits[0]
        assert set(h.keys()) == {
            "namespace", "canonical_id", "score", "title",
            "snippet", "section", "char_count",
        }
        assert h["namespace"] == "arxiv"
        assert h["canonical_id"] == "1706.03762"
        assert h["score"] > 0
        assert h["section"] == "Methods"
        assert h["char_count"] > 0

    def test_top_k_caps_results(self, isolated_cache):
        for i in range(5):
            _seed_markdown(
                isolated_cache, "arxiv", f"230{i}.00001",
                f"# Paper {i}\n\n## Abstract\n\nattention is the topic.\n",
            )
        hits = cache_search.search("attention", top_k=2)
        assert len(hits) == 2

    def test_namespace_filter(self, isolated_cache):
        # Only the manual hit should come back when namespace="manual".
        _seed_markdown(
            isolated_cache, "arxiv", "2301.00001",
            "# Arxiv paper\n\nattention mechanism here.\n",
        )
        _seed_markdown(
            isolated_cache, "manual", "my-paper",
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
            isolated_cache, "acl_anthology", "10.18653_v1_2023.acl-long.1",
            "# Some ACL paper\n\nattention.\n",
        )
        hits = cache_search.search("attention")
        assert hits[0]["canonical_id"] == "10.18653/v1/2023.acl-long.1"

    def test_zero_score_hits_dropped(self, isolated_cache):
        # An empty markdown file shouldn't surface as a phantom hit.
        _seed_markdown(
            isolated_cache, "arxiv", "2301.99999", "",
        )
        _seed_markdown(
            isolated_cache, "arxiv", "1706.03762",
            "# Real paper\n\nattention everywhere.\n",
        )
        hits = cache_search.search("attention")
        assert all(h["score"] > 0 for h in hits)
        assert all(h["canonical_id"] != "2301.99999" for h in hits)

    def test_top_k_clamped_to_max(self, isolated_cache):
        # Even an absurd top_k must not leak more than the documented cap.
        _seed_markdown(
            isolated_cache, "arxiv", "2301.00001",
            "# x\n\nattention.\n",
        )
        # Doesn't crash; the clamp on _MAX_TOP_K is internal.
        hits = cache_search.search("attention", top_k=99999)
        assert len(hits) <= cache_search._MAX_TOP_K

    def test_handles_unreadable_file(self, isolated_cache, monkeypatch):
        # A file vanishing mid-walk (concurrent eviction, etc.) must
        # not fail the whole search — skip and continue.
        _seed_markdown(
            isolated_cache, "arxiv", "2301.00001",
            "# Real\n\nattention here.\n",
        )
        ghost = _seed_markdown(
            isolated_cache, "arxiv", "ghost", "doesn't matter\n",
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
            isolated_cache, "arxiv", "1706.03762",
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
            isolated_cache, "arxiv", "2301.00001",
            "# Arxiv\n\ntransformer model.\n",
        )
        _seed_markdown(
            isolated_cache, "manual", "my-paper",
            "# Manual\n\ntransformer model.\n",
        )
        result = await server.search_cached_papers(
            "transformer", namespace="manual"
        )
        assert result["result_count"] == 1
        assert result["results"][0]["namespace"] == "manual"
