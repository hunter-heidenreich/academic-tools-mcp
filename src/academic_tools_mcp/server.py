"""Thin MCP server entry point.

Importing a `tools/*` module runs its `@mcp.tool` decorators, so the import list
below *is* the registration. `__all__` holds the tool callables, the provider
modules tests patch, `mcp` and this module's debug gate. Invariant: never a tool
module's internals.
"""

from typing import Any

from .app import READ_ONLY_LOCAL_ANNOTATIONS, mcp
from .net import stats
from .providers import (
    arxiv,
    biorxiv,
    crossref,
    openalex,
    opencitations,
    paperswithcode,
    pmc,
    wikipedia,
)
from .tools.catalog import (
    get_benchmark_leaderboard,
    get_paper_catalog,
    get_paper_code,
    get_paper_evaluations,
    get_pwc_task,
    search_paperswithcode,
)
from .tools.graph import (
    get_paper_citations,
    get_paper_citations_count,
    get_paper_references,
    get_paper_references_count,
)
from .tools.paper import (
    get_author,
    get_institution,
    get_paper_abstract,
    get_paper_authors,
    get_paper_bibtex,
    get_paper_metadata,
    get_paper_updates,
    get_paper_versions,
    get_papers_metadata,
)
from .tools.pipeline import (
    convert_paper,
    download_pdf,
    get_paper_markdown,
    get_paper_section,
    get_paper_sections,
    import_paper,
    list_cached_papers,
)
from .tools.search import (
    autocomplete_openalex,
    find_in_paper,
    get_wikipedia_summary,
    search_arxiv,
    search_authors,
    search_cached_papers,
    search_crossref_by_title,
    search_openalex,
    search_wikipedia,
)
from .util import config

__all__ = [
    "_DEBUG_TOOLS_ENABLED",
    "arxiv",
    "autocomplete_openalex",
    "biorxiv",
    "convert_paper",
    "crossref",
    "download_pdf",
    "find_in_paper",
    "get_author",
    "get_benchmark_leaderboard",
    "get_institution",
    "get_paper_abstract",
    "get_paper_authors",
    "get_paper_bibtex",
    "get_paper_catalog",
    "get_paper_citations",
    "get_paper_citations_count",
    "get_paper_code",
    "get_paper_evaluations",
    "get_paper_markdown",
    "get_paper_metadata",
    "get_paper_references",
    "get_paper_references_count",
    "get_paper_section",
    "get_paper_sections",
    "get_paper_updates",
    "get_paper_versions",
    "get_papers_metadata",
    "get_pwc_task",
    "get_wikipedia_summary",
    "import_paper",
    "list_cached_papers",
    "mcp",
    "openalex",
    "opencitations",
    "paperswithcode",
    "pmc",
    "search_arxiv",
    "search_authors",
    "search_cached_papers",
    "search_crossref_by_title",
    "search_openalex",
    "search_paperswithcode",
    "search_wikipedia",
    "wikipedia",
]


# Tool groups that operators can disable at startup via DISABLED_TOOL_GROUPS
# (comma-separated, e.g. "paperswithcode,wikipedia") to trim the MCP tool catalog
# and prompt token footprint.
TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "paper": (
        "get_paper_metadata",
        "get_papers_metadata",
        "get_paper_authors",
        "get_paper_abstract",
        "get_paper_bibtex",
        "get_paper_versions",
        "get_author",
        "get_institution",
        "get_paper_updates",
    ),
    "pipeline": (
        "download_pdf",
        "convert_paper",
        "get_paper_sections",
        "get_paper_section",
        "get_paper_markdown",
        "import_paper",
        "list_cached_papers",
    ),
    "graph": (
        "get_paper_references_count",
        "get_paper_references",
        "get_paper_citations_count",
        "get_paper_citations",
    ),
    "search": (
        "search_arxiv",
        "search_crossref_by_title",
        "search_openalex",
        "search_authors",
        "autocomplete_openalex",
        "find_in_paper",
        "search_cached_papers",
    ),
    "wikipedia": ("search_wikipedia", "get_wikipedia_summary"),
    "paperswithcode": (
        "get_paper_code",
        "get_paper_catalog",
        "get_paper_evaluations",
        "get_pwc_task",
        "get_benchmark_leaderboard",
        "search_paperswithcode",
    ),
}


def resolve_disabled_tools(raw_groups: str | None) -> set[str]:
    """Resolve a comma-separated ``DISABLED_TOOL_GROUPS`` string to tool names."""
    if not raw_groups:
        return set()
    disabled: set[str] = set()
    for part in raw_groups.split(","):
        group = part.strip().lower()
        if group in TOOL_GROUPS:
            disabled.update(TOOL_GROUPS[group])
    return disabled


_DISABLED_TOOLS = resolve_disabled_tools(config.get("DISABLED_TOOL_GROUPS"))
if _DISABLED_TOOLS:
    mcp.disable(names=_DISABLED_TOOLS, components={"tool"})


# Gated on a truthy ENABLE_DEBUG_TOOLS, read once at import, so flipping it needs
# a restart. Keep `get_server_stats` inside the `if`: an agent must never observe
# cache or throttle state.

_DEBUG_TOOLS_ENABLED = config.flag("ENABLE_DEBUG_TOOLS")

if _DEBUG_TOOLS_ENABLED:

    @mcp.tool(annotations=READ_ONLY_LOCAL_ANNOTATIONS)
    async def get_server_stats() -> dict[str, Any]:
        """Operator-only: snapshot cumulative cache and HTTP counters.

        Returns ``{providers, env_file}``. ``providers`` maps each cache
        namespace (``openalex``, ``arxiv``, ``acl_anthology``, ``oa_download``,
        ...) to the counters it has moved — ``cache_hits``, ``cache_misses``,
        ``negative_hits``, ``http_calls``, ``http_retries``,
        ``backpressure_refusals``, ``quota_refusals``, ``quota_window_ignored``,
        ``client_config_ignored``, ``cache_write_failures`` since process start,
        plus live ``in_flight`` (in-flight requests *and* callers queued on a
        pacing gap). An absent counter means zero. ``env_file`` is the ``.env``
        that won at import, or null.

        Use this when something feels slow or rate-limit-pressured, to see
        which namespace is hitting the network vs. serving from cache.
        """
        return stats.snapshot()


if __name__ == "__main__":
    mcp.run()
