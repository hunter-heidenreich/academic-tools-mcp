"""Environment configuration, loaded from ``.env`` plus the real environment.

Resolution must work from an installed wheel, not only a source checkout — the
package ships an ``academic-tools-mcp`` console script and ``.env.example``
tells operators to set ``CACHE_DIR`` for exactly that case. A single
``<package>/../../../.env`` rule points inside the virtualenv from
``site-packages`` and silently disables every env var there.

Candidates are tried in order and the first that exists wins:

1. ``ACADEMIC_TOOLS_ENV_FILE`` — explicit override, for anyone who needs it.
2. The project root relative to this file — the source-checkout case, kept
   first among the implicit paths so existing setups behave identically.
3. ``$PWD/.env`` — running the server from a directory holding its config.
4. ``$XDG_CONFIG_HOME`` (or ``~/.config``) ``/academic-tools-mcp/.env`` — the
   conventional home for an installed tool's configuration.

Real environment variables always win: ``load_dotenv`` is called without
``override``, so an operator can export a value and have it take effect
regardless of what any file says.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


def _candidate_env_paths() -> list[Path]:
    """Ordered ``.env`` locations to try. See the module docstring."""
    candidates: list[Path] = []

    explicit = os.environ.get("ACADEMIC_TOOLS_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())

    # Source checkout: src/academic_tools_mcp/config.py -> project root.
    candidates.append(Path(__file__).resolve().parent.parent.parent / ".env")

    candidates.append(Path.cwd() / ".env")

    xdg = os.environ.get("XDG_CONFIG_HOME")
    config_home = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    candidates.append(config_home / "academic-tools-mcp" / ".env")

    return candidates


def _load_env() -> Path | None:
    """Load the first ``.env`` that exists. Returns the path used, or None."""
    for path in _candidate_env_paths():
        try:
            if path.is_file():
                load_dotenv(path)
                return path
        except OSError:
            # An unreadable candidate (permissions, a dangling symlink) must
            # not stop us trying the rest.
            continue
    return None


# Resolved once at import. Exposed so an operator can see which file won.
ENV_FILE: Path | None = _load_env()


def get(key: str) -> str | None:
    """Get a config value from the environment.

    Empty strings read as unset, so a commented-out-but-present
    ``CROSSREF_MAILTO=`` behaves the same as omitting the line.
    """
    return os.environ.get(key) or None


# The spelling of "on" an operator may reasonably use in a shell or a .env.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def flag(key: str) -> bool:
    """Whether a boolean config key is enabled.

    The single home for env-var truthiness: anything outside ``_TRUE_VALUES``
    (including unset and empty) is off, so two call sites can't disagree about
    whether ``YES`` or ``on`` counts. Surrounding whitespace is stripped — a
    ``.env`` line with a trailing space is a typo, not a request to disable
    the feature. Read at call time, so a caller that re-checks per request
    picks up a change without a restart.
    """
    return (os.environ.get(key) or "").strip().lower() in _TRUE_VALUES
