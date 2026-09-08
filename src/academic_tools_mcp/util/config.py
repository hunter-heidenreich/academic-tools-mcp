"""Environment configuration, loaded from ``.env`` plus the real environment.

Resolution must work from an installed wheel, not only a source checkout — the
package ships an ``academic-tools-mcp`` console script and ``.env.example``
tells operators to set ``CACHE_DIR`` for exactly that case. A single
``<package>/../../../.env`` rule points inside the virtualenv from
``site-packages`` and silently disables every env var there.

``ACADEMIC_TOOLS_ENV_FILE`` is authoritative: when it is set it is the *only*
candidate, so a typo'd path means "no ``.env``" rather than a silent fallback
to a different operator's config. Otherwise candidates are tried in order and
the first that exists wins:

1. The project root relative to this file — the source-checkout case, kept
   first among the implicit paths so existing setups behave identically.
2. ``$PWD/.env`` — running the server from a directory holding its config.
3. ``$XDG_CONFIG_HOME`` (or ``~/.config``) ``/academic-tools-mcp/.env`` — the
   conventional home for an installed tool's configuration.

Real environment variables always win: ``load_dotenv`` is called without
``override``, so an operator can export a value and have it take effect
regardless of what any file says.
"""

import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

from dotenv import load_dotenv


def get(key: str) -> str | None:
    """Get a config value from the environment.

    Surrounding whitespace is stripped and an empty result reads as unset, so a
    commented-out-but-present ``CROSSREF_MAILTO=`` and a line with a trailing
    space both behave the same as omitting the line. Interior whitespace
    survives — converter templates and paths are full of it.
    """
    return (os.environ.get(key) or "").strip() or None


# The spelling of "on" an operator may reasonably use in a shell or a .env.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def flag(key: str) -> bool:
    """Whether a boolean config key is enabled.

    The single home for env-var truthiness: anything outside ``_TRUE_VALUES``
    (including unset, empty and whitespace-only, which ``get`` folds together)
    is off, so two call sites can't disagree about whether ``YES`` or ``on``
    counts. Read at call time, so a caller that re-checks per request picks up
    a change without a restart.
    """
    return (get(key) or "").lower() in _TRUE_VALUES


# The spelling of "off" for a setting whose whole point is a limit — the
# numeric sibling of _TRUE_VALUES, and single-homed for the same reason.
_DISABLE_VALUES = frozenset({"none", "off", "disabled", "0"})

_Number = TypeVar("_Number", int, float)


def number(
    key: str,
    default: _Number,
    *,
    cast: Callable[[str], _Number],
    on_nonpositive: Literal["default", "disable"],
) -> _Number | None:
    """Resolve a numeric setting an operator can turn off.

    Returns the parsed value, ``None`` when the guard is disabled, or
    ``default`` when the value is absent or unusable. ``_DISABLE_VALUES`` is
    the only vocabulary that disables outright.

    ``on_nonpositive`` decides what a parsed ``<= 0`` means, and the two
    callers genuinely differ: a negative ``MAX_PDF_BYTES`` is a typo that must
    not silently drop the disk guard (``"default"``), while a non-positive
    timeout is a second disable idiom (``"disable"``). Passing it explicitly
    keeps that a deliberate choice rather than a copy that drifted.

    A non-finite float falls back to the default: ``nan`` compares false
    against every bound, so honouring it would hand the caller a limit nothing
    can satisfy. Guarded by type, not by calling ``math.isfinite`` on whatever
    ``cast`` returned — an ``int`` is finite by construction, and one too large
    for a float raises ``OverflowError`` on the way in.
    """
    raw = get(key)
    if raw is None:
        return default
    raw = raw.lower()
    if raw in _DISABLE_VALUES:
        return None
    try:
        value = cast(raw)
    except ValueError:
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    if value > 0:
        return value
    return None if on_nonpositive == "disable" else default


def _expand(value: str) -> Path:
    return Path(value).expanduser()


def _xdg_config_home() -> Path:
    xdg = get("XDG_CONFIG_HOME")
    return _expand(xdg) if xdg else Path.home() / ".config"


def _candidate_env_paths() -> list[Path]:
    """Ordered ``.env`` locations to try. See the module docstring."""
    explicit = get("ACADEMIC_TOOLS_ENV_FILE")
    builders: list[Callable[[], Path]]
    if explicit:
        builders = [lambda: _expand(explicit)]
    else:
        builders = [
            # Source checkout: src/academic_tools_mcp/config.py -> project root.
            lambda: Path(__file__).resolve().parent.parent.parent / ".env",
            lambda: Path.cwd() / ".env",
            lambda: _xdg_config_home() / "academic-tools-mcp" / ".env",
        ]

    candidates: list[Path] = []
    for build in builders:
        try:
            candidates.append(build())
        except (OSError, RuntimeError):
            # Path.cwd() raises on a deleted working directory and
            # expanduser() on an unresolvable home. Neither is worth aborting
            # the import over, let alone skipping the remaining candidates.
            continue
    return candidates


def _load_env() -> Path | None:
    """Load the first ``.env`` that exists. Returns the path used, or None."""
    for path in _candidate_env_paths():
        try:
            if path.is_file():
                load_dotenv(path)
                return path
        except (OSError, UnicodeDecodeError):
            # An unreadable candidate (permissions, a dangling symlink) or one
            # that isn't UTF-8 must not stop us trying the rest. UnicodeDecode-
            # Error escapes load_dotenv itself and would abort the import.
            continue
    return None


# Resolved once at import. Reported by ``get_server_stats`` so an operator can
# see which file won.
ENV_FILE: Path | None = _load_env()
