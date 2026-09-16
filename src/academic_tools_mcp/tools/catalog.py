"""Papers with Code tools: code, catalog, evaluations, tasks, leaderboards, search.

Dedicated tools rather than a new ``_source``: none of this data has a counterpart in
the unified paper tools.
"""

from typing import Annotated, Any

from pydantic import Field

from ..app import FORCE_REFRESH, enrich_error, mcp
from ..providers import paperswithcode

ARXIV_ID = Annotated[
    str,
    Field(
        description="arXiv ID: bare (1706.03762, hep-th/9901001), arXiv:-prefixed, "
        "or an arxiv.org URL; any version suffix is ignored. DOIs are not accepted — "
        "find the arXiv ID with get_paper_metadata or search_arxiv."
    ),
]

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


async def _paper(arxiv_id: str, force_refresh: bool) -> tuple[str, dict[str, Any]]:
    """The canonical ID, and the paper's record or an error with a suggestion."""
    canonical = paperswithcode.canonical_arxiv_id(arxiv_id)
    paper = await paperswithcode.get_paper(arxiv_id, force_refresh=force_refresh)
    if "error" in paper:
        suggestion = (
            _NOT_CATALOGUED_SUGGESTION
            if paperswithcode.is_arxiv_id(arxiv_id)
            else _NOT_ARXIV_SUGGESTION
        )
        _suggest(paper, not_found=suggestion)
    return canonical, paper


def _repository_rank(repo: dict[str, Any]) -> tuple[bool, bool, int]:
    """Official first, then by stars descending; an unknown star count sorts last."""
    stars = repo.get("stars")
    return (not repo.get("is_official"), stars is None, -(stars or 0))


