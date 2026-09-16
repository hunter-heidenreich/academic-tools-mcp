"""Papers with Code client (paperswithcode.co): anonymous read-only JSON, keyed by arXiv ID.

Code repositories, tasks, methods and leaderboards — data no other provider here has.
Upstream is a beta and "not a bulk export service", so each public function fetches at
most one page; nothing paginates on the caller's behalf.
"""

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from ..net import clients, http
from ..net.throttle import SubGap, Throttle
from ..store import cache, singleflight
from ..util import textnorm, useragent
from . import arxiv

NAMESPACE = "paperswithcode"

# Agent-facing provider name; every site that names us reads it.
LABEL = "Papers with Code"
_BASE_URL = "https://paperswithcode.co/api/v1"

# The public paper page, for a link the agent can hand a reader.
PAPER_PAGE_URL = "https://paperswithcode.co/paper/{}"

_PARSE_ERRORS = http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable Papers with Code response."""
    return http.parse_error_dict(LABEL)


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``clients.get_client``."""
    return clients.get_client(NAMESPACE, headers=useragent.headers(), timeout=30.0)


# Two documented per-IP allowances: the whole catalog, and a stricter one for list and
# search endpoints. We take 90% of each as a sustained rate and never spend the bursts,
# since the operator's browser shares the IP. Sequential, as upstream asks.
_DOCUMENTED_CATALOG_PER_MINUTE = 120
_DOCUMENTED_SEARCH_PER_MINUTE = 60
_BUDGET_FRACTION = 0.9
_MAX_CONCURRENT = 1
_MIN_REQUEST_GAP = 60.0 / (_DOCUMENTED_CATALOG_PER_MINUTE * _BUDGET_FRACTION)
_SEARCH_REQUEST_GAP = 60.0 / (_DOCUMENTED_SEARCH_PER_MINUTE * _BUDGET_FRACTION)
# Small: at this gap, a caller queued behind two others already waits over a second.
_MAX_PENDING = 3

_single_flight = singleflight.SingleFlight()

# Repository stars and linked code drift; leaderboards gain rows as results land.
_POSITIVE_TTL_SECONDS = 7 * 86400.0
# The task taxonomy is curated and slow-moving.
_TASK_TTL_SECONDS = 30 * 86400.0
# Results reorder as papers are ingested; a day still makes a repeated query free.
_SEARCH_TTL_SECONDS = 86400.0

# Exported as the tools' validation bounds. MAX_PAGE is upstream's pagination cap;
# MAX_PAGE_SIZE is ours, well under upstream's, to keep each response small.
MAX_PAGE = 100
MAX_PAGE_SIZE = 25
# A popular paper links hundreds of repositories; the tool returns a ranked slice.
MAX_REPOSITORIES = 50

_throttle = Throttle(
    namespace=NAMESPACE,
    label=LABEL,
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
    # No transparent retry: a retry spends the operator's shared allowance into the
    # cooldown a 429 just announced. The agent gets Retry-After instead.
    retry_attempts=1,
)


async def _throttled_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at the catalog rate. Url-only: ``_get_client`` is the only place to configure it."""
    return await _throttle.get(_get_client(), url, **kwargs)


_search_gap = SubGap(_throttle, min_gap_seconds=_SEARCH_REQUEST_GAP)


def reset_search_pacing() -> None:
    """Reset the list-and-search gap (test seam, called by conftest)."""
    _search_gap.reset()


async def _throttled_search_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at the list-and-search rate, then through the catalog slot.

    For every collection endpoint (``/papers/search``, ``/evaluations/``), not just search.
    """
    await _search_gap.wait()
    return await _throttled_get(url, **kwargs)


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


_SLUG_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")


def canonical_slug(value: str) -> str:
    """Fold a task or benchmark name, slug or numeric ID to upstream's slug form. Idempotent.

    Upstream slugs lowercase the name and hyphenate each non-alphanumeric run, so
    ``WMT 2014 English->German (newstest2014)`` → ``wmt-2014-english-german-newstest2014``.
    Diacritics fold to their base letter first (``Métodos`` → ``metodos``), as slugifiers
    do, rather than splitting the word. Slugs and numeric IDs pass through unchanged.
    """
    return _SLUG_SEPARATOR_RE.sub("-", textnorm.fold(value).lower()).strip("-")


