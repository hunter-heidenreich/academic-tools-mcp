"""Tests for the search tools in ``tools/search.py``.

Covers the slim hit shape, parameter threading to each provider, the
error contract, and ``find_in_paper``.
"""

import pytest

from academic_tools_mcp import cache, manual, papers, server
from academic_tools_mcp.providers import arxiv, crossref

# ---------------------------------------------------------------------------
# Slim search hits: author_count lets the agent decide whether to paginate
# ---------------------------------------------------------------------------


class TestSearchAuthorCount:
    """Slim search responses dropped the full author list to keep
    payloads small; agents got `first_author` only and had no way to
    know whether the paper has 3 authors or 3,000. ``author_count``
    closes that loop without re-bloating the response.
    """

    @pytest.mark.asyncio
    async def test_search_arxiv_includes_author_count(self, monkeypatch):
        async def fake_search(query, max_results=10):
            return {
                "total_results": 1,
                "entries": [
                    {
                        "id": "http://arxiv.org/abs/2301.00001v1",
                        "title": "Tiny paper",
                        "published": "2023-01-01T00:00:00Z",
                        "authors": [{"name": "Jane Doe"}, {"name": "John Roe"}],
                    },
                ],
            }

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        result = await server.search_arxiv("anything")
        assert result["results"][0]["author_count"] == 2
        assert result["results"][0]["first_author"] == "Jane Doe"

    @pytest.mark.asyncio
    async def test_search_arxiv_zero_authors(self, monkeypatch):
        # Defensive: the parser returns [] for missing authors. The slim
        # tool must still report 0, not crash, and not omit the field.
        async def fake_search(query, max_results=10):
            return {
                "total_results": 1,
                "entries": [
                    {
                        "id": "http://arxiv.org/abs/2301.99999",
                        "title": "Authorless oddity",
                        "published": "2023-01-01T00:00:00Z",
                        "authors": [],
                    }
                ],
            }

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        result = await server.search_arxiv("anything")
        assert result["results"][0]["author_count"] == 0
        assert result["results"][0]["first_author"] is None

    @pytest.mark.asyncio
    async def test_search_arxiv_response_shape_matches_crossref(self, monkeypatch):
        # search_arxiv and search_crossref_by_title must return the same
        # top-level shape so an agent can branch on the source without
        # feature-detecting field names. total_results is the upstream
        # match count (how many exist); result_count is how many hits the
        # call actually returned.
        async def fake_arxiv(query, max_results=10):
            return {"total_results": 0, "entries": []}

        async def fake_crossref(bibliographic, year=None, rows=5):
            return {"items": [], "total_results": 0}

        monkeypatch.setattr(arxiv, "search_papers", fake_arxiv)
        monkeypatch.setattr(crossref, "search_works", fake_crossref)

        arxiv_result = await server.search_arxiv("anything")
        crossref_result = await server.search_crossref_by_title("anything")
        assert set(arxiv_result.keys()) == {"total_results", "result_count", "results"}
        assert set(crossref_result.keys()) == set(arxiv_result.keys())

    @pytest.mark.asyncio
    async def test_search_arxiv_total_vs_result_count(self, monkeypatch):
        # total_results is arXiv's upstream match count; result_count is
        # how many hits this page returned. They are NOT the same number.
        async def fake_search(query, max_results=10):
            return {
                "total_results": 50000,
                "entries": [
                    {
                        "id": "http://arxiv.org/abs/2301.00001v1",
                        "title": "One of many",
                        "published": "2023-01-01T00:00:00Z",
                        "authors": [{"name": "Jane Doe"}],
                    },
                ],
            }

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        result = await server.search_arxiv("anything")
        assert result["total_results"] == 50000
        assert result["result_count"] == 1

    @pytest.mark.asyncio
    async def test_search_crossref_total_vs_result_count(self, monkeypatch):
        # Crossref exposes message.total-results upstream; the tool surfaces
        # it as total_results (NOT len of the returned page) plus a
        # result_count for the page size, matching search_arxiv.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["One of many"],
                        "author": [{"given": "Jane", "family": "Doe"}],
                        "published-online": {"date-parts": [[2023]]},
                    }
                ],
                "total_results": 312,
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["total_results"] == 312
        assert result["result_count"] == 1

    @pytest.mark.asyncio
    async def test_search_crossref_date_parts_none(self, monkeypatch):
        # Malformed Crossref record: date-parts present but null. Must not
        # crash (TypeError on None[0]) — degrade to year=None.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["Null date-parts"],
                        "published-online": {"date-parts": None},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["year"] is None

    @pytest.mark.asyncio
    async def test_search_crossref_date_parts_empty(self, monkeypatch):
        # Malformed Crossref record: date-parts is an empty list. Must not
        # crash (IndexError on [][0]) — degrade to year=None.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["Empty date-parts"],
                        "published-print": {"date-parts": []},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["year"] is None

    @pytest.mark.asyncio
    async def test_search_crossref_year_from_posted(self, monkeypatch):
        # bioRxiv/preprint records carry their date under `posted`, not
        # published-print/-online. The de-facto bioRxiv search must still
        # surface the year.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1101/2024.05.01.123",
                        "title": ["A preprint"],
                        "author": [{"given": "Jane", "family": "Doe"}],
                        "posted": {"date-parts": [[2024, 5]]},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["year"] == 2024

    @pytest.mark.asyncio
    async def test_search_crossref_org_first_author(self, monkeypatch):
        # Consortium authors carry a `name` field with no given/family.
        # first_author must surface it instead of dropping to None.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["Big collaboration paper"],
                        "author": [{"name": "The ATLAS Collaboration"}],
                        "published-online": {"date-parts": [[2023]]},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["first_author"] == "The ATLAS Collaboration"
        assert result["results"][0]["author_count"] == 1

    @pytest.mark.asyncio
    async def test_search_crossref_by_title_includes_author_count(self, monkeypatch):
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["Some title"],
                        "author": [
                            {"given": "Jane", "family": "Doe"},
                            {"given": "John", "family": "Roe"},
                            {"given": "Alice", "family": "Smith"},
                        ],
                        "published-online": {"date-parts": [[2023]]},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["author_count"] == 3
        assert result["results"][0]["first_author"] == "Jane Doe"

    @pytest.mark.asyncio
    async def test_search_crossref_missing_author_field(self, monkeypatch):
        # Some Crossref records omit the author array entirely. The
        # slim tool must report 0 instead of NoneType / KeyError.
        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["No-author edge case"],
                        "published-online": {"date-parts": [[2023]]},
                    }
                ],
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert result["results"][0]["author_count"] == 0
        assert result["results"][0]["first_author"] is None