def _rank_repositories(repositories: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The first *limit* repositories by ``_repository_rank``; ties keep upstream order."""
    return sorted(repositories, key=_repository_rank)[:limit]


@mcp.tool
async def get_paper_code(
    arxiv_id: ARXIV_ID,
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
    """Code and Hugging Face artifacts linked to a paper on Papers with Code.

    Returns ``{arxiv_id, pwc_url, has_official_implementation, repository_count,
    repositories: [{url, owner, name, stars, is_official}, ...], repositories_truncated,
    project_pages: [{url, is_official}, ...], hf_models, hf_datasets, hf_spaces,
    hf_artifact_counts: {models, datasets, spaces}}``. ``repository_count`` is the full
    total; ``repositories_truncated`` is true when more exist than were returned.
    ``is_official`` is Papers with Code's label, not verified. The ``hf_*`` URL lists
    are usually empty for arXiv papers, but ``hf_artifact_counts`` still counts them.
    ``arxiv_id`` is echoed canonical, without version.

    Errors: ``{error, suggestion}``, plus ``not_found: true`` for an uncatalogued paper
    or a non-arXiv ID, or ``retryable: true`` (with ``retry_after_seconds`` when
    rate-limited).

    get_paper_catalog reads the same cached record, so calling both costs one request.
    """
    canonical, paper = await _paper(arxiv_id, force_refresh)
    if "error" in paper:
        return paper
    repositories = paper["repositories"]
    total = paper["repository_count"]
    if total is None:
        total = len(repositories)
    ranked = _rank_repositories(repositories, max_repositories)
    return {
        "arxiv_id": canonical,
        "pwc_url": paperswithcode.paper_page_url(canonical),
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


@mcp.tool
async def get_paper_catalog(
    arxiv_id: ARXIV_ID,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """How Papers with Code classifies a paper: tasks, methods, benchmarks, lineage.

    Returns ``{arxiv_id, pwc_url, title, published, tldr, conference, conference_url,
    conference_award, organizations: [{name, slug}], hf_authors: [{name,
    hf_username}], tasks: [{name, slug}], methods: [{name, slug, introduced_year,
    source_title}], introduced_benchmarks: [{name, slug, full_name}],
    introduced_frameworks: [{name, slug}], leaderboard_ranks: [{benchmark,
    benchmark_slug, task_slug, rank, metric}], predecessors: [{arxiv_id, title}],
    successors: [{arxiv_id, title}]}``. ``tldr`` is machine-generated. ``hf_authors``
    holds only authors with a Hugging Face account; get_paper_authors has the full
    list. ``leaderboard_ranks`` holds only the best placements; get_paper_evaluations
    pages every result.

    Errors: as get_paper_code.

    Chain: a task ``slug`` → get_pwc_task; a ``benchmark_slug`` →
    get_benchmark_leaderboard.
    """
    canonical, paper = await _paper(arxiv_id, force_refresh)
    if "error" in paper:
        return paper
    return {
        "arxiv_id": canonical,
        "pwc_url": paperswithcode.paper_page_url(canonical),
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


@mcp.tool
async def get_paper_evaluations(
    arxiv_id: ARXIV_ID,
    page: PWC_PAGE = 1,
    page_size: PWC_PAGE_SIZE = 10,
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Every benchmark result a paper reports on Papers with Code, one page at a time.

    Rows on the most-benchmarked datasets come first. Returns ``{arxiv_id, page,
    page_size, total_results, has_more, evaluations: [{model, task, task_slug, benchmark,
    benchmark_slug, metrics: {name: value}, best_metric, rank, num_parameters,
    is_open, uses_additional_data, harness, evaluated_on, code_url, hf_model_url,
    source_url}, ...]}``. ``rank`` is the row's leaderboard position; ``metrics``
    values may be numbers or strings, as upstream reports them.

    Errors: as get_paper_code.

    Each uncached page costs a rate-limited request; check ``total_results`` before
    paging far.
    """
    canonical, paper = await _paper(arxiv_id, False)
    if "error" in paper:
        return paper
    result = await paperswithcode.get_paper_evaluations(
        arxiv_id, page=page, page_size=page_size, force_refresh=force_refresh
    )
    if "error" in result:
        return _suggest(result, not_found="No evaluations on this page; try page=1.")
    return {
        "arxiv_id": canonical,
        **_page_envelope(result, page, page_size),
        "evaluations": [_without(row, _PAPER_ROW_KEYS) for row in result["evaluations"]],
    }


@mcp.tool
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


@mcp.tool
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
    """A benchmark's leaderboard on Papers with Code, best rank first.

    Returns ``{benchmark: {benchmark_id, name, slug, full_name, description, homepage,
    hf_url, license, license_url, introduced_year, introducing_paper: {title,
    arxiv_id}, modalities, languages, paper_count, task_count, pwc_url}, page,
    page_size, total_results, has_more, evaluations: [{model, task, task_slug, metrics,
    best_metric, rank, num_parameters, is_open, uses_additional_data, harness,
    evaluated_on, paper_title, paper_arxiv_id, code_url, hf_model_url, source_url},
    ...]}``. A benchmark used by several tasks mixes their rows; group on
    ``task_slug``. Leaderboards are community-curated: cite the row's paper, not the
    leaderboard.

    Errors: as get_pwc_task.

    An uncached call costs two rate-limited requests: the benchmark, then the page.
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


@mcp.tool
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
    """Keyword search over the Papers with Code catalog. Returns a slim triage list.

    Returns ``{page, result_count, has_more, results: [{arxiv_id, title, first_author,
    author_count, published, has_official_implementation, repository_count}, ...]}``.
    Upstream reports no total; ``has_more`` says whether another page exists.
    ``arxiv_id`` is null for a paper not from arXiv, which the other Papers with Code
    tools cannot look up.

    Errors: ``{error, suggestion}``, plus ``retryable: true`` on a transient failure
    (with ``retry_after_seconds`` when rate-limited).

    Search has Papers with Code's tightest rate limit: prefer one precise query over
    many pages. Chain get_paper_code or get_paper_catalog on ``arxiv_id``; each
    follow-up costs a request, since hits are not cached as paper records.
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