# ---------------------------------------------------------------------------
# Response parsing
#
# Only the top-level shape can make a body a (retryable) parse error. Every nested read
# degrades to None or [] instead of raising: AttributeError/TypeError would escape
# both _PARSE_ERRORS and HTTPX_ERRORS.
# ---------------------------------------------------------------------------


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: Any) -> int | None:
    # bool is an int subclass; a `true` count is not a count.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _id(value: Any) -> str | None:
    """An ID as a string. Documented as one, but a bare integer is accepted too."""
    if isinstance(value, str) and value:
        return value
    return str(value) if _int(value) is not None else None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in _list(value) if isinstance(item, dict)]


def _strs(value: Any) -> list[str]:
    return (
        [item for item in value if isinstance(item, str) and item]
        if isinstance(value, list)
        else []
    )


def _scalar_metrics(value: Any) -> dict[str, Any]:
    """A metrics object as ``{name: number|string|null}``, dropping anything nested."""
    if not isinstance(value, dict):
        return {}
    return {
        name: metric
        for name, metric in value.items()
        if isinstance(name, str) and (metric is None or isinstance(metric, (str, int, float)))
    }


def _repository(raw: dict[str, Any]) -> dict[str, Any] | None:
    url = _str(raw.get("url"))
    if url is None:
        return None
    return {
        "url": url,
        "owner": _str(raw.get("owner")),
        "name": _str(raw.get("name")),
        "stars": _int(raw.get("num_stars")),
        "is_official": raw.get("is_official") is True,
    }


def _paper_of(data: Any) -> dict[str, Any] | None:
    """The normalized record for a paper, or ``None`` for a wrong-shape body.

    ``id`` and ``title`` are required: a 200 without them would cache an empty record
    for the full TTL.
    """
    if not isinstance(data, dict):
        return None
    pwc_id = _id(data.get("id"))
    title = _str(data.get("title"))
    if pwc_id is None or title is None:
        return None

    hf_summary = data.get("hf_artifact_summary")
    hf_summary = hf_summary if isinstance(hf_summary, dict) else {}

    return {
        "pwc_id": pwc_id,
        "arxiv_id": _str(data.get("arxiv_id")),
        "title": title,
        "published": _str(data.get("published")),
        "tldr": _str(data.get("tldr")),
        "authors": [
            {"name": name, "hf_username": _str(link.get("hf_username"))}
            for link in _dicts(data.get("author_links"))
            if (name := _str(link.get("name"))) is not None
        ],
        "conference": _str(data.get("conference_name")) or _str(data.get("proceeding")),
        "conference_url": _str(data.get("conference_url_abs")),
        "conference_award": _str(data.get("conference_award")),
        "has_official_implementation": _bool(data.get("has_official_implementation")),
        "repository_count": _int(data.get("code_repository_count")),
        "repositories": [
            repo for raw in _dicts(data.get("repositories")) if (repo := _repository(raw))
        ],
        "project_pages": [
            {"url": url, "is_official": page.get("is_official") is True}
            for page in _dicts(data.get("project_pages"))
            if (url := _str(page.get("url"))) is not None
        ],
        "hf_models": _strs(data.get("hf_models")),
        "hf_datasets": _strs(data.get("hf_datasets")),
        "hf_spaces": _strs(data.get("hf_spaces")),
        "hf_artifact_counts": {
            "models": _int(hf_summary.get("model_count")),
            "datasets": _int(hf_summary.get("dataset_count")),
            "spaces": _int(hf_summary.get("space_count")),
        },
        "tasks": [
            {"name": name, "slug": _str(task.get("slug"))}
            for task in _dicts(data.get("tasks"))
            if (name := _str(task.get("name"))) is not None
        ],
        "methods": [
            {
                "name": name,
                "slug": _str(method.get("slug")),
                "introduced_year": _int(method.get("introduced_year")),
                "source_title": _str(method.get("source_title")),
            }
            for method in _dicts(data.get("methods"))
            if (name := _str(method.get("name"))) is not None
        ],
        "introduced_benchmarks": [
            {
                "name": name,
                "slug": _str(bench.get("slug")),
                "full_name": _str(bench.get("full_name")),
            }
            for bench in _dicts(data.get("introduced_benchmarks"))
            if (name := _str(bench.get("name"))) is not None
        ],
        "introduced_frameworks": [
            {"name": name, "slug": _str(fw.get("slug"))}
            for fw in _dicts(data.get("introduced_frameworks"))
            if (name := _str(fw.get("name"))) is not None
        ],
        "leaderboard_ranks": [
            {
                "benchmark": name,
                "benchmark_slug": _str(rank.get("dataset_slug")),
                "task_slug": _str(rank.get("task_slug")),
                "rank": _int(rank.get("best_rank")),
                "metric": _str(rank.get("best_metric")),
            }
            for rank in _dicts(data.get("leaderboard_ranks"))
            if (name := _str(rank.get("dataset_name"))) is not None
        ],
        "organizations": [
            {"name": name, "slug": _str(org.get("slug"))}
            for org in _dicts(data.get("organizations"))
            if (name := _str(org.get("name"))) is not None
        ],
        "predecessors": _related(data.get("predecessor_papers")),
        "successors": _related(data.get("successor_papers")),
    }


