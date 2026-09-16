"""Tests for the Papers with Code tools in ``tools/catalog.py``.

The provider is patched at the module object, so these cover the tool layer only:
slices, envelopes, suggestions and bounds.
"""

import inspect
import re
from typing import Any

import pytest

from academic_tools_mcp import server
from academic_tools_mcp.providers import paperswithcode
from academic_tools_mcp.tools import catalog

_RECORD: dict[str, Any] = {
    "pwc_id": "755",
    "arxiv_id": "1706.03762",
    "title": "Attention Is All You Need",
    "published": "2017-06-12",
    "tldr": "Transformers.",
    "authors": [
        {"name": "Ashish Vaswani", "hf_username": "essentialite"},
        {"name": "Noam Shazeer", "hf_username": None},
    ],
    "conference": "NeurIPS 2017",
    "conference_url": None,
    "conference_award": None,
    "has_official_implementation": True,
    "repository_count": 4,
    "repositories": [
        {"url": "u-few", "owner": None, "name": None, "stars": 5, "is_official": False},
        {"url": "u-none", "owner": None, "name": None, "stars": None, "is_official": False},
        {"url": "u-many", "owner": None, "name": None, "stars": 900, "is_official": False},
        {"url": "u-official", "owner": None, "name": None, "stars": 1, "is_official": True},
    ],
    "project_pages": [],
    "hf_models": [],
    "hf_datasets": [],
    "hf_spaces": [],
    "hf_artifact_counts": {"models": None, "datasets": None, "spaces": None},
    "tasks": [{"name": "Machine Translation", "slug": "machine-translation"}],
    "methods": [],
    "introduced_benchmarks": [],
    "introduced_frameworks": [],
    "leaderboard_ranks": [],
    "organizations": [],
    "predecessors": [],
    "successors": [],
}

_ROW = {
    "model": "Transformer",
    "task": "MT",
    "task_slug": "mt",
    "benchmark": "WMT14",
    "benchmark_slug": "wmt14",
    "metrics": {"BLEU": 28.4},
    "best_metric": "BLEU",
    "rank": 1,
    "num_parameters": None,
    "is_open": True,
    "uses_additional_data": False,
    "harness": None,
    "evaluated_on": None,
    "paper_title": "Attention Is All You Need",
    "paper_arxiv_id": "1706.03762",
    "code_url": None,
    "hf_model_url": None,
    "source_url": None,
}


def _patch_paper(monkeypatch, result):
    async def fake(arxiv_id, *, force_refresh=False):
        return dict(result)

    monkeypatch.setattr(paperswithcode, "get_paper", fake)


class TestGetPaperCode:
    @pytest.mark.asyncio
    async def test_ranks_official_first_then_stars_and_truncates(self, monkeypatch):
        _patch_paper(monkeypatch, _RECORD)

        result = await server.get_paper_code("arXiv:1706.03762v7", max_repositories=3)

        assert [r["url"] for r in result["repositories"]] == ["u-official", "u-many", "u-few"]
        assert result["repositories_truncated"] is True
        assert result["repository_count"] == 4
        assert result["arxiv_id"] == "1706.03762"
        assert result["pwc_url"] == "https://paperswithcode.co/paper/1706.03762"

    @pytest.mark.asyncio
    async def test_upstream_total_beyond_the_list_still_reads_truncated(self, monkeypatch):
        _patch_paper(monkeypatch, {**_RECORD, "repository_count": 595})

        result = await server.get_paper_code("1706.03762", max_repositories=50)

        assert len(result["repositories"]) == 4
        assert result["repositories_truncated"] is True

    @pytest.mark.asyncio
    async def test_a_missing_total_falls_back_to_the_list(self, monkeypatch):
        _patch_paper(monkeypatch, {**_RECORD, "repository_count": None})

        result = await server.get_paper_code("1706.03762", max_repositories=50)

        assert result["repository_count"] == 4
        assert result["repositories_truncated"] is False

    @pytest.mark.asyncio
    async def test_a_doi_gets_the_arxiv_suggestion(self, monkeypatch):
        result = await server.get_paper_code("10.1234/example")

        assert result["not_found"] is True
        assert "get_paper_metadata" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_an_uncatalogued_paper_points_at_search(self, monkeypatch):
        _patch_paper(monkeypatch, {"error": "none", "not_found": True})

        result = await server.get_paper_code("2301.00001")

        assert "search_paperswithcode" in result["suggestion"]

    @pytest.mark.asyncio
    async def test_a_rate_limit_says_stop_calling(self, monkeypatch):
        _patch_paper(monkeypatch, {"error": "429", "retryable": True, "retry_after_seconds": 30})

        result = await server.get_paper_code("1706.03762")

        assert "retry_after_seconds" in result["suggestion"]


