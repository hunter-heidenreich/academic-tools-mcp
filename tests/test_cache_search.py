"""Tests for the BM25 search over cached markdown files."""

import json
from concurrent.futures import ThreadPoolExecutor

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
        # Subject-class form, lowercased by _canonical_arxiv_id:
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
    def _count_tokenize_calls(self, monkeypatch):
        """Wrap _tokenize with a counter and return the list of texts seen.

        Returns a list whose entries are the text args _tokenize was
        called with (query calls included). Tests assert on how many
        *document* tokenisations happen by filtering out the short query.
        """
        seen: list[str] = []
        original = cache_search._tokenize

        def counting(text, *, normalize=False):
            seen.append(text)
            return original(text, normalize=normalize)

        monkeypatch.setattr(cache_search, "_tokenize", counting)
        return seen

    def test_index_built_and_reused(self, isolated_cache, monkeypatch):
        # A long body so we can tell document tokenisations apart from
        # the tiny query tokenisation by length.
        body = "# Paper\n\n## Abstract\n\n" + "attention transformer " * 50
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", body)

        seen = self._count_tokenize_calls(monkeypatch)
        hits1 = cache_search.search("attention")
        assert len(hits1) == 1
        # The index file now exists.
        assert cache_search._index_path().exists()
        doc_tokenizations_first = [t for t in seen if t == body]
        # Built once: both modes tokenised on first sight → 2 calls.
        assert len(doc_tokenizations_first) == 2

        seen.clear()
        hits2 = cache_search.search("attention")
        assert hits2 == hits1
        # Second search: the unchanged doc is NOT re-tokenised at all.
        assert [t for t in seen if t == body] == []

    def test_staleness_on_content_change(self, isolated_cache, monkeypatch):
        path = _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Paper\n\n## Abstract\n\nThis paper is about felines.\n",
        )
        assert cache_search.search("genomics") == []

        # Rewrite with new content. write_text changes size (and mtime),
        # so the staleness check must re-tokenise it.
        seen = self._count_tokenize_calls(monkeypatch)
        new_body = "# Paper\n\n## Abstract\n\nThis paper is about genomics.\n"
        path.write_text(new_body)
        hits = cache_search.search("genomics")
        assert len(hits) == 1
        assert hits[0]["canonical_id"] == "2301.00001"
        assert new_body in seen  # the changed doc was re-tokenised

    def test_unchanged_sibling_not_retokenized(self, isolated_cache, monkeypatch):
        stable = "# A\n\n## Abstract\n\n" + "attention " * 40
        _seed_markdown(isolated_cache, "arxiv", "1111.00001", stable)
        changing_path = _seed_markdown(
            isolated_cache,
            "arxiv",
            "2222.00002",
            "# B\n\n## Abstract\n\nfelines everywhere.\n",
        )
        cache_search.search("attention")  # build the index

        seen = self._count_tokenize_calls(monkeypatch)
        changing_path.write_text("# B\n\n## Abstract\n\ngenomics everywhere.\n")
        cache_search.search("attention")
        # The stable sibling must not be re-tokenised; only the changed one.
        assert stable not in seen

    def test_deletion_pruning(self, isolated_cache):
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "1111.00001",
            "# A\n\nattention model.\n",
        )
        gone = _seed_markdown(
            isolated_cache,
            "arxiv",
            "2222.00002",
            "# B\n\nattention model.\n",
        )
        assert len(cache_search.search("attention")) == 2

        gone.unlink()
        hits = cache_search.search("attention")
        assert len(hits) == 1
        assert all(h["canonical_id"] != "2222.00002" for h in hits)
        # The deleted doc's entry is gone from the on-disk index too.
        index = json.loads(cache_search._index_path().read_text())
        keys = list(index["entries"])
        assert not any(k.endswith("2222.00002") for k in keys)

    def test_namespace_corpus_stats_isolated(self, isolated_cache):
        # A namespace-filtered search must compute IDF over only that
        # namespace, identical to a cache holding just that namespace.
        # Seed arxiv with many docs containing "model" (drives its IDF
        # down globally) and one manual doc.
        for i in range(6):
            _seed_markdown(
                isolated_cache,
                "arxiv",
                f"230{i}.00001",
                f"# Arxiv {i}\n\nmodel model model attention.\n",
            )
        _seed_markdown(
            isolated_cache,
            "manual",
            "only-one",
            "# Manual\n\nmodel attention here.\n",
        )
        manual_hits = cache_search.search("model", namespace="manual")
        assert len(manual_hits) == 1
        assert manual_hits[0]["namespace"] == "manual"

        # Compare against a cache that ONLY has the manual doc — same score.
        manual_score = manual_hits[0]["score"]
        # Drop everything but the manual doc by deleting arxiv files and
        # forcing an index refresh; the manual score must be unchanged
        # because corpus stats were already namespace-scoped.
        for i in range(6):
            (isolated_cache / "arxiv" / "markdown" / f"230{i}.00001.md").unlink()
        again = cache_search.search("model", namespace="manual")
        assert again[0]["score"] == manual_score

    def test_normalize_served_from_one_build(self, isolated_cache, monkeypatch):
        body = "# Survey\n\n## Refs\n\nMethod introduced by Gutiérrez et al.\n"
        _seed_markdown(isolated_cache, "arxiv", "2301.00002", body)

        # Build the index via a normalize=False search first.
        assert cache_search.search("gutierrez") == []

        # Now flip to normalize=True. The accented doc must surface
        # WITHOUT re-tokenising it — both modes were stored at build time.
        seen = self._count_tokenize_calls(monkeypatch)
        hits = cache_search.search("gutierrez", normalize=True)
        assert len(hits) == 1
        assert hits[0]["canonical_id"] == "2301.00002"
        assert body not in seen  # no document re-tokenisation on mode flip

    def test_corrupt_index_self_heals(self, isolated_cache):
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Real\n\nattention everywhere.\n",
        )
        cache_search.search("attention")  # build it
        # Corrupt the index file.
        cache_search._index_path().write_text("{not valid json")
        hits = cache_search.search("attention")
        assert len(hits) == 1
        assert hits[0]["canonical_id"] == "2301.00001"
        # Rebuilt into valid JSON.
        index = json.loads(cache_search._index_path().read_text())
        assert index["version"] == cache_search._INDEX_VERSION

    def test_version_mismatch_rebuilds(self, isolated_cache):
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Real\n\nattention everywhere.\n",
        )
        # Hand-write a stale-version index.
        path = cache_search._index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 0, "entries": {}}))
        hits = cache_search.search("attention")
        assert len(hits) == 1
        index = json.loads(path.read_text())
        assert index["version"] == cache_search._INDEX_VERSION
        assert len(index["entries"]) == 1

    def test_force_refresh_rebuilds_despite_stale_signal(self, isolated_cache, monkeypatch):
        body = "# Paper\n\n## Abstract\n\n" + "attention " * 30
        _seed_markdown(isolated_cache, "arxiv", "2301.00001", body)
        cache_search.search("attention")  # build the index

        seen = self._count_tokenize_calls(monkeypatch)
        # Without force_refresh the unchanged file is reused (no tokenise).
        cache_search.search("attention")
        assert body not in seen
        seen.clear()
        # With force_refresh it is re-tokenised even though mtime/size match.
        cache_search.search("attention", force_refresh=True)
        assert body in seen

    def test_results_identical_full_dict(self, isolated_cache):
        # Lock the complete per-hit dict so the index path can't silently
        # drift any field from the original fresh-scan output.
        body = (
            "# Some Paper\n\n"
            "## Introduction\n\nbackground prose here.\n\n"
            "## Methods\n\n" + "The transformer applies attention everywhere. " * 5 + "\n"
        )
        _seed_markdown(isolated_cache, "arxiv", "1706.03762", body)
        hits = cache_search.search("transformer attention")
        assert hits == [
            {
                "namespace": "arxiv",
                "canonical_id": "1706.03762",
                "score": hits[0]["score"],  # float compared separately below
                "title": "Some Paper",
                "snippet": hits[0]["snippet"],
                "section": "Methods",
                "char_count": len(body),
            }
        ]
        assert hits[0]["score"] > 0
        assert "transformer applies attention" in hits[0]["snippet"]

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
            results = [f.result() for f in [pool.submit(run) for _ in range(16)]]

        # Every concurrent search returns the full, correct result set.
        assert all(len(r) == 8 for r in results)
        # The index on disk is still valid JSON after the concurrent writes.
        index = json.loads(cache_search._index_path().read_text())
        assert len(index["entries"]) == 8

    def test_index_dir_not_walked(self, isolated_cache):
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Real\n\nattention.\n",
        )
        cache_search.search("attention")  # creates __search_index__/
        # The reserved index dir must never be yielded as a corpus file.
        walked = cache_search._iter_markdown_files()
        assert all(ns != cache_search._INDEX_DIRNAME for ns, _ in walked)


