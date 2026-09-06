"""Thin MCP server entry point.

Imports the shared `mcp` instance and the four tool-group modules (importing them
runs their `@mcp.tool` decorators, registering the tools), then re-exports the
tool callables plus the providers and helpers that tests / callers reach via
`server.<name>`. The optional operator-only debug tool is registered at the end.
"""

from typing import Any

from . import _stats, config
from ._app import mcp
from .providers import arxiv, biorxiv, crossref, openalex, opencitations, wikipedia
from .tools.graph import (
    get_paper_citations,
    get_paper_citations_count,
    get_paper_references,
    get_paper_references_count,
)
from .tools.paper import (
    _format_crossref_metadata,
    _format_openalex_metadata,
    get_author,
    get_paper_abstract,
    get_paper_authors,
    get_paper_bibtex,
    get_paper_metadata,
    get_papers_metadata,
)
from .tools.pipeline import (
    _download_pdf_by_provider,
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
    search_wikipedia,
)

__all__ = [
    "_DEBUG_TOOLS_ENABLED",
    "_download_pdf_by_provider",
    "_format_crossref_metadata",
    "_format_openalex_metadata",
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
    "search_wikipedia",
    "wikipedia",
]


# Operator-only debug tools (gated behind ENABLE_DEBUG_TOOLS env var)
# ---------------------------------------------------------------------------
#
# These are NOT registered when the env var is absent so agents never
# see them in normal operation. The env var flips them on for an
# operator who wants to inspect cumulative cache/HTTP counters from
# within Claude Code itself, without dropping into a Python REPL.

_DEBUG_TOOLS_ENABLED = config.flag("ENABLE_DEBUG_TOOLS")

if _DEBUG_TOOLS_ENABLED:

    @mcp.tool
    async def get_server_stats() -> dict[str, Any]:
        """Operator-only: snapshot cumulative cache + HTTP counters.

        Only registered when ``ENABLE_DEBUG_TOOLS=1`` in the environment;
        agents never see it in normal operation. Returns the per-provider
        counters tracked by ``_stats.snapshot()``: cache_hits / cache_misses
        / negative_hits, http_calls / http_retries, backpressure_refusals,
        cache_write_failures, and live in_flight counts. Cumulative since
        process start.

        Use this when something feels slow or rate-limit-pressured to see
        which provider is hitting the network vs. serving from cache.
        """
        return _stats.snapshot()


if __name__ == "__main__":
    mcp.run()
