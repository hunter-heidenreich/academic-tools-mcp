"""Papers with Code tools: code, catalog, evaluations, tasks, leaderboards, search.

Dedicated tools rather than a new ``_source``: none of this data has a counterpart in
the unified paper tools.
"""

from typing import Annotated, Any

from pydantic import Field

from ..app import (
    ARXIV_ID,
    FORCE_REFRESH,
    READ_ONLY_NETWORK_ANNOTATIONS,
    enrich_error,
    mcp,
    openalex_arxiv_id,
    resolve_paper_identifier,
)
from ..providers import arxiv, openalex, paperswithcode
from ..util import doinorm

PWC_PAGE = Annotated[
    int,
    Field(description="Page number, from 1.", ge=1, le=paperswithcode.MAX_PAGE),
]

PWC_PAGE_SIZE = Annotated[
    int,
    Field(description="Rows per page.", ge=1, le=paperswithcode.MAX_PAGE_SIZE),
]

ISO_DATE = Annotated[
    str | None,
    Field(
        description="Inclusive publication date bound, YYYY-MM-DD. Optional.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
]

_NOT_ARXIV_SUGGESTION = (
    "Papers with Code takes arXiv IDs only. Find this paper's arXiv ID with "
    "get_paper_metadata or search_arxiv, then retry."
)
_NOT_CATALOGUED_SUGGESTION = (
    "Papers with Code has no record for this arXiv ID; many papers have none. "
    "search_paperswithcode on the title can confirm."
)


def _suggest(result: dict[str, Any], *, not_found: str) -> dict[str, Any]:
    """Attach the suggestion matching the error's verdict.

    A 429 and a local lockout get the same advice: the allowance is per IP, so any call
    before ``retry_after_seconds`` lands in the same cooldown.
    """
    if result.get("not_found") is True:
        return enrich_error(result, not_found)
    if result.get("retryable") is True and "retry_after_seconds" in result:
        return enrich_error(
            result,
            "Papers with Code is rate-limiting this IP. Make no Papers with Code "
            "call until retry_after_seconds has passed.",
        )
    if result.get("retryable") is True:
        return enrich_error(result, "Transient Papers with Code failure — retry shortly.")
    return enrich_error(result, "Papers with Code rejected the request; check the arguments.")


async def _resolve_to_arxiv_id(
    identifier: str, *, force_refresh: bool
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve *identifier* to an arXiv ID when OpenAlex records one.

    Returns ``(arxiv_id, None)`` when *identifier* is already an arXiv ID or
    resolves to one through OpenAlex, ``(None, error)`` on an upstream lookup
    failure, or ``(None, None)`` when no arXiv ID is known.
    """
    if arxiv.is_arxiv_id(identifier):
        return identifier, None
    was_non_doi = (
        openalex.is_pmid(identifier)
        or openalex.is_pmcid(identifier)
        or openalex.is_work_id(identifier)
    )
    routed, trade_err = await resolve_paper_identifier(identifier, force_refresh=force_refresh)
    if trade_err is not None:
        return None, trade_err
    if arxiv.is_arxiv_id(routed):
        return routed, None
    if doinorm.looks_like_doi(routed) or openalex.is_work_id(routed):
        work = await openalex.get_work(
            routed,
            force_refresh=force_refresh and not (was_non_doi and openalex.is_work_id(routed)),
        )
        if "error" in work:
            if work.get("not_found") is True:
                return None, None
            return None, enrich_error(work, _NOT_ARXIV_SUGGESTION)
        return openalex_arxiv_id(work), None
    return None, None


async def _paper(arxiv_id: str, *, force_refresh: bool) -> tuple[str, dict[str, Any]]:
    """The canonical ID, and the paper's record or an error with a suggestion."""
    resolved, err = await _resolve_to_arxiv_id(arxiv_id, force_refresh=force_refresh)
    if err is not None:
        return arxiv.base_arxiv_id(arxiv_id), err
    target = resolved if resolved is not None else arxiv_id
    canonical = arxiv.base_arxiv_id(target)
    paper = await paperswithcode.get_paper(target, force_refresh=force_refresh)
    if "error" in paper:
        suggestion = (
            _NOT_CATALOGUED_SUGGESTION if arxiv.is_arxiv_id(target) else _NOT_ARXIV_SUGGESTION
        )
        return canonical, _suggest(paper, not_found=suggestion)
    return canonical, paper


def _repository_rank(repo: dict[str, Any]) -> tuple[bool, bool, int]:
    """Official first, then by stars descending; an unknown star count sorts last."""
    stars = repo.get("stars")
    return (not repo.get("is_official"), stars is None, -(stars or 0))


def _rank_repositories(repositories: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The first *limit* repositories by ``_repository_rank``; ties keep upstream order."""
    return sorted(repositories, key=_repository_rank)[:limit]


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_code(
    identifier: ARXIV_ID,
    max_repositories: Annotated[
        int,
        Field(
            description="Repositories to return, official first, then by stars.",
            ge=1,
            le=paperswithcode.MAX_REPOSITORIES,
        ),
    ] = 10,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Get linked code repositories, project pages, and Hugging Face artifacts on Papers with Code.

    Returns ``arxiv_id``, ``pwc_url``, ``has_official_implementation``,
    ``repository_count``, ``repositories`` (official first, then by stars),
    ``repositories_truncated``, ``project_pages``, ``hf_models``, ``hf_datasets``,
    ``hf_spaces``, and ``hf_artifact_counts``. Shares one cached record with
    ``get_paper_catalog`` (calling both costs one request).
    """
    canonical, paper = await _paper(identifier, force_refresh=force_refresh)
    if "error" in paper:
        return paper
    repositories = paper["repositories"]
    total = paper["repository_count"]
    if total is None:
        total = len(repositories)
    ranked = _rank_repositories(repositories, max_repositories)
    return {
        "arxiv_id": canonical,
        "pwc_url": paperswithcode.PAPER_PAGE_URL.format(canonical),
        "has_official_implementation": paper["has_official_implementation"],
        "repository_count": total,
        "repositories": ranked,
        "repositories_truncated": len(ranked) < max(total, len(repositories)),
        "project_pages": paper["project_pages"],
        "hf_models": paper["hf_models"],
        "hf_datasets": paper["hf_datasets"],
        "hf_spaces": paper["hf_spaces"],
        "hf_artifact_counts": paper["hf_artifact_counts"],
    }


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_catalog(
    identifier: ARXIV_ID,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Get Papers with Code classification, lineage, and best leaderboard ranks.

    Returns ``arxiv_id``, ``pwc_url``, ``title``, ``published``, ``tldr``,
    ``conference``, ``conference_url``, ``conference_award``, ``organizations``,
    ``hf_authors``, ``tasks``, ``methods``, ``introduced_benchmarks``,
    ``introduced_frameworks``, ``leaderboard_ranks``, ``predecessors``, and
    ``successors``. Chain task ``slug`` → ``get_pwc_task``; ``benchmark_slug`` →
    ``get_benchmark_leaderboard``; ``get_paper_evaluations`` for all results.
    """
    canonical, paper = await _paper(identifier, force_refresh=force_refresh)
    if "error" in paper:
        return paper
    return {
        "arxiv_id": canonical,
        "pwc_url": paperswithcode.PAPER_PAGE_URL.format(canonical),
        "title": paper["title"],
        "published": paper["published"],
        "tldr": paper["tldr"],
        "conference": paper["conference"],
        "conference_url": paper["conference_url"],
        "conference_award": paper["conference_award"],
        "organizations": paper["organizations"],
        "hf_authors": [author for author in paper["authors"] if author["hf_username"]],
        "tasks": paper["tasks"],
        "methods": paper["methods"],
        "introduced_benchmarks": paper["introduced_benchmarks"],
        "introduced_frameworks": paper["introduced_frameworks"],
        "leaderboard_ranks": paper["leaderboard_ranks"],
        "predecessors": paper["predecessors"],
        "successors": paper["successors"],
    }


def _page_envelope(result: dict[str, Any], page: int, page_size: int) -> dict[str, Any]:
    return {
        "page": page,
        "page_size": page_size,
        # Always an int, as on every tool that reports one.
        "total_results": result.get("total_results") or 0,
        "has_more": result.get("next_page") is not None,
    }


_PAPER_ROW_KEYS = ("paper_title", "paper_arxiv_id")
_BENCHMARK_ROW_KEYS = ("benchmark", "benchmark_slug")


def _without(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in keys}


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_paper_evaluations(
    identifier: ARXIV_ID,
    page: PWC_PAGE = 1,
    page_size: PWC_PAGE_SIZE = 10,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Page through every benchmark evaluation reported by a paper on Papers with Code.

    Each uncached page costs a rate-limited request; check ``total_results``
    before paging far.
    """
    canonical, paper = await _paper(identifier, force_refresh=force_refresh)
    if "error" in paper:
        return paper
    result = await paperswithcode.get_paper_evaluations(
        paper, page=page, page_size=page_size, force_refresh=force_refresh
    )
    if "error" in result:
        return _suggest(result, not_found="No evaluations on this page; try page=1.")
    return {
        "arxiv_id": canonical,
        **_page_envelope(result, page, page_size),
        "evaluations": [_without(row, _PAPER_ROW_KEYS) for row in result["evaluations"]],
    }


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_pwc_task(
    task: Annotated[
        str,
        Field(
            description="Task slug (machine-translation), display name (Machine "
            "Translation) or numeric Papers with Code ID."
        ),
    ],
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """A research task from the Papers with Code taxonomy.

    Returns ``{task_id, name, slug, description, keywords, parent_id, area_id, level,
    paper_count, benchmark_count, evaluation_count, research_trends}``. ``parent_id``
    is another task's ID and can be passed back to this tool. ``research_trends`` is
    editorial, with no fixed entry shape.

    Errors: ``{error, suggestion}``, plus ``not_found: true`` for an unknown task or
    ``retryable: true`` (with ``retry_after_seconds`` when rate-limited).

    A paper's task slugs come from get_paper_catalog.
    """
    result = await paperswithcode.get_task(task, force_refresh=force_refresh)
    if "error" in result:
        return _suggest(
            result,
            not_found="Unknown task. Use a slug from get_paper_catalog's tasks.",
        )
    return result


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def get_benchmark_leaderboard(
    benchmark: Annotated[
        str,
        Field(
            description="Benchmark slug (wmt-2014-english-german-newstest2014), display "
            "name, or numeric Papers with Code ID."
        ),
    ],
    page: PWC_PAGE = 1,
    page_size: PWC_PAGE_SIZE = 10,
    is_open: Annotated[
        bool | None,
        Field(description="true: open models only; false: closed only; omit for both."),
    ] = None,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Get a benchmark's details and leaderboard on Papers with Code (best rank first).

    Community-curated: cite the row's source paper, not the leaderboard. An
    uncached call costs two rate-limited requests (benchmark + page).
    """
    result = await paperswithcode.get_leaderboard(
        benchmark,
        page=page,
        page_size=page_size,
        is_open=is_open,
        force_refresh=force_refresh,
    )
    if "error" in result:
        return _suggest(
            result,
            not_found="Unknown benchmark or empty page. Use a slug from "
            "get_paper_catalog's leaderboard_ranks or introduced_benchmarks.",
        )
    return {
        "benchmark": result["benchmark"],
        **_page_envelope(result, page, page_size),
        "evaluations": [_without(row, _BENCHMARK_ROW_KEYS) for row in result["evaluations"]],
    }


@mcp.tool(annotations=READ_ONLY_NETWORK_ANNOTATIONS)
async def search_paperswithcode(
    query: Annotated[
        str,
        Field(description="Keywords matched against titles, authors and arXiv IDs.", min_length=1),
    ],
    page: PWC_PAGE = 1,
    page_size: PWC_PAGE_SIZE = 10,
    has_official_implementation: Annotated[
        bool, Field(description="Only papers with an official code repository.")
    ] = False,
    published_after: ISO_DATE = None,
    published_before: ISO_DATE = None,
) -> dict[str, Any]:
    """Keyword search over Papers with Code (slim triage list; strictest rate limit).

    Prefer one precise query over many pages. Chain ``arxiv_id`` into
    ``get_paper_code`` or ``get_paper_catalog``.
    """
    result = await paperswithcode.search(
        query,
        page=page,
        page_size=page_size,
        has_official_implementation=has_official_implementation,
        start_date=published_after,
        end_date=published_before,
    )
    if "error" in result:
        return _suggest(result, not_found="No results on this page; try page=1.")
    return {
        "page": page,
        "result_count": len(result["results"]),
        "has_more": result.get("next_page") is not None,
        "results": result["results"],
    }