class TestSearchParameterThreading:
    """Every search parameter has to reach the provider. Six of them were
    accepted at the MCP boundary and asserted nowhere, so a mis-wired keyword
    would have shipped green.
    """

    @pytest.mark.asyncio
    async def test_search_arxiv_threads_max_results_at_both_bounds(self, monkeypatch):
        seen = []

        async def fake_search(query, max_results=10):
            seen.append(max_results)
            return {"total_results": 0, "entries": []}

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        await server.search_arxiv("x", max_results=1)
        await server.search_arxiv("x", max_results=arxiv.MAX_SEARCH_RESULTS)
        assert seen == [1, arxiv.MAX_SEARCH_RESULTS]

    @pytest.mark.asyncio
    async def test_search_crossref_threads_year_and_max_results(self, monkeypatch):
        seen = {}

        async def fake_search(bibliographic, year=None, rows=5):
            seen.update(bibliographic=bibliographic, year=year, rows=rows)
            return {"items": [], "total_results": 0}

        monkeypatch.setattr(crossref, "search_works", fake_search)

        await server.search_crossref_by_title("attention", year=2017, max_results=20)
        assert seen == {"bibliographic": "attention", "year": 2017, "rows": 20}

    @pytest.mark.asyncio
    async def test_the_crossref_bound_is_the_provider_constant(self):
        # The cap belongs to crossref, not to a number transcribed here.
        field = server.search_crossref_by_title.__annotations__["max_results"].__metadata__[0]
        assert field.metadata[1].le == crossref.MAX_SEARCH_ROWS

    @pytest.mark.asyncio
    async def test_search_crossref_defaults_to_five_rows(self, monkeypatch):
        seen = []

        async def fake_search(bibliographic, year=None, rows=5):
            seen.append(rows)
            return {"items": [], "total_results": 0}

        monkeypatch.setattr(crossref, "search_works", fake_search)

        await server.search_crossref_by_title("anything")
        assert seen == [5]


