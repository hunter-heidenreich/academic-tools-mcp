"""Thin MCP server entry point.

Importing a `tools/*` module runs its `@mcp.tool` decorators, so the import list
below *is* the registration. `__all__` holds the tool callables, the provider
modules tests patch, `mcp` and this module's debug gate. Invariant: never a tool
module's internals.
"""

from typing import Any

from .app import mcp
from .net import stats
from .providers import arxiv, biorxiv, crossref, openalex, opencitations, wikipedia
from .tools.graph import (
    get_paper_citations,
    get_paper_citations_count,
    get_paper_references,
    get_paper_references_count,
)
from .tools.paper import (
    get_author,
    get_paper_abstract,
    get_paper_authors,
    get_paper_bibtex,
    get_paper_metadata,
    get_papers_metadata,
)
from .tools.pipeline import (
    convert_paper,
    download_pdf,
    get_paper_section,
    get_paper_sections,
    import_paper,
)
from .tools.search import (
    find_in_paper,
    get_wikipedia_summary,
    search_arxiv,
    search_cached_papers,
    search_crossref_by_title,
    search_openalex,
    search_wikipedia,
)
from .util import config

__all__ = [
    "_DEBUG_TOOLS_ENABLED",
    "arxiv",
    "biorxiv",
    "convert_paper",
    "crossref",
    "download_pdf",
    "find_in_paper",
    "get_author",
    "get_paper_abstract",
    "get_paper_authors",
    "get_paper_bibtex",
    "get_paper_citations",
    "get_paper_citations_count",
    "get_paper_metadata",
    "get_paper_references",
    "get_paper_references_count",
    "get_paper_section",
    "get_paper_sections",
    "get_papers_metadata",
    "get_wikipedia_summary",
    "import_paper",
    "mcp",
    "openalex",
    "opencitations",
    "search_arxiv",
    "search_cached_papers",
    "search_crossref_by_title",
    "search_openalex",
    "search_wikipedia",
    "wikipedia",
]


# Gated on a truthy ENABLE_DEBUG_TOOLS, read once at import, so flipping it needs
# a restart. Keep `get_server_stats` inside the `if`: an agent must never observe
# cache or throttle state.

_DEBUG_TOOLS_ENABLED = config.flag("ENABLE_DEBUG_TOOLS")

if _DEBUG_TOOLS_ENABLED:

    @mcp.tool
    async def get_server_stats() -> dict[str, Any]:
        """Operator-only: snapshot cumulative cache and HTTP counters.

        Returns ``{providers, env_file}``. ``providers`` maps each cache
        namespace (``openalex``, ``arxiv``, ``acl_anthology``, ``oa_download``,
        ...) to the counters it has moved — ``cache_hits``, ``cache_misses``,
        ``negative_hits``, ``http_calls``, ``http_retries``,
        ``backpressure_refusals``, ``cache_write_failures`` since process start,
        plus live ``in_flight``. An absent counter means zero. ``env_file`` is
        the ``.env`` that won at import, or null.

        Use this when something feels slow or rate-limit-pressured, to see
        which namespace is hitting the network vs. serving from cache.
        """
        return stats.snapshot()


if __name__ == "__main__":
    mcp.run()
