"""Tests for the Papers with Code client: slugs, parsing, caching and politeness."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from academic_tools_mcp.net import clients, stats
from academic_tools_mcp.providers import paperswithcode as pwc
from academic_tools_mcp.store import cache

_BAD_JSON = object()


def _stub(monkeypatch: pytest.MonkeyPatch, *payloads: Any) -> list[httpx.Request]:
    """Serve ``payloads`` from successive GETs, returning the captured requests.

    A payload may be ``_BAD_JSON``, a ``(status, payload)`` or ``(status, payload,
    headers)`` tuple, or an exception to raise; the last one repeats.
    """
    requests: list[httpx.Request] = []
    seq = list(payloads)

    def respond(request: httpx.Request) -> httpx.Response:
        payload = seq[len(requests)] if len(requests) < len(seq) else seq[-1]
        requests.append(request)
        if isinstance(payload, Exception):
            raise payload
        status, headers = 200, {}
        if isinstance(payload, tuple):
            status, payload, *rest = payload
            headers = rest[0] if rest else {}
        body = b"{ truncated" if payload is _BAD_JSON else json.dumps(payload).encode()
        return httpx.Response(
            status, headers={"content-type": "application/json", **headers}, content=body
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(pwc._throttle, "min_gap_seconds", 0.0)
    monkeypatch.setattr(pwc._search_gap, "min_gap_seconds", 0.0)
    monkeypatch.setattr(clients, "get_client", lambda *a, **kw: client)
    return requests


_PAPER = {
    "id": "755",
    "arxiv_id": "1706.03762",
    "title": "Attention Is All You Need",
    "published": "2017-06-12",
    "tldr": "Transformers.",
    "authors": ["Ashish Vaswani", "Noam Shazeer"],
    "author_links": [
        {"id": "417", "name": "Ashish Vaswani", "hf_username": "essentialite"},
        {"id": "420", "name": "Noam Shazeer", "hf_username": None},
    ],
    "proceeding": "NeurIPS 2017 12",
    "conference_name": None,
    "conference_url_abs": "http://papers.nips.cc/paper/7181",
    "has_official_implementation": True,
    "code_repository_count": 595,
    "repositories": [
        {
            "url": "https://github.com/tensorflow/tensor2tensor",
            "owner": "tensorflow",
            "name": "tensor2tensor",
            "num_stars": 17472,
            "is_official": True,
        },
        {"url": "https://github.com/huggingface/transformers", "num_stars": 166260},
    ],
    "project_pages": None,
    "hf_models": [],
    "hf_artifact_summary": {"model_count": 3, "dataset_count": 0, "space_count": 1},
    "tasks": [{"id": "6", "name": "Machine Translation", "slug": "machine-translation"}],
    "methods": [{"id": "50", "name": "Adam", "slug": "adam", "introduced_year": 2014}],
    "introduced_benchmarks": [{"id": "226", "name": "WMT14 En-De", "slug": "wmt14"}],
    "leaderboard_ranks": [
        {"dataset_name": "PTB", "dataset_slug": "ptb", "best_rank": 1, "best_metric": "F1"}
    ],
    "predecessor_papers": None,
    "successor_papers": [{"id": "9", "title": "Next", "arxiv_id": "1807.00001"}],
}

_EVALUATIONS = {
    "count": 3,
    "next_page": 2,
    "results": [
        {
            "model_name": "Transformer (big)",
            "task_name": "Machine Translation",
            "dataset_name": "WMT14 En-Fr",
            "metrics": {"BLEU": "41.8", "nested": {"no": 1}},
            "best_rank": 2,
            "is_open": True,
            "result_url": "https://example.org/result",
        }
    ],
}


# ---------------------------------------------------------------------------
# canonical_slug
# ---------------------------------------------------------------------------


class TestCanonicalSlug:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Machine Translation", "machine-translation"),
            ("machine-translation", "machine-translation"),
            ("WMT 2014 English->German (newstest2014)", "wmt-2014-english-german-newstest2014"),
            ("226", "226"),
            ("Métodos Análisis", "metodos-analisis"),
            ("  ../..  ", ""),
        ],
    )
    def test_canonical_slug(self, value, expected):
        assert pwc.canonical_slug(value) == expected


# ---------------------------------------------------------------------------
# get_paper
# ---------------------------------------------------------------------------


class TestGetPaper:
    @pytest.mark.asyncio
    async def test_parses_and_requests_resources(self, monkeypatch):
        requests = _stub(monkeypatch, _PAPER)

        paper = await pwc.get_paper("arXiv:1706.03762v7")

        assert requests[0].url.path == "/api/v1/papers/arxiv/1706.03762"
        assert requests[0].url.params["include_resources"] == "true"
        assert paper["pwc_id"] == "755"
        assert paper["conference"] == "NeurIPS 2017 12"
        assert paper["repository_count"] == 595
        assert paper["repositories"][1] == {
            "url": "https://github.com/huggingface/transformers",
            "owner": None,
            "name": None,
            "stars": 166260,
            "is_official": False,
        }
        assert paper["authors"][0] == {"name": "Ashish Vaswani", "hf_username": "essentialite"}
        assert paper["hf_artifact_counts"] == {"models": 3, "datasets": 0, "spaces": 1}
        assert paper["project_pages"] == []
        assert paper["predecessors"] == []
        assert paper["successors"] == [{"arxiv_id": "1807.00001", "title": "Next"}]

    @pytest.mark.asyncio
    async def test_old_style_id_keeps_its_slash_inside_one_segment(self, monkeypatch):
        requests = _stub(monkeypatch, _PAPER)

        await pwc.get_paper("cs.DS/0610046")

        assert requests[0].url.raw_path.startswith(b"/api/v1/papers/arxiv/cs.DS%2F0610046")

    @pytest.mark.asyncio
    async def test_every_spelling_shares_one_request(self, monkeypatch):
        requests = _stub(monkeypatch, _PAPER)

        await pwc.get_paper("1706.03762")
        await pwc.get_paper("1706.03762v3")
        await pwc.get_paper("https://arxiv.org/abs/1706.03762")

        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_a_non_arxiv_id_costs_no_request_and_is_not_cached(self, monkeypatch):
        requests = _stub(monkeypatch, _PAPER)

        result = await pwc.get_paper("10.1234/example")

        assert result["not_found"] is True
        assert requests == []
        assert cache.get_negative(pwc.NAMESPACE, "papers", "10.1234/example") is None

    @pytest.mark.asyncio
    async def test_404_is_negative_cached(self, monkeypatch):
        requests = _stub(monkeypatch, (404, {"detail": "Not found"}))

        first = await pwc.get_paper("2301.00001")
        second = await pwc.get_paper("2301.00001")

        assert first["not_found"] is True and second["not_found"] is True
        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_force_refresh_clears_a_negative(self, monkeypatch):
        requests = _stub(monkeypatch, (404, {"detail": "Not found"}), _PAPER)

        await pwc.get_paper("1706.03762")
        paper = await pwc.get_paper("1706.03762", force_refresh=True)

        assert paper["title"] == "Attention Is All You Need"
        assert len(requests) == 2

    @pytest.mark.asyncio
    async def test_positive_ttl_evicts(self, monkeypatch):
        requests = _stub(monkeypatch, _PAPER)
        await pwc.get_paper("1706.03762")
        monkeypatch.setattr(pwc, "_POSITIVE_TTL_SECONDS", -1.0)

        await pwc.get_paper("1706.03762")

        assert len(requests) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            _BAD_JSON,
            None,
            [],
            "a string",
            {"id": "1"},  # no title
            {"title": "No id"},
            {"id": True, "title": "bool id"},
        ],
    )
    async def test_a_wrong_shape_is_retryable_and_uncached(self, monkeypatch, body):
        requests = _stub(monkeypatch, body)

        first = await pwc.get_paper("1706.03762")
        await pwc.get_paper("1706.03762")

        assert first["retryable"] is True
        assert "not_found" not in first
        assert len(requests) == 2

    @pytest.mark.asyncio
    async def test_garbage_below_the_top_level_degrades(self, monkeypatch):
        _stub(
            monkeypatch,
            {
                "id": 755,
                "title": "T",
                "repositories": [None, "x", {"url": 5}, {"url": "u", "num_stars": True}],
                "tasks": "not a list",
                "author_links": [{"name": None}],
                "hf_artifact_summary": [],
                "hf_models": ["https://hf.co/m", 3],
            },
        )

        paper = await pwc.get_paper("1706.03762")

        assert paper["pwc_id"] == "755"
        assert paper["repositories"] == [
            {"url": "u", "owner": None, "name": None, "stars": None, "is_official": False}
        ]
        assert paper["tasks"] == [] and paper["authors"] == []
        assert paper["hf_models"] == ["https://hf.co/m"]


# ---------------------------------------------------------------------------
# Rate-limit handling
# ---------------------------------------------------------------------------


class TestRateLimits:
    @pytest.mark.asyncio
    async def test_429_is_not_retried_and_carries_retry_after(self, monkeypatch):
        requests = _stub(monkeypatch, (429, {"detail": "slow down"}, {"retry-after": "30"}))

        result = await pwc.get_paper("1706.03762")

        assert result["retryable"] is True
        assert result["retry_after_seconds"] == 30
        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_after_a_429_every_call_is_refused_locally(self, monkeypatch):
        requests = _stub(monkeypatch, (429, {"detail": "slow down"}, {"retry-after": "30"}), _PAPER)

        await pwc.get_paper("1706.03762")
        refused = await pwc.get_task("machine-translation")
        searched = await pwc.search("attention")

        assert len(requests) == 1
        for result in (refused, searched):
            assert result["quota_exhausted"] is True
            assert result["retryable"] is True
            assert 0 < result["retry_after_seconds"] <= 30
        assert stats.snapshot()["providers"][pwc.NAMESPACE]["quota_refusals"] == 2

    @pytest.mark.asyncio
    async def test_a_429_is_not_cached(self, monkeypatch):
        _stub(monkeypatch, (429, {}, {"retry-after": "30"}))

        await pwc.get_paper("1706.03762")

        assert cache.get(pwc.NAMESPACE, "papers", "1706.03762") is None
        assert cache.get_negative(pwc.NAMESPACE, "papers", "1706.03762") is None

    @pytest.mark.asyncio
    async def test_backpressure_past_max_pending(self, monkeypatch):
        _stub(monkeypatch, _PAPER)
        monkeypatch.setattr(pwc._throttle, "pending", pwc._MAX_PENDING)

        result = await pwc.get_paper("1706.03762")

        assert result["backpressure"] is True
        assert result["max_concurrency"] == pwc._MAX_PENDING

    @pytest.mark.asyncio
    async def test_list_endpoints_pass_the_search_gate(self, monkeypatch):
        _stub(monkeypatch, {"next_page": None, "results": []})
        monkeypatch.setattr(pwc._search_gap, "min_gap_seconds", 5.0)
        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        await pwc.search("one")
        await pwc.search("two")

        assert len(slept) == 1 and 0 < slept[0] <= 5.0

    @pytest.mark.asyncio
    async def test_detail_endpoints_skip_the_search_gate(self, monkeypatch):
        _stub(monkeypatch, _PAPER)
        called = False

        async def gate(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("a detail lookup went through the search gate")

        monkeypatch.setattr(pwc, "_throttled_search_get", gate)

        await pwc.get_paper("1706.03762")

        assert called is False


# ---------------------------------------------------------------------------
# Evaluations, tasks, benchmarks, search
# ---------------------------------------------------------------------------


# A parsed ``get_paper`` record, as the evaluations tool hands it over.
_RECORD = pwc._paper_of(_PAPER)


class TestEvaluations:
    @pytest.mark.asyncio
    async def test_pages_on_the_records_numeric_id(self, monkeypatch):
        requests = _stub(monkeypatch, _EVALUATIONS)

        result = await pwc.get_paper_evaluations(_RECORD, page=1, page_size=5)

        assert requests[0].url.path == "/api/v1/evaluations/"
        assert requests[0].url.params["paper_id"] == "755"
        assert requests[0].url.params["page_size"] == "5"
        assert result["total_results"] == 3 and result["next_page"] == 2
        row = result["evaluations"][0]
        assert row["metrics"] == {"BLEU": "41.8"}
        assert row["rank"] == 2
        assert row["source_url"] == "https://example.org/result"

    @pytest.mark.asyncio
    async def test_a_cached_page_costs_nothing(self, monkeypatch):
        requests = _stub(monkeypatch, _EVALUATIONS)

        await pwc.get_paper_evaluations(_RECORD)
        await pwc.get_paper_evaluations(_RECORD)

        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_pages_cache_apart(self, monkeypatch):
        requests = _stub(monkeypatch, _EVALUATIONS)

        await pwc.get_paper_evaluations(_RECORD, page=1)
        await pwc.get_paper_evaluations(_RECORD, page=2)

        assert len(requests) == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body", [{"results": "x"}, {"results": [1, 2]}, [], {"count": 3}, _BAD_JSON]
    )
    async def test_a_wrong_shape_page_is_retryable(self, monkeypatch, body):
        _stub(monkeypatch, body)

        result = await pwc.get_paper_evaluations(_RECORD)

        assert result["retryable"] is True

    @pytest.mark.asyncio
    async def test_an_empty_page_is_a_real_answer(self, monkeypatch):
        _stub(monkeypatch, {"count": 0, "next_page": None, "results": []})

        result = await pwc.get_paper_evaluations(_RECORD)

        assert result["evaluations"] == [] and result["total_results"] == 0


class TestTaskAndBenchmark:
    @pytest.mark.asyncio
    async def test_task_by_display_name(self, monkeypatch):
        requests = _stub(
            monkeypatch,
            {"id": "6", "name": "Machine Translation", "level": 0, "research_trends": ["a", 1]},
        )

        task = await pwc.get_task("Machine Translation")

        assert requests[0].url.path == "/api/v1/tasks/machine-translation"
        assert task["task_id"] == "6" and task["research_trends"] == ["a"]

    @pytest.mark.asyncio
    async def test_an_empty_slug_costs_no_request(self, monkeypatch):
        requests = _stub(monkeypatch, {"id": "6", "name": "x"})

        result = await pwc.get_task(" ?? ")

        assert result["not_found"] is True and requests == []

    @pytest.mark.asyncio
    async def test_leaderboard_resolves_then_pages_best_rank_first(self, monkeypatch):
        requests = _stub(
            monkeypatch,
            {"id": "226", "name": "WMT14", "introducing_paper": {"title": "T", "arxiv_id": "1"}},
            _EVALUATIONS,
        )

        result = await pwc.get_leaderboard("wmt14", page=2, page_size=3, is_open=True)

        assert requests[0].url.path == "/api/v1/datasets/wmt14"
        params = requests[1].url.params
        assert params["dataset_id"] == "226" and params["ordering"] == "best_rank"
        assert params["is_open"] == "true" and params["page"] == "2"
        assert result["benchmark"]["introducing_paper"] == {"title": "T", "arxiv_id": "1"}
        assert len(result["evaluations"]) == 1

    @pytest.mark.asyncio
    async def test_is_open_omitted_when_unset(self, monkeypatch):
        requests = _stub(monkeypatch, {"id": "226", "name": "WMT14"}, _EVALUATIONS)

        await pwc.get_leaderboard("wmt14")

        assert "is_open" not in requests[1].url.params

    @pytest.mark.asyncio
    async def test_unknown_benchmark_is_negative_cached(self, monkeypatch):
        requests = _stub(monkeypatch, (404, {}))

        await pwc.get_leaderboard("nope")
        result = await pwc.get_leaderboard("nope")

        assert result["not_found"] is True and len(requests) == 1


class TestSearch:
    @pytest.mark.asyncio
    async def test_sends_keyword_mode_and_filters(self, monkeypatch):
        requests = _stub(
            monkeypatch,
            {
                "next_page": 2,
                "results": [
                    {"title": "Hit", "arxiv_id": "1706.03762", "authors": ["A", "B"]},
                    {"title": None},
                ],
            },
        )

        result = await pwc.search(
            "  attention   is all ",
            has_official_implementation=True,
            start_date="2017-01-01",
        )

        params = requests[0].url.params
        assert params["mode"] == "keyword" and params["q"] == "attention is all"
        assert params["has_official_implementation"] == "true"
        assert params["start_date"] == "2017-01-01" and "end_date" not in params
        assert result["next_page"] == 2
        assert result["results"] == [
            {
                "arxiv_id": "1706.03762",
                "title": "Hit",
                "first_author": "A",
                "author_count": 2,
                "published": None,
                "has_official_implementation": None,
                "repository_count": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_repeat_queries_are_cached_by_normalized_params(self, monkeypatch):
        requests = _stub(monkeypatch, {"next_page": None, "results": []})

        await pwc.search("attention")
        await pwc.search("  attention ")

        assert len(requests) == 1

    @pytest.mark.asyncio
    async def test_hits_do_not_warm_the_paper_cache(self, monkeypatch):
        _stub(
            monkeypatch,
            {"next_page": None, "results": [{"title": "Hit", "arxiv_id": "1706.03762"}]},
        )

        await pwc.search("attention")

        assert cache.get(pwc.NAMESPACE, "papers", "1706.03762") is None