def _related(value: Any) -> list[dict[str, Any]]:
    return [
        {"arxiv_id": _str(paper.get("arxiv_id")), "title": title}
        for paper in _dicts(value)
        if (title := _str(paper.get("title"))) is not None
    ]


def _evaluation(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": _str(raw.get("model_name")),
        "task": _str(raw.get("task_name")),
        "task_slug": _str(raw.get("task_slug")),
        "benchmark": _str(raw.get("dataset_name")),
        "benchmark_slug": _str(raw.get("dataset_slug")),
        "metrics": _scalar_metrics(raw.get("metrics")),
        "best_metric": _str(raw.get("best_metric")),
        "rank": _int(raw.get("best_rank")),
        "num_parameters": _int(raw.get("num_parameters")),
        "is_open": _bool(raw.get("is_open")),
        "uses_additional_data": _bool(raw.get("uses_additional_data")),
        "harness": _str(raw.get("harness")),
        "evaluated_on": _str(raw.get("evaluated_on")),
        "paper_title": _str(raw.get("paper_title")),
        "paper_arxiv_id": _str(raw.get("paper_arxiv_id")),
        "code_url": _str(raw.get("code_url")),
        "hf_model_url": _str(raw.get("hf_model_url")),
        "source_url": _str(raw.get("result_url")) or _str(raw.get("source_url")),
    }


def _page_of(data: Any) -> tuple[list[dict[str, Any]], int | None, int | None] | None:
    """``(rows, count, next_page)``, or ``None`` for a wrong shape.

    Non-empty ``results`` with no objects in it is malformed, not an empty page.
    """
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if not isinstance(results, list):
        return None
    rows = _dicts(results)
    if results and not rows:
        return None
    return rows, _int(data.get("count")), _int(data.get("next_page"))