class TestGetPaperCatalog:
    @pytest.mark.asyncio
    async def test_hf_authors_keeps_only_linked_accounts(self, monkeypatch):
        _patch_paper(monkeypatch, _RECORD)

        result = await server.get_paper_catalog("1706.03762")

        assert result["hf_authors"] == [{"name": "Ashish Vaswani", "hf_username": "essentialite"}]
        assert "repositories" not in result


class TestPagedTools:
    @pytest.mark.asyncio
    async def test_paper_evaluations_drop_the_redundant_paper_fields(self, monkeypatch):
        _patch_paper(monkeypatch, _RECORD)

        async def fake(arxiv_id, *, page, page_size, force_refresh):
            return {"pwc_id": "755", "total_results": 3, "next_page": 2, "evaluations": [_ROW]}

        monkeypatch.setattr(paperswithcode, "get_paper_evaluations", fake)

        result = await server.get_paper_evaluations("1706.03762", page=1, page_size=1)

        assert result["has_more"] is True and result["total_results"] == 3
        assert "paper_title" not in result["evaluations"][0]
        assert result["evaluations"][0]["benchmark"] == "WMT14"

    @pytest.mark.asyncio
    async def test_leaderboard_drops_the_redundant_benchmark_fields(self, monkeypatch):
        async def fake(benchmark, *, page, page_size, is_open, force_refresh):
            return {
                "benchmark": {"name": "WMT14"},
                "total_results": None,
                "next_page": None,
                "evaluations": [_ROW],
            }

        monkeypatch.setattr(paperswithcode, "get_leaderboard", fake)

        result = await server.get_benchmark_leaderboard("wmt14")

        assert result["total_results"] == 0 and result["has_more"] is False
        assert "benchmark" not in result["evaluations"][0]
        assert result["evaluations"][0]["paper_arxiv_id"] == "1706.03762"

    @pytest.mark.asyncio
    async def test_search_envelope(self, monkeypatch):
        async def fake(query, **kwargs):
            return {"next_page": 2, "results": [{"title": "Hit"}]}

        monkeypatch.setattr(paperswithcode, "search", fake)

        result = await server.search_paperswithcode("attention")

        assert result == {
            "page": 1,
            "result_count": 1,
            "has_more": True,
            "results": [{"title": "Hit"}],
        }

    @pytest.mark.asyncio
    async def test_task_not_found_suggests_the_catalog(self, monkeypatch):
        async def fake(task, *, force_refresh=False):
            return {"error": "none", "not_found": True}

        monkeypatch.setattr(paperswithcode, "get_task", fake)

        result = await server.get_pwc_task("nope")

        assert "get_paper_catalog" in result["suggestion"]


_TOOLS = (
    catalog.get_paper_code,
    catalog.get_paper_catalog,
    catalog.get_paper_evaluations,
    catalog.get_pwc_task,
    catalog.get_benchmark_leaderboard,
    catalog.search_paperswithcode,
)


@pytest.mark.parametrize("tool", _TOOLS, ids=lambda t: t.__name__)
def test_bounds_bind_to_the_provider_constants(tool):
    checked = 0
    signature = inspect.signature(tool)
    for name, param in signature.parameters.items():
        for meta in getattr(param.annotation, "__metadata__", ()):
            le = next((m.le for m in getattr(meta, "metadata", ()) if getattr(m, "le", None)), None)
            if le is None:
                continue
            expected = {
                "page": paperswithcode.MAX_PAGE,
                "page_size": paperswithcode.MAX_PAGE_SIZE,
                "max_repositories": paperswithcode.MAX_REPOSITORIES,
            }[name]
            assert le == expected, name
            checked += 1
    # get_pwc_task and get_paper_catalog take no bounded parameter.
    assert checked or tool in (catalog.get_pwc_task, catalog.get_paper_catalog)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_paper_code", "get_paper_catalog"])
async def test_every_returned_key_is_named_in_the_docstring(monkeypatch, name):
    _patch_paper(monkeypatch, _RECORD)
    tool = getattr(catalog, name)

    result = await tool("1706.03762")

    doc = inspect.getdoc(tool) or ""
    for key in result:
        assert re.search(rf"\b{re.escape(key)}\b", doc), f"{name} returns undocumented {key}"
