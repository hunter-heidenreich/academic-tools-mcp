"""Leaf shape guards for untyped JSON trees returned by upstream providers.

Provider responses arrive as ``Any`` below the top-level envelope: a field
documented as an object, array, string, or integer may be ``null``, a scalar of
another type, or a boolean (which in Python is an :class:`int` subclass). Every
nested read goes through these guards so a malformed record degrades to ``None``,
``""``, ``{}``, or ``[]`` instead of raising ``AttributeError`` or ``TypeError``
past the provider's ``{error, retryable}`` boundary.
"""

from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    """``value`` when it is a ``dict``, else ``{}``."""
    return value if isinstance(value, dict) else {}


def as_int(value: Any) -> int | None:
    """``value`` when it is an ``int`` (and not a ``bool``), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def non_neg_int(value: Any) -> int | None:
    """``value`` when it is a non-negative ``int`` (excluding ``bool``), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def as_bool(value: Any) -> bool | None:
    """``value`` when it is a ``bool``, else ``None``."""
    return value if isinstance(value, bool) else None


def as_str(value: Any) -> str:
    """``value`` when it is a ``str``, else ``""``."""
    return value if isinstance(value, str) else ""


def non_empty_str(value: Any) -> str | None:
    """``value`` when it is a non-empty ``str``, else ``None``."""
    return value if isinstance(value, str) and value else None


def stripped_str(value: Any) -> str | None:
    """``value.strip()`` when ``value`` is a non-blank ``str``, else ``None``."""
    return value.strip() or None if isinstance(value, str) else None


def dict_list(value: Any) -> list[dict[str, Any]]:
    """The ``dict`` elements of ``value``, or ``[]`` when it isn't a ``list``."""
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def str_list(value: Any) -> list[str]:
    """The non-empty ``str`` elements of ``value``, or ``[]`` when it isn't a ``list``."""
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def unwrap_first(value: Any) -> Any:
    """First element of a ``list`` (``None`` when empty), else ``value`` itself.

    Crossref returns ``title`` and ``container-title`` as single-element lists.
    """
    if isinstance(value, list):
        return value[0] if value else None
    return value