class TestSearchErrorContract:
    """An errored search never leaks a partial result list, and its advice
    must not argue with the retry verdict it rides beside.
    """

    @pytest.mark.asyncio
    async def test_a_rejected_arxiv_query_is_told_to_rewrite_not_retry(self, monkeypatch):
        # arXiv answers a malformed query with 200 + an api/errors entry, which
        # the provider classifies `retryable: False`. Advising a retry sends the
        # agent back at a call that cannot succeed.
        async def fake_search(query, max_results=10):
            return {"error": "arXiv rejected the search query: ti:", "retryable": False}

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        result = await server.search_arxiv("ti:")
        assert "Rewrite the query" in result["suggestion"]
        assert "temporarily unavailable" not in result["suggestion"]
        assert "results" not in result and "result_count" not in result

    @pytest.mark.asyncio
    async def test_a_transient_arxiv_failure_is_told_to_retry(self, monkeypatch):
        async def fake_search(query, max_results=10):
            return {"error": "arXiv request timed out", "retryable": True}

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        result = await server.search_arxiv("anything")
        assert "temporarily unavailable" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_a_crossref_error_points_at_the_preprint_search(self, monkeypatch):
        async def fake_search(bibliographic, year=None, rows=5):
            return {"error": "Crossref unavailable", "retryable": True}

        monkeypatch.setattr(crossref, "search_works", fake_search)

        result = await server.search_crossref_by_title("anything")
        assert "search_arxiv" in result["suggestion"]
        assert "results" not in result and "result_count" not in result


class TestSearchHitFields:
    """The triage hit's chainable handles and its year."""

    @pytest.mark.asyncio
    async def test_the_hit_carries_the_bare_arxiv_id(self, monkeypatch):
        # `arxiv_id` is what the docstring's "free cache hit" promise rests on;
        # it must be the bare versioned id, not the Atom URL.
        async def fake_search(query, max_results=10):
            return {
                "total_results": 1,
                "entries": [
                    {
                        "id": "http://arxiv.org/abs/2301.00001v1",
                        "title": "T",
                        "published": "2023-04-05T00:00:00Z",
                        "authors": [{"name": "Jane Doe"}],
                    }
                ],
            }

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        hit = (await server.search_arxiv("anything"))["results"][0]
        assert hit["arxiv_id"] == "2301.00001v1"
        assert hit["published_year"] == 2023

    @pytest.mark.asyncio
    async def test_a_superscript_year_degrades_instead_of_raising(self, monkeypatch):
        """`"²⁰²³".isdigit()` is True and `int()` on it raises ValueError.

        `arxiv.search_papers` documents avoiding the same trap when parsing
        `totalResults`; the year parse must agree with it.
        """

        async def fake_search(query, max_results=10):
            return {
                "total_results": 1,
                "entries": [
                    {
                        "id": "http://arxiv.org/abs/2301.1",
                        "title": "T",
                        "published": "\u00b2\u2070\u00b2\u00b3-01-01",
                        "authors": [],
                    }
                ],
            }

        monkeypatch.setattr(arxiv, "search_papers", fake_search)

        hit = (await server.search_arxiv("anything"))["results"][0]
        assert hit["published_year"] is None

    @pytest.mark.asyncio
    async def test_a_non_dict_crossref_author_row_is_filtered_not_fatal(self, monkeypatch):
        """`search_works` types the items but not their `author` rows.

        A bare-string row used to reach `.get()` as an AttributeError, and the
        wrong-shape body is positive-cached for the full TTL — so every call
        raised until the entry expired.
        """

        async def fake_search(bibliographic, year=None, rows=5):
            return {
                "items": [
                    {
                        "DOI": "10.1234/x",
                        "title": ["Mixed rows"],
                        "author": ["Jane Doe", {"given": "John", "family": "Roe"}],
                    }
                ],
                "total_results": 1,
            }

        monkeypatch.setattr(crossref, "search_works", fake_search)

        hit = (await server.search_crossref_by_title("anything"))["results"][0]
        # Counted the same list the name came from: one usable row, not two.
        assert hit["first_author"] == "John Roe"
        assert hit["author_count"] == 1

    @pytest.mark.asyncio
    async def test_an_absent_upstream_total_is_zero_on_both_tools(self, monkeypatch):
        # Crossref omits `total-results` on some responses. The key means the
        # same thing on both tools or an agent cannot branch on it.
        async def fake_arxiv(query, max_results=10):
            return {"entries": []}

        async def fake_crossref(bibliographic, year=None, rows=5):
            return {"items": []}

        monkeypatch.setattr(arxiv, "search_papers", fake_arxiv)
        monkeypatch.setattr(crossref, "search_works", fake_crossref)

        assert (await server.search_arxiv("x"))["total_results"] == 0
        assert (await server.search_crossref_by_title("x"))["total_results"] == 0


