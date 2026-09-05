"""Shared outbound ``User-Agent`` construction.

Four providers hand-rolled a User-Agent in four different formats, and three
(`biorxiv`, `opencitations`, `acl_anthology`) plus the open-access download
path built no headers at all — so they went out as ``python-httpx/x.y``.
``providers/arxiv.py`` documents at length why that matters (arXiv's Fastly
edge throttles anonymous library traffic far harder); the reasoning was never
propagated to the others.

Worse, the URL the hand-rolled agents advertised —
``https://github.com/academic-tools-mcp`` — does not exist. The entire point
of a contact URL is that an operator who needs to reach you can; a 404 defeats
it. The version was hardcoded ``1.0`` against a calendar-versioned package.

Both are fixed here, once:

    academic-tools-mcp/2026.9.4 (+https://github.com/hunter-heidenreich/academic-tools-mcp; mailto:you@example.org)

Wikimedia's User-Agent policy, Crossref's polite pool, and OpenAlex's polite
pool all ask for exactly this shape: a name, a version, a way to reach the
operator.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

_PROJECT_URL = "https://github.com/hunter-heidenreich/academic-tools-mcp"
_DISTRIBUTION = "academic-tools-mcp"

# Fallback when running from a source tree with no installed distribution
# metadata. Only affects the string we advertise, never behaviour.
_UNKNOWN_VERSION = "0+unknown"


def package_version() -> str:
    """Version of the installed distribution, or a clear placeholder."""
    try:
        return version(_DISTRIBUTION)
    except PackageNotFoundError:  # pragma: no cover - source-tree fallback
        return _UNKNOWN_VERSION


def build(mailto: str | None = None) -> str:
    """Build the outbound User-Agent, appending ``mailto`` when configured.

    The descriptive agent is returned whether or not a contact address is
    set — an anonymous-but-identifiable client is still far better than
    ``python-httpx``, and several upstreams throttle the latter specifically.
    """
    agent = f"{_DISTRIBUTION}/{package_version()} (+{_PROJECT_URL}"
    if mailto:
        agent += f"; mailto:{mailto}"
    return agent + ")"


def headers(mailto: str | None = None) -> dict[str, str]:
    """Request headers carrying the shared User-Agent."""
    return {"User-Agent": build(mailto)}