# ---------------------------------------------------------------------------
# Snippet/section offsets under length-changing lowercase
# ---------------------------------------------------------------------------


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


class TestIndexMemo:
    def test_index_not_reparsed_when_unchanged(self, isolated_cache, monkeypatch):
        # First search warms the in-memory memo (and writes index.json). A
        # second search over an unchanged corpus must NOT re-parse the index
        # JSON from disk — it serves the memo, keyed by index.json's stat.
        _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Paper\n\n## Abstract\n\n" + "attention " * 30,
        )
        cache_search.search("attention")  # warm memo + write index.json

        calls = {"n": 0}
        original = cache_search._load_index

        def counting():
            calls["n"] += 1
            return original()

        monkeypatch.setattr(cache_search, "_load_index", counting)
        cache_search.search("attention")
        assert calls["n"] == 0

    def test_memo_invalidated_on_corpus_edit(self, isolated_cache):
        # The index-file memo must never shortcut the corpus stat-walk: editing
        # a markdown file leaves index.json untouched (so the memo signature is
        # still "fresh"), but the changed content must still be picked up.
        path = _seed_markdown(
            isolated_cache,
            "arxiv",
            "2301.00001",
            "# Paper\n\n## Abstract\n\nThis paper is about felines.\n",
        )
        assert len(cache_search.search("felines")) == 1
        assert cache_search.search("genomics") == []

        path.write_text("# Paper\n\n## Abstract\n\nThis paper is about genomics.\n")
        assert len(cache_search.search("genomics")) == 1
        assert cache_search.search("felines") == []