def _task_of(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    task_id = _id(data.get("id"))
    name = _str(data.get("name"))
    if task_id is None or name is None:
        return None
    return {
        "task_id": task_id,
        "name": name,
        "slug": _str(data.get("slug")),
        "description": _str(data.get("description")),
        "keywords": _str(data.get("keywords")),
        "parent_id": _id(data.get("parent_id")),
        "area_id": _id(data.get("area_id")),
        "level": _int(data.get("level")),
        "paper_count": _int(data.get("paper_count")),
        "benchmark_count": _int(data.get("benchmark_count")),
        "evaluation_count": _int(data.get("evaluation_count")),
        # Element shape is undocumented; keep strings and objects.
        "research_trends": [
            trend for trend in _list(data.get("research_trends")) if isinstance(trend, (str, dict))
        ],
    }


def _dataset_of(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    dataset_id = _id(data.get("id"))
    name = _str(data.get("name"))
    if dataset_id is None or name is None:
        return None
    introducing = data.get("introducing_paper")
    introducing = introducing if isinstance(introducing, dict) else {}
    return {
        "benchmark_id": dataset_id,
        "name": name,
        "slug": _str(data.get("slug")),
        "full_name": _str(data.get("full_name")),
        "description": _str(data.get("short_description")) or _str(data.get("description")),
        "homepage": _str(data.get("homepage")) or _str(data.get("url")),
        "hf_url": _str(data.get("hf_url")),
        "license": _str(data.get("license_name")),
        "license_url": _str(data.get("license_url")),
        "introduced_year": _int(data.get("introduced_year")),
        "introducing_paper": {
            "title": _str(introducing.get("title")) or _str(data.get("introducing_paper_title")),
            "arxiv_id": _str(introducing.get("arxiv_id")),
        },
        "modalities": _strs(data.get("modalities")),
        "languages": _strs(data.get("languages")),
        "paper_count": _int(data.get("paper_count")),
        "task_count": _int(data.get("task_count")),
        "pwc_url": _str(data.get("pwc_url")),
    }


def _summary_hit(raw: dict[str, Any]) -> dict[str, Any] | None:
    title = _str(raw.get("title"))
    if title is None:
        return None
    authors = _strs(raw.get("authors"))
    return {
        "arxiv_id": _str(raw.get("arxiv_id")),
        "title": title,
        "first_author": authors[0] if authors else None,
        "author_count": len(authors),
        "published": _str(raw.get("published")),
        "has_official_implementation": _bool(raw.get("has_official_implementation")),
        "repository_count": _int(raw.get("code_repository_count")),
    }


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def _fetch_record(
    url: str,
    *,
    entity: str,
    canonical: str,
    parse: Callable[[Any], dict[str, Any] | None],
    not_found_error: str,
    params: dict[str, Any] | None = None,
    search_gate: bool = False,
) -> dict[str, Any]:
    """GET, parse and cache: the fetch body every getter below shares.

    ``parse`` returns the dict to cache, or ``None`` for a wrong shape (retryable,
    uncached). A 404 is negative-cached under ``entity``.
    """
    get = _throttled_search_get if search_gate else _throttled_get
    try:
        response = await get(url, params=params)
        if response.status_code == 404:
            err = http.not_found(not_found_error)
            cache.put_negative(NAMESPACE, entity, canonical, err)
            return err
        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except http.HTTPX_ERRORS as e:
        return http.error_dict(LABEL, e)

    record = parse(data)
    if record is None:
        return _parse_error_dict()
    cache.put(NAMESPACE, entity, canonical, record)
    return record


async def get_paper(arxiv_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """An arXiv paper's normalized record (``_paper_of``), repositories included.

    An uncatalogued paper is a negative-cached ``not_found``.
    """
    # Unversioned, unlike arxiv's own key: upstream has one record per paper.
    canonical = arxiv.base_arxiv_id(arxiv_id)
    not_found_error = f"Papers with Code has no record for arXiv ID: {arxiv_id}"

    async def _fetch() -> dict[str, Any]:
        # The request-side shape check: nothing but an arXiv ID (never `..` or empty)
        # reaches the URL. Uncached, since no request was made.
        if not arxiv.is_arxiv_id(canonical):
            return http.not_found(not_found_error)
        # The request keeps the caller's case (`math.GT/...`); safe="" keeps an
        # old-style ID's slash inside one path segment.
        bare = arxiv.strip_version(arxiv.normalize_arxiv_id(arxiv_id))
        url = f"{_BASE_URL}/papers/arxiv/{quote(bare, safe='')}"
        if not http.addresses_a_record(url):
            return http.not_found(not_found_error)
        return await _fetch_record(
            url,
            params={"include_resources": "true"},
            entity="papers",
            canonical=canonical,
            parse=_paper_of,
            not_found_error=not_found_error,
        )

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="papers",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=("papers", canonical),
    )


def _parse_evaluation_page(data: Any) -> dict[str, Any] | None:
    page = _page_of(data)
    if page is None:
        return None
    rows, count, next_page = page
    return {
        "total_results": count,
        "next_page": next_page,
        "evaluations": [_evaluation(row) for row in rows],
    }


async def _evaluation_page(
    *,
    params: dict[str, Any],
    page: int,
    page_size: int,
    not_found_error: str,
    force_refresh: bool,
) -> dict[str, Any]:
    """One cached page of ``/evaluations/``, keyed on its full query."""
    query = {**params, "page": page, "page_size": page_size}
    canonical = json.dumps(query, sort_keys=True)

    async def _fetch() -> dict[str, Any]:
        return await _fetch_record(
            f"{_BASE_URL}/evaluations/",
            params=query,
            entity="evaluations",
            canonical=canonical,
            parse=_parse_evaluation_page,
            not_found_error=not_found_error,
            search_gate=True,
        )

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="evaluations",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=("evaluations", canonical),
    )


async def get_paper_evaluations(
    paper: dict[str, Any], *, page: int = 1, page_size: int = 10, force_refresh: bool = False
) -> dict[str, Any]:
    """One page of a paper's evaluation rows, most-benchmarked dataset first.

    ``paper`` is a ``get_paper`` record, whose numeric ID the endpoint takes. Returns
    ``{total_results, next_page, evaluations}``.
    """
    return await _evaluation_page(
        params={"paper_id": paper["pwc_id"], "ordering": "-benchmark_popularity"},
        page=page,
        page_size=page_size,
        not_found_error=f"No evaluations page {page} for: {paper['title']}",
        force_refresh=force_refresh,
    )


async def _get_singleton(
    entity: str,
    identifier: str,
    *,
    ttl: float,
    parse: Callable[[Any], dict[str, Any] | None],
    kind: str,
    force_refresh: bool,
) -> dict[str, Any]:
    """A task or benchmark by slug, name or numeric ID."""
    canonical = canonical_slug(identifier)
    not_found_error = f"Papers with Code has no {kind}: {identifier}"

    async def _fetch() -> dict[str, Any]:
        # canonical_slug emits only [a-z0-9-], so the one path that escapes is an empty
        # slug, which would list the whole collection. Uncached: no request made.
        url = f"{_BASE_URL}/{entity}/{canonical}"
        if not canonical or not http.addresses_a_record(url):
            return http.not_found(not_found_error)
        return await _fetch_record(
            url,
            entity=entity,
            canonical=canonical,
            parse=parse,
            not_found_error=not_found_error,
        )

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity=entity,
        canonical=canonical,
        positive_ttl=ttl,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=(entity, canonical),
    )


