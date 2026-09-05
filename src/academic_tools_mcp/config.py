"""Environment configuration, loaded from ``.env`` plus the real environment.

The ``.env`` file used to be resolved as ``<package>/../../../.env`` — correct
for a source checkout (``src/academic_tools_mcp/`` → project root) and
meaningless from ``site-packages``, where it points somewhere inside the
virtualenv. That silently disabled every env var for an installed wheel, which
is a supported mode: ``pyproject.toml`` ships an ``academic-tools-mcp`` console
script and ``.env.example`` explicitly tells operators to set ``CACHE_DIR``
"when running from an installed wheel".

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
