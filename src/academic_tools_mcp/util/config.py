"""Environment configuration, loaded from ``.env`` plus the real environment.

Resolution has to work from an installed wheel, not just a source checkout —
the package ships an ``academic-tools-mcp`` console script, and ``.env.example``
tells operators to set ``CACHE_DIR`` for exactly that case. Any single
project-relative rule points inside the virtualenv once the package lives in
``site-packages``, silently disabling every env var there; hence a candidate
list rather than one path.

``ACADEMIC_TOOLS_ENV_FILE`` is authoritative: set, it is the *only* candidate,
so a typo'd path means "no ``.env``" rather than a silent fallback to a
different operator's config. Otherwise the first candidate that exists wins:

1. :func:`project_root` ``/.env`` — the source checkout's own config, ahead of
   whatever directory the server happens to be started from.
2. ``$PWD/.env`` — running the server from a directory holding its config.
3. ``$XDG_CONFIG_HOME`` (or ``~/.config``) ``/academic-tools-mcp/.env`` — the
   conventional home for an installed tool's configuration.

Real environment variables always win: ``load_dotenv`` runs without
``override``, so an exported value applies whatever any file says.
"""

import math
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

from dotenv import load_dotenv


def get(key: str) -> str | None:
    """Get a config value from the environment.

    Surrounding whitespace is stripped and an empty result reads as unset: a
    blank or whitespace-only ``CROSSREF_MAILTO=`` behaves exactly like omitting
    the line. Interior whitespace survives — converter templates and paths are
    full of it.
    """
    return (os.environ.get(key) or "").strip() or None


# The spellings of "on" an operator may reasonably use in a shell or a .env.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def flag(key: str) -> bool:
    """Whether a boolean config key is enabled.

    The single home for env-var truthiness: anything outside ``_TRUE_VALUES``
    is off, case-insensitively — including the unset, empty and whitespace-only
    that ``get`` folds together — so two call sites can't disagree about ``YES``.
    Read at call time, so a caller re-checking per request needs no restart.
    """
    return (get(key) or "").lower() in _TRUE_VALUES


# The spellings of "off" for a setting whose point is a limit — _TRUE_VALUES' numeric sibling.
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

    Returns the parsed value, ``None`` when disabled, or
    ``default`` when the value is absent or unusable. ``_DISABLE_VALUES`` is
    the only vocabulary that disables outright.

    ``on_nonpositive`` decides what a parsed ``<= 0`` means, and the two
    callers genuinely differ: a negative ``MAX_PDF_BYTES`` is a typo that must
    not silently drop the disk guard (``"default"``), while a non-positive
    timeout is a second disable idiom (``"disable"``). Passing it explicitly
    keeps that a deliberate choice rather than a copy that drifted.

    A non-finite float falls back to the default: ``nan`` compares false
    against every bound, so honouring it hands the caller a limit nothing can
    satisfy. Guarded by ``isinstance``, not by calling ``math.isfinite`` on
    whatever ``cast`` returned — an ``int`` is finite by construction, and one
    too large for a float raises ``OverflowError`` inside ``math.isfinite``.
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


def project_root() -> Path:
    """The directory holding ``src/``, found by name rather than by counting.

    One home, because the two callers sit at different depths: this module's
    source-checkout ``.env`` candidate and ``store.cache``'s default ``.cache/``.
    ``parents[n]`` encodes where the *caller* sits, so moving either module a
    directory down silently relocates what it resolves — a ``.env`` that stops
    being read, a ``.cache/`` that orphans every artifact already in it.
    """
    package = __name__.split(".", 1)[0]
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == package:
            return parent.parent.parent  # <package>/ -> src/ -> project root
    raise RuntimeError(f"{here} is not inside a directory named {package!r}")


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
            lambda: project_root() / ".env",
            lambda: Path.cwd() / ".env",
            lambda: _xdg_config_home() / "academic-tools-mcp" / ".env",
        ]

    candidates: list[Path] = []
    for build in builders:
        try:
            candidates.append(build())
        except (OSError, RuntimeError):
            # Invariant: no candidate may abort the import. RuntimeError from
            # project_root() or an unresolvable home, OSError from a deleted cwd.
            continue
    return candidates


def _load_env() -> Path | None:
    """Load the first ``.env`` that exists. Returns the path used, or ``None``."""
    for path in _candidate_env_paths():
        try:
            if path.is_file():
                load_dotenv(path)
                return path
        except (OSError, UnicodeDecodeError):
            # Same invariant: an unreadable candidate (permissions, dangling symlink) or a
            # non-UTF-8 one — UnicodeDecodeError is not an OSError and escapes load_dotenv.
            continue
    return None


# Resolved once at import; read by stats.snapshot so an operator can see which file won.
ENV_FILE: Path | None = _load_env()
