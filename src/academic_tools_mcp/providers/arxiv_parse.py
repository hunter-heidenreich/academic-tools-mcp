"""Pure arXiv ID normalization and Atom / OAI-PMH XML parsing helpers."""

import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

from ..util import doinorm

_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
_OAI_NS = "http://www.openarchives.org/OAI/2.0/"
_ARXIV_RAW_NS = "http://arxiv.org/OAI/arXivRaw/"


# ---------------------------------------------------------------------------
# ID normalization
# ---------------------------------------------------------------------------

# As permissive as ``doinorm._DOI_URL_RE``: a spelling it misses files the same paper twice.
_ARXIV_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/([^?#]+?)(?:\.pdf)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# arXiv's DataCite registrant prefix, public as ``acl.ACL_DOI_PREFIX`` is: one
# spelling, not two.
ARXIV_DOI_PREFIX = "10.48550/arXiv."
_ARXIV_DOI_RE = re.compile(rf"^{re.escape(ARXIV_DOI_PREFIX)}(.+)$", re.IGNORECASE)

# Exported: ``corpus`` matches the old-style form over ``safe_stem``'s ``_``, not ``/``.
# ``math.GT``/``cond-mat.stat-mech`` are why the archive class carries ``.``.
OLD_ARCHIVE_PATTERN = r"[a-z][a-z.\-]*"
_VERSION_PATTERN = r"(?:v\d+)?"
OLD_NUMBER_PATTERN = rf"\d{{7}}{_VERSION_PATTERN}"
_NEW_ID_PATTERN = rf"\d{{4}}\.\d{{4,5}}{_VERSION_PATTERN}"

_NEW_ID_RE = re.compile(rf"^{_NEW_ID_PATTERN}$")
_OLD_ID_RE = re.compile(rf"^{OLD_ARCHIVE_PATTERN}/{OLD_NUMBER_PATTERN}$")
_VERSION_SUFFIX_RE = re.compile(r"v\d+$")


def _is_arxiv_shape(candidate: str) -> bool:
    """Whether an already-bare id matches either arXiv grammar, ignoring case."""
    lowered = candidate.lower()
    return bool(_NEW_ID_RE.match(lowered) or _OLD_ID_RE.match(lowered))


def normalize_arxiv_id(arxiv_id: str) -> str:
    """Normalize an arXiv identifier to a bare ID (with version if present).

    Accepts:
      - bare ID: 2301.00001, 2301.00001v2, hep-th/9901001
      - ``arXiv:`` prefix in any case, with or without a space
      - an ``abs``/``pdf``/``html`` URL, either scheme (or none), optional ``www.`` /
        ``export.`` host label, optional ``.pdf`` extension and trailing slash
      - arXiv's DataCite DOI (``10.48550/arXiv.…``), in any spelling ``doinorm.normalize`` takes

    Case is preserved (``canonical_arxiv_id`` owns the fold); an unrecognised string
    comes back stripped of whitespace and any ``arXiv:`` prefix. **Idempotent for every
    input** — the step order below is what keeps it that way.
    """
    arxiv_id = arxiv_id.strip()

    while arxiv_id[:6].lower() == "arxiv:":
        arxiv_id = arxiv_id[6:].strip()

    if m := _ARXIV_URL_RE.match(arxiv_id):
        return m.group(1)

    # Through the shared normalizer, so ``doi.org``/``doi:`` spellings collapse too. Only
    # an arXiv-shaped tail: an unrelated DataCite record must survive, and nesting stay
    # idempotent.
    if (m := _ARXIV_DOI_RE.match(doinorm.normalize(arxiv_id))) and _is_arxiv_shape(m.group(1)):
        return m.group(1)

    return arxiv_id


def canonical_arxiv_id(arxiv_id: str) -> str:
    """Cache key for an arXiv ID: ``normalize_arxiv_id`` plus a lowercase fold.

    **Version included, deliberately.** The fetch keeps it, so a stripped key
    serves whichever version was requested first to every later caller.
    """
    return normalize_arxiv_id(arxiv_id).lower()


def is_arxiv_id(identifier: str) -> bool:
    """The shape test ``manual.resolve_target`` routes on, over the canonical form.

    An id this rejects lands in ``manual`` under a key already arXiv's, so the
    same paper caches, downloads and converts twice.
    """
    return _is_arxiv_shape(canonical_arxiv_id(identifier))


def strip_version(arxiv_id: str) -> str:
    """Drop a trailing ``v<n>``, preserving case — BibTeX's ``eprint`` keeps ``math.GT``."""
    return _VERSION_SUFFIX_RE.sub("", arxiv_id)


def base_arxiv_id(arxiv_id: str) -> str:
    """The "latest" cache key, which a bare request uses. Separate so callers must choose."""
    return strip_version(canonical_arxiv_id(arxiv_id))


def _version_number(arxiv_id: str) -> int:
    """The ``v<n>`` suffix as an int; 0 for a bare id."""
    m = _VERSION_SUFFIX_RE.search(arxiv_id)
    return int(m.group()[1:]) if m else 0


def id_from_entry(paper: dict[str, Any]) -> str:
    """The bare, versioned ID from an Atom entry's ``id`` URL. Never a local ``split``.

    ``isinstance``, not truthiness: a non-string ``id`` degrades to ``""`` instead of
    reaching ``.strip()`` as an AttributeError.
    """
    raw = paper.get("id")
    return normalize_arxiv_id(raw) if isinstance(raw, str) else ""


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------


def _is_error_entry(entry: ET.Element) -> bool:
    """arXiv answers a bad id *or* a bad query with an ``api/errors`` entry, under 400 or 200.

    Shared so ``search_papers`` classifies it the way ``get_paper`` does.
    """
    id_el = entry.find(f"{{{_ATOM_NS}}}id")
    return id_el is not None and "api/errors" in (id_el.text or "")


def _total_results(root: ET.Element) -> int | None:
    """A feed's ``opensearch:totalResults``, or ``None`` when absent or not a count.

    ``isdecimal``, not ``isdigit``: a superscript passes ``isdigit`` and raises in ``int()``.
    """
    total_el = root.find(f"{{{_OPENSEARCH_NS}}}totalResults")
    text = total_el.text.strip() if total_el is not None and total_el.text else ""
    return int(text) if text.isdecimal() else None


def _parse_entry(entry: ET.Element) -> dict[str, Any]:
    """Parse a single Atom <entry> element into a dict."""

    def _text(tag: str, ns: str = _ATOM_NS) -> str | None:
        el = entry.find(f"{{{ns}}}{tag}")
        if el is not None and el.text:
            return el.text.strip()
        return None

    authors = []
    for author_el in entry.findall(f"{{{_ATOM_NS}}}author"):
        name_el = author_el.find(f"{{{_ATOM_NS}}}name")
        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        affiliations = [
            aff.text.strip() for aff in author_el.findall(f"{{{_ARXIV_NS}}}affiliation") if aff.text
        ]
        authors.append({"name": name, "affiliations": affiliations})

    links = []
    for link_el in entry.findall(f"{{{_ATOM_NS}}}link"):
        links.append(
            {
                "href": link_el.get("href", ""),
                "rel": link_el.get("rel", ""),
                "title": link_el.get("title") or None,
            }
        )

    categories = [
        cat.get("term", "") for cat in entry.findall(f"{{{_ATOM_NS}}}category") if cat.get("term")
    ]

    primary_cat_el = entry.find(f"{{{_ARXIV_NS}}}primary_category")
    primary_category = primary_cat_el.get("term", "") if primary_cat_el is not None else ""

    raw_id = _text("id") or ""

    # Titles and abstracts arrive hard-wrapped; collapse the newlines.
    raw_title = _text("title") or ""
    raw_summary = _text("summary") or ""

    return {
        "id": raw_id,
        "title": " ".join(raw_title.split()),
        "summary": " ".join(raw_summary.split()),
        "published": _text("published") or "",
        "updated": _text("updated") or "",
        "authors": authors,
        "categories": categories,
        "primary_category": primary_category,
        "links": links,
        "comment": _text("comment", _ARXIV_NS),
        "journal_ref": _text("journal_ref", _ARXIV_NS),
        "doi": _text("doi", _ARXIV_NS),
    }


def _iso_utc(rfc2822: str | None) -> str | None:
    """An OAI ``Mon, 12 Jun 2017 17:57:34 GMT`` date in the Atom feed's ISO 8601 form."""
    if not rfc2822:
        return None
    try:
        return parsedate_to_datetime(rfc2822).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


def _parse_versions(root: ET.Element) -> dict[str, Any] | None:
    """An ``arXivRaw`` record as ``{arxiv_id, submitter, license, versions}``, or ``None``."""
    raw = root.find(f".//{{{_ARXIV_RAW_NS}}}arXivRaw")
    if raw is None:
        return None

    def _text(el: ET.Element, tag: str) -> str | None:
        child = el.find(f"{{{_ARXIV_RAW_NS}}}{tag}")
        return child.text.strip() if child is not None and child.text else None

    versions = [
        {
            "version": v.get("version") or None,
            "date": _iso_utc(_text(v, "date")),
            "size": _text(v, "size"),
        }
        for v in raw.findall(f"{{{_ARXIV_RAW_NS}}}version")
    ]
    return {
        "arxiv_id": _text(raw, "id") or "",
        "submitter": _text(raw, "submitter"),
        "license": _text(raw, "license"),
        "versions": versions,
    }