async def get_task(task: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """A task by slug, display name or numeric ID (see ``canonical_slug``)."""
    return await _get_singleton(
        "tasks",
        task,
        ttl=_TASK_TTL_SECONDS,
        parse=_task_of,
        kind="task",
        force_refresh=force_refresh,
    )


async def get_benchmark(benchmark: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """A benchmark (upstream's *dataset*) by slug, display name or numeric ID."""
    return await _get_singleton(
        "datasets",
        benchmark,
        ttl=_POSITIVE_TTL_SECONDS,
        parse=_dataset_of,
        kind="benchmark",
        force_refresh=force_refresh,
    )


async def get_leaderboard(
    benchmark: str,
    *,
    page: int = 1,
    page_size: int = 10,
    is_open: bool | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """One page of a benchmark's leaderboard, best rank first.

    Returns ``{benchmark, total_results, next_page, evaluations}``. A cold cache costs
    two requests (benchmark to numeric ID, then the page); ``force_refresh`` refreshes
    only the page.
    """
    dataset = await get_benchmark(benchmark)
    if "error" in dataset:
        return dataset
    params: dict[str, Any] = {"dataset_id": dataset["benchmark_id"], "ordering": "best_rank"}
    if is_open is not None:
        params["is_open"] = "true" if is_open else "false"
    result = await _evaluation_page(
        params=params,
        page=page,
        page_size=page_size,
        not_found_error=f"No leaderboard page {page} for benchmark: {benchmark}",
        force_refresh=force_refresh,
    )
    return result if "error" in result else {"benchmark": dataset, **result}


def _parse_search_page(data: Any) -> dict[str, Any] | None:
    page = _page_of(data)
    if page is None:
        return None
    rows, _count, next_page = page
    return {
        "next_page": next_page,
        "results": [hit for row in rows if (hit := _summary_hit(row)) is not None],
    }


async def search(
    query: str,
    *,
    page: int = 1,
    page_size: int = 10,
    has_official_implementation: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """One page of keyword search: ``{next_page, results}``. Upstream sends no total.

    Hits are partial records, so they don't warm ``get_paper``'s cache, which they would
    poison. The page is cached, keyed on the normalized query.
    """
    params: dict[str, Any] = {
        "q": " ".join(query.split()),
        "mode": "keyword",
        "page": page,
        "page_size": page_size,
    }
    if has_official_implementation:
        params["has_official_implementation"] = "true"
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    canonical = json.dumps(params, sort_keys=True)

    async def _fetch() -> dict[str, Any]:
        return await _fetch_record(
            f"{_BASE_URL}/papers/search",
            params=params,
            entity="searches",
            canonical=canonical,
            parse=_parse_search_page,
            not_found_error=f"No search results page {page} for: {query}",
            search_gate=True,
        )

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="searches",
        canonical=canonical,
        positive_ttl=_SEARCH_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=False,
        sf_key=("searches", canonical),
    )
