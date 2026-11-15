"""Pure identifier normalization and response-parsing helpers for Crossref.

Extracted from ``providers/crossref.py`` so pure JATS/author/update/relation/envelope
parsing stays separate from HTTP client lifecycle, pool promotion, and cache I/O.
"""

import html
import re
from typing import Any

from ..util import doinorm
from ..util.jsonshape import (
    as_dict,
    as_str,
    dict_list as _dicts,
    non_empty_str,
    str_or_none as _str_or_none,
)


def canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return doinorm.canonical(doi)


def author_name(author: Any) -> str:
    """``"First Last"`` from a Crossref author entry, or its organisation ``name``.

    Crossref splits a personal name into ``given``/``family`` where OpenAlex and
    arXiv give one string. Rejoining is this module's business, so ``bibtex``'s
    surname rule and ``get_paper_authors``' name field cannot disagree about a
    name. ``Any``, not ``dict``: the entries come from untyped JSON.
    """
    if not isinstance(author, dict):
        return ""
    given = non_empty_str(author.get("given"))
    if family := non_empty_str(author.get("family")):
        return f"{given} {family}".strip() if given else family
    return as_str(author.get("name"))


def institution_name(institution: Any) -> str:
    """The awarding institution's name from a Crossref ``institution`` value, or ``""``.

    Crossref deposits it as a *list of objects* (``[{"name": ..., "place": [...]}]``),
    not the list of strings ``title`` and ``container-title`` carry — read alike,
    every dissertation loses its ``school``. ``Any``, not ``dict``: untyped JSON,
    and some deposits carry a bare string.
    """
    if isinstance(institution, list):
        # First *named* entry: a place-only entry ahead of it is a real deposit.
        return next((found for entry in institution if (found := institution_name(entry))), "")
    if isinstance(institution, str):
        return institution
    return as_str(as_dict(institution).get("name"))


# Crossref deposits an abstract as a JATS fragment, so it is markup, not text.
_JATS_TAG_RE = re.compile(r"<[^>]*>")
# A structured abstract's section titles are content ("Background", "Methods"),
# but a lone leading "Abstract" title is the label of the field itself.
_JATS_LABEL_RE = re.compile(r"^\s*abstract[\s:]+", re.IGNORECASE)


def abstract_text(work: dict[str, Any]) -> str | None:
    """A Crossref work's abstract as plain text, or ``None``.

    The counterpart of ``openalex.reconstruct_abstract``: what the provider
    stores is not what an agent can read. Crossref deposits JATS, so tags are
    stripped, entities unescaped and whitespace collapsed — a structured
    abstract keeps its section titles, but the ``<jats:title>Abstract</jats:title>``
    labelling the field is dropped. Shape-guarded: the ``message`` arrives
    verbatim from untyped JSON.
    """
    raw = work.get("abstract")
    if not isinstance(raw, str) or not raw:
        return None
    text = " ".join(html.unescape(_JATS_TAG_RE.sub(" ", raw)).split())
    text = _JATS_LABEL_RE.sub("", text, count=1)
    return text or None


# The update types that make a paper uncitable, as opposed to amended. Crossmark's
# vocabulary spells that class four ways and a withdrawn or removed paper is no more
# citable than a retracted one, so keying on `retraction` alone reads a withdrawal as
# a sound paper — the one answer this data exists to prevent. The amending types
# (correction, corrigendum, erratum, expression_of_concern, clarification, addendum,
# new_edition, new_version) are reported in `updates` and left for the caller to weigh.
RETRACTION_UPDATE_TYPES = frozenset({"retraction", "partial_retraction", "withdrawal", "removal"})


def retracts(update_type: str) -> bool:
    """Whether a notice of this ``type`` withdraws the work rather than amending it.

    Matched case- and separator-insensitively: the vocabulary is underscored, but
    deposit is by hand and a hyphenated ``partial-retraction`` is one typo away.
    """
    return update_type.strip().lower().replace("-", "_") in RETRACTION_UPDATE_TYPES


def updates(work: dict[str, Any]) -> list[dict[str, Any]]:
    """Retraction and correction notices naming this work, from ``updated-by``.

    Crossref documents only the opposite direction — ``update-to``, on the notice — but
    deposits the back-reference here too, so this costs nothing beyond the ``get_work``
    already made. ``source`` separates a publisher's own deposit from the Retraction
    Watch database; neither corrects the other, so both are kept. ``updated`` stays the
    raw ``{"date-parts": …}``, the year being ``app.crossref_date``'s, a layer up.
    """
    found = []
    for entry in _dicts(work.get("updated-by")):
        doi = non_empty_str(entry.get("DOI"))
        update_type = _str_or_none(entry.get("type"))
        # Both are required upstream, and a notice missing either is unciteable.
        if doi is None or update_type is None:
            continue
        found.append(
            {
                "doi": canonical_doi(doi),
                "type": update_type,
                "label": _str_or_none(entry.get("label")),
                "source": _str_or_none(entry.get("source")),
                "updated": as_dict(entry.get("updated")),
            }
        )
    return found


def relations(work: dict[str, Any]) -> dict[str, list[str]]:
    """Related DOIs by relation name (``is-preprint-of``, ``has-preprint``, …).

    ``relation`` is a *hashmap* of name to a list of ``{id-type, id, asserted-by}``, not
    the flat list its neighbours carry — read like ``author``, every relation is lost.
    Non-DOI ids are dropped, every consumer downstream taking a DOI. Deposit is thin, so
    an empty result is silence, not a denial that a preprint exists.
    """
    raw = work.get("relation")
    if not isinstance(raw, dict):
        return {}
    found: dict[str, list[str]] = {}
    for name, entries in raw.items():
        if not isinstance(name, str):
            continue
        dois = [
            canonical_doi(rel_id)
            for rel in _dicts(entries)
            if rel.get("id-type") == "doi" and isinstance(rel_id := rel.get("id"), str) and rel_id
        ]
        if dois:
            found[name] = dois
    return found


def _message_of(data: object) -> dict[str, object] | None:
    """Crossref's ``message`` envelope, or ``None`` for a wrong-shape body.

    The shape ladder's first two rungs, shared by both readers; ``search_works`` adds
    the ``items`` rungs on top.
    """
    if not isinstance(data, dict):
        return None
    message = data.get("message")
    return message if isinstance(message, dict) else None


def _not_found_message(doi: str, agency_label: str | None) -> str:
    """The refusal for a DOI Crossref has no work for, naming the registrar when known."""
    base = f"No work found on Crossref for DOI: {doi}"
    if agency_label is None:
        return base
    return f"{base}. It is registered with {agency_label}, which this server does not index"