# ---------------------------------------------------------------------------
# find_in_paper: error contract + happy path
# ---------------------------------------------------------------------------


class TestFindInPaper:
    """find_in_paper scans one converted paper's markdown. It must error
    cleanly (with a suggestion, like every other tool here) when the paper
    isn't converted, and return positioned hits when it is.
    """

    @pytest.fixture
    def isolated_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / ".cache")
        return tmp_path / ".cache"

    @pytest.mark.asyncio
    async def test_unconverted_paper_errors_with_suggestion(self, isolated_cache):
        # No markdown on disk for this identifier — the response must carry
        # both an `error` and a `suggestion`, matching the {error, suggestion}
        # contract the rest of the search tools honour.
        result = await server.find_in_paper("2301.00001", "anything")
        assert "error" in result
        assert "suggestion" in result

    @pytest.mark.asyncio
    async def test_finds_substring_in_converted_paper(self, isolated_cache):
        # Seed a converted-markdown file at the path find_in_paper resolves
        # to, then assert it locates the query and returns positioned hits.
        target = manual.resolve_target("2301.00001")
        md_path = papers.markdown_path(target["namespace"], target["canonical"])
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# Title\n\n## Introduction\n\nWe study variational dropout here.\n")

        result = await server.find_in_paper("2301.00001", "variational dropout")
        assert result["result_count"] == 1
        assert result["truncated"] is False
        hit = result["results"][0]
        assert hit["section"] == "Introduction"
        assert "char_offset" in hit

    @pytest.mark.asyncio
    async def test_case_sensitive_and_whole_words_reach_the_scanner(self, isolated_cache):
        # Both flags were exercised only against papers.find_in_markdown, so
        # the kwarg wiring through the tool was unverified.
        target = manual.resolve_target("2301.00002")
        md_path = papers.markdown_path(target["namespace"], target["canonical"])
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# T\n\n## Intro\n\nA subset of the Set of sets.\n")

        loose = await server.find_in_paper("2301.00002", "set")
        assert loose["result_count"] == 3  # subset, Set, sets

        whole = await server.find_in_paper("2301.00002", "set", whole_words=True)
        assert [h["match"] for h in whole["results"]] == ["Set"]

        cased = await server.find_in_paper("2301.00002", "Set", case_sensitive=True)
        assert [h["match"] for h in cased["results"]] == ["Set"]

    @pytest.mark.asyncio
    async def test_the_echoed_identifier_is_canonical(self, isolated_cache):
        """One markdown file must not answer to two identities.

        Every other tool that echoes an identifier canonicalizes it first —
        `_canonical_id` on the paper family, `doi` on the graph tools.
        """
        target = manual.resolve_target("2301.00003v2")
        md_path = papers.markdown_path(target["namespace"], target["canonical"])
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# T\n\n## Intro\n\ndropout.\n")

        for spelling in ("2301.00003v2", "arXiv:2301.00003v2"):
            result = await server.find_in_paper(spelling, "dropout")
            assert result["paper_identifier"] == "2301.00003v2"

    @pytest.mark.asyncio
    async def test_truncated_is_true_when_more_matches_exist(self, isolated_cache):
        target = manual.resolve_target("2301.00004")
        md_path = papers.markdown_path(target["namespace"], target["canonical"])
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("# T\n\n## Intro\n\ndropout dropout dropout.\n")

        capped = await server.find_in_paper("2301.00004", "dropout", max_results=2)
        assert capped["result_count"] == 2
        assert capped["truncated"] is True
