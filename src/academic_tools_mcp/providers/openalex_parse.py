"""Pure identifier normalization, URL safety, and response-parsing helpers for OpenAlex.

Extracted from ``providers/openalex.py`` so pure shape/ID logic stays separate from
HTTP client lifecycle, rate-limiting, single-flight coalescing, and cache I/O.
"""

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from ..net import http
from ..util import doinorm, orcidnorm
from ..util.jsonshape import non_neg_int as _non_neg_int


def canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return doinorm.canonical(doi)


def _usable_http_url(value: Any) -> str | None:
    """Return a stripped ``http(s)`` URL with a valid public host and port, or None."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if http.is_public_http_url(candidate) else None


def best_pdf_url(work: dict[str, Any]) -> str | None:
    """Pick the best open-access *PDF* URL from a raw OpenAlex work, or None.

    A direct PDF link over a landing page, in order: ``best_oa_location``'s
    (OpenAlex's chosen best OA copy), ``primary_location``'s (the version of
    record, if OA), then ``open_access.oa_url`` — frequently a landing page.

    Type- and scheme-checked all the way down, not annotation-trusted: this is
    the OA download trust boundary, and whatever it returns is fetched. A
    non-HTTP, hostless or whitespace-only entry falls through to the next
    candidate rather than shadowing a usable fallback.
    """
    for obj_key, url_key in (
        ("best_oa_location", "pdf_url"),
        ("primary_location", "pdf_url"),
        ("open_access", "oa_url"),
    ):
        obj = work.get(obj_key)
        if not isinstance(obj, dict):
            continue
        url = _usable_http_url(obj.get(url_key))
        if url is not None:
            return url
    return None


# The openalex.org URL spellings an entity ID is pasted in — the latitude
# ``doinorm._DOI_URL_RE`` and ``arxiv._ARXIV_URL_RE`` carry, for the same reason.
# Gated on an entity-shaped tail, so an ORCID URL falls through untouched.
_OPENALEX_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.|api\.)?openalex\.org/(?:\w+/)?([a-z]\d+)/?$",
    re.IGNORECASE,
)


def _normalize_author_id(author_id: str) -> str:
    """Normalize an author identifier to its bare form.

    A bare OpenAlex ID (``A5023888391``), any openalex.org URL
    ``_OPENALEX_URL_RE`` covers, or any ORCID spelling ``orcidnorm`` folds.
    """
    author_id = author_id.strip()
    if m := _OPENALEX_URL_RE.match(author_id):
        return m.group(1)
    if orcidnorm.looks_like_orcid(author_id):
        return orcidnorm.normalize(author_id)
    return author_id


def _author_path_id(bare_id: str) -> str:
    """The encoded path segment OpenAlex resolves; a bare ORCID 404s, so it takes ``orcid:``.

    Takes the already-normalized id rather than re-deriving it, so the value its caller
    shape-checks is the value that reaches the URL.
    """
    if orcidnorm.looks_like_orcid(bare_id):
        return f"orcid:{quote(bare_id, safe='')}"
    return quote(bare_id, safe="")


# A ROR is `0`, six crockford-base32 characters, then two check digits. The alphabet
# excludes `i`/`l`/`o`/`u`, which is what keeps this from matching a bare OpenAlex ID.
_ROR_RE = re.compile(r"^0[0-9a-hj-km-np-tv-z]{6}\d{2}$", re.IGNORECASE)
_ROR_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?ror\.org/", re.IGNORECASE)


def _looks_like_ror(value: str) -> bool:
    """Whether ``value`` is a bare ROR, once any URL or scheme prefix is off."""
    return bool(_ROR_RE.match(value))


def _strip_repeated_prefix(value: str, prefix: str) -> str:
    """Strip one or more case-insensitive ``prefix`` occurrences from the start of ``value``."""
    n = len(prefix)
    while value[:n].lower() == prefix:
        value = value[n:].strip()
    return value


def _normalize_institution_id(institution_id: str) -> str:
    """Normalize an institution identifier to its bare form.

    A bare OpenAlex ID (``I27837315``), any openalex.org URL ``_OPENALEX_URL_RE``
    covers, or a ROR in any spelling — bare, ``ror:``-prefixed, or a ror.org URL.
    Idempotent.
    """
    institution_id = institution_id.strip()
    if m := _OPENALEX_URL_RE.match(institution_id):
        return m.group(1)

    institution_id = _strip_repeated_prefix(institution_id, "ror:")
    # The trailing slash too, as `_OPENALEX_URL_RE` tolerates on the other spelling.
    return _ROR_URL_RE.sub("", institution_id).strip().rstrip("/")


def _institution_path_id(bare_id: str) -> str:
    """The encoded path segment OpenAlex resolves; a bare ROR takes ``ror:``, as an ORCID does.

    Takes the already-normalized id, as ``_author_path_id`` does.
    """
    if _looks_like_ror(bare_id):
        return f"ror:{quote(bare_id, safe='')}"
    return quote(bare_id, safe="")


def canonical_institution_id(institution_id: str) -> str:
    """Return a canonical institution ID for cache keying."""
    return _normalize_institution_id(institution_id).lower()


def canonical_author_id(author_id: str) -> str:
    """Return a canonical author ID for cache keying."""
    return _normalize_author_id(author_id).lower()


# PubMed's own URL, plus the legacy ``/pubmed/`` path older papers still print.
# As permissive as ``_ARXIV_URL_RE``, for the same reason.
_PUBMED_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)"
    r"/(\d+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# The whole live PMID range fits in 8 digits; ``is_pmid`` owns the bare-run floor.
_PMID_RE = re.compile(r"^\d{1,8}$")

# Live PMIDs are 7-8 digits, and a shorter bare run is likelier a hand-written label than
# one of the oldest records. Sampled, not documented, as ``_BARE_WORK_ID_FLOOR`` is.
_BARE_PMID_FLOOR = 7

# The entity letter ``_OPENALEX_URL_RE`` deliberately does not check.
# ``is_work_id`` owns the bare-run floor, as with PMIDs.
_WORK_ID_RE = re.compile(r"^W\d{1,12}$", re.IGNORECASE)

# Live work ids run 8-10 digits; nothing shorter resolves. Sampled, not documented.
_BARE_WORK_ID_FLOOR = 9

# NCBI PMC and Europe PMC article URLs.
_PMCID_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?"
    r"(?:ncbi\.nlm\.nih\.gov/pmc/articles|pmc\.ncbi\.nlm\.nih\.gov/articles|europepmc\.org/article/pmc)"
    r"/(PMC\d+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# Canonical PMCID: PMC followed by digits (up to 10 digits covers all live/future IDs).
_PMCID_RE = re.compile(r"^PMC\d{1,10}$", re.IGNORECASE)


def _matches_tiered_id(
    identifier: str,
    *,
    normalize: Callable[[str], str],
    pattern: re.Pattern[str],
    bare_floor: int,
) -> bool:
    """Two-tier shape test: any valid length when prefixed/URL-wrapped, ``>= bare_floor`` when bare."""
    stripped = identifier.strip()
    normalized = normalize(stripped)
    if not pattern.match(normalized):
        return False
    return normalized != stripped or len(normalized) >= bare_floor


def normalize_pmid(pmid: str) -> str:
    """Normalize a PubMed identifier to bare digits.

    Accepts a bare run, an any-case ``pmid:`` prefix (OpenCitations' own
    spelling) and a PubMed URL per ``_PUBMED_URL_RE``. Anything unrecognised
    comes back stripped of whitespace and any prefix; ``is_pmid`` decides
    whether that is an error. Idempotent.
    """
    pmid = _strip_repeated_prefix(pmid.strip(), "pmid:")
    if m := _PUBMED_URL_RE.match(pmid):
        return m.group(1)
    return pmid


def is_pmid(identifier: str) -> bool:
    """The shape test the tool layer resolves on, over the normalized form.

    **Two tiers, deliberately.** An explicit ``pmid:`` prefix or PubMed URL is
    unambiguous, so any 1-8 digit id is claimed. A *bare* run is claimed only from
    ``_BARE_PMID_FLOOR`` up, so a freeform ``import_paper(file, "1234")`` label keeps
    routing to ``manual``.
    """
    return _matches_tiered_id(
        identifier,
        normalize=normalize_pmid,
        pattern=_PMID_RE,
        bare_floor=_BARE_PMID_FLOOR,
    )


def normalize_work_id(work_id: str) -> str:
    """Normalize an OpenAlex work identifier to its bare form (``W4312223440``).

    Accepts a bare ID, an any-case ``openalex:`` prefix (OpenCitations' own spelling
    on every graph row) and any openalex.org URL ``_OPENALEX_URL_RE`` covers. Anything
    unrecognised comes back stripped of whitespace and any prefix; ``is_work_id``
    decides whether that is an error. Idempotent.
    """
    work_id = _strip_repeated_prefix(work_id.strip(), "openalex:")
    if m := _OPENALEX_URL_RE.match(work_id):
        return m.group(1)
    return work_id


def is_work_id(identifier: str) -> bool:
    """The shape test the tool layer resolves on, over the normalized form.

    **Two tiers, as with ``is_pmid``.** An explicit ``openalex:`` prefix or an
    openalex.org URL is unambiguous, so any work-shaped id is claimed. A *bare* run is
    claimed only from ``_BARE_WORK_ID_FLOOR`` up, so a freeform
    ``import_paper(file, "W2024")`` label keeps routing to ``manual``.
    """
    return _matches_tiered_id(
        identifier,
        normalize=normalize_work_id,
        pattern=_WORK_ID_RE,
        bare_floor=_BARE_WORK_ID_FLOOR,
    )


def canonical_work_id(work_id: str) -> str:
    """Cache-key form: ``normalize_work_id`` plus a case fold, as the other two IDs do."""
    return normalize_work_id(work_id).lower()


def normalize_pmcid(pmcid: str) -> str:
    """Normalize a PubMed Central identifier to canonical ``PMC<digits>`` form.

    Accepts bare ``PMC1234567`` (any case), ``pmc:`` / ``pmcid:`` prefixes (with
    or without the ``PMC`` letters), and NCBI / Europe PMC article URLs.
    Idempotent.
    """
    value = pmcid.strip()
    if m := _PMCID_URL_RE.match(value):
        return f"PMC{m.group(1)[3:]}"
    had_prefix = False
    for prefix in ("pmcid:", "pmc:"):
        stripped = _strip_repeated_prefix(value, prefix)
        if stripped != value:
            had_prefix = True
            value = stripped
    if m := _PMCID_URL_RE.match(value):
        return f"PMC{m.group(1)[3:]}"
    if had_prefix and value.isdecimal():
        return f"PMC{value}"
    if _PMCID_RE.match(value):
        return f"PMC{value[3:]}"
    return value


def is_pmcid(identifier: str) -> bool:
    """Return True when *identifier* is a PMCID (bare ``PMC...``, prefixed, or PMC URL)."""
    return bool(_PMCID_RE.match(normalize_pmcid(identifier)))


def canonical_pmcid(pmcid: str) -> str:
    """Cache-key form for PMCIDs: lowercased ``pmc<digits>``."""
    return normalize_pmcid(pmcid).lower()


# The resolver prefixes a *response* DOI can carry: ``doinorm._DOI_URL_RE``'s host
# set, deliberately without its DOI-shape requirement on the tail.
_RESPONSE_DOI_URL_RE = re.compile(r"^https?://(?:dx\.|www\.)?doi\.org/", re.IGNORECASE)


def _canonical_from_response_doi(work_doi: Any) -> str | None:
    """The canonical bare DOI from an OpenAlex work's ``doi``, or None.

    Maps a batch response back to the keys we asked for, so it strips the
    resolver prefix *unconditionally* where ``doinorm.canonical`` strips only a
    DOI-shaped path. ``Any``: this runs outside any ``except``.
    """
    if not isinstance(work_doi, str):
        return None
    return _RESPONSE_DOI_URL_RE.sub("", work_doi.strip()).lower() or None


def _meta_count(data: dict[str, Any]) -> int | None:
    """The non-negative integer ``meta.count`` from a list response, or ``None``."""
    meta = data.get("meta")
    return _non_neg_int(meta.get("count")) if isinstance(meta, dict) else None


def _extract_batch_pmid(work: dict[str, Any]) -> str | None:
    """The normalized bare PMID from an OpenAlex work's ``ids.pmid``, or None."""
    ids = work.get("ids")
    raw_pmid = ids.get("pmid") if isinstance(ids, dict) else None
    if not isinstance(raw_pmid, str) or not raw_pmid:
        return None
    normalized = normalize_pmid(raw_pmid)
    return normalized if _PMID_RE.match(normalized) else None


def _extract_batch_work_id(work: dict[str, Any]) -> str | None:
    """The canonical bare work ID from an OpenAlex work's ``id``, or None."""
    raw_id = work.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        return None
    canonical = canonical_work_id(raw_id)
    return canonical if _WORK_ID_RE.match(canonical) else None


def _extract_batch_pmcid(work: dict[str, Any]) -> str | None:
    """The canonical bare PMCID from an OpenAlex work's ``ids.pmcid``, or None."""
    ids = work.get("ids")
    raw_pmcid = ids.get("pmcid") if isinstance(ids, dict) else None
    if not isinstance(raw_pmcid, str) or not raw_pmcid:
        return None
    canonical = canonical_pmcid(raw_pmcid)
    return canonical if _PMCID_RE.match(canonical) else None


def reconstruct_abstract(inverted_index: Any) -> str:
    """Reconstruct plain text from OpenAlex's inverted index abstract format.

    ``Any``: a malformed index reconstructs to ``""`` rather than raising past
    ``get_paper_abstract``, which has no ``except``.
    """
    if not isinstance(inverted_index, dict):
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        word_positions.extend(
            (idx, word) for pos in positions if (idx := _non_neg_int(pos)) is not None
        )
    word_positions.sort()
    return " ".join(word for _, word in word_positions)
