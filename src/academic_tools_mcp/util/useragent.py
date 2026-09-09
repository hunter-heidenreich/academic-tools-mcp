"""Shared outbound ``User-Agent`` construction — one home, every client.

Wikimedia, Crossref and OpenAlex all ask for the same shape::

    academic-tools-mcp/<version> (+<project url>; mailto:<contact>)

The version comes from installed distribution metadata, never a literal.
"""

import re
from functools import cache
from importlib.metadata import PackageNotFoundError, version

_PROJECT_URL = "https://github.com/hunter-heidenreich/academic-tools-mcp"
_DISTRIBUTION = "academic-tools-mcp"

# Advertised from an uninstalled source tree; never gates behaviour.
_UNKNOWN_VERSION = "0+unknown"

# Outside printable ASCII, plus the parens that delimit the User-Agent comment.
_UNSAFE_IN_MAILTO = re.compile(r"[^\x20-\x7e]|[()]")

_MAILTO_PREFIX = "mailto:"


@cache
def package_version() -> str:
    """Version of the installed distribution, or a clear placeholder.

    Cached because every ``_get_client`` rebuilds its headers per request.
    """
    try:
        return version(_DISTRIBUTION)
    except PackageNotFoundError:
        return _UNKNOWN_VERSION


def normalize_mailto(mailto: str | None) -> str | None:
    """Scrub an operator-supplied contact, or ``None`` if nothing survives.

    Invariant: scrub before stripping the prefix, in a loop — scrubbing can
    reveal one (``mail(to:x`` → ``mailto:x``), so the loop keeps it idempotent.
    """
    if not mailto:
        return None
    value = _UNSAFE_IN_MAILTO.sub("", mailto).strip()
    while value[: len(_MAILTO_PREFIX)].lower() == _MAILTO_PREFIX:
        value = value[len(_MAILTO_PREFIX) :].strip()
    return value or None


def build(mailto: str | None = None) -> str:
    """Build the outbound User-Agent, appending a contact that survives ``normalize_mailto``."""
    contact = normalize_mailto(mailto)
    suffix = f"; {_MAILTO_PREFIX}{contact}" if contact else ""
    return f"{_DISTRIBUTION}/{package_version()} (+{_PROJECT_URL}{suffix})"


def headers(mailto: str | None = None) -> dict[str, str]:
    """Request headers carrying the shared User-Agent."""
    return {"User-Agent": build(mailto)}
