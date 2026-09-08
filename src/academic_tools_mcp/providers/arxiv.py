"""arXiv client: Atom search and metadata, keyed by canonical (versionless) ID."""

import re
import xml.etree.ElementTree as ET
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from ..download import streaming
from ..net import clients, http
from ..net.throttle import Throttle
from ..store import cache, singleflight, stems
from ..util import config, doinorm, useragent

# Both are transient, not "not found" — .claude/rules/providers.md § arxiv.py.
_PARSE_ERRORS = (ET.ParseError, DefusedXmlException)


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable arXiv response — it speaks XML, not JSON."""
    return http.parse_error_dict(LABEL, detail="could not be parsed as XML")


ARXIV_BASE_URL = "https://export.arxiv.org/api/query"
NAMESPACE = "arxiv"

# Agent-facing provider name; every site that names us reads it (providers.md).
LABEL = "arXiv"

# XML namespaces
_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"

# concurrency=1 is arXiv's documented "single connection" rule; 5 pending at a
# 3s gap is 15s of agent-blocking, past which callers get backpressure.
_MAX_CONCURRENT = 1
_MIN_REQUEST_GAP = 3.0
_MAX_PENDING = 5

_single_flight = singleflight.SingleFlight()

# Short: an arXiv id goes live mid-session, so a 404 at 9am should clear by 10am.
_NEG_TTL_SECONDS = 3600.0

# Cache entity for definitive PDF-download failures, on that same short TTL.
_NEG_ENTITY = "downloads"

# PDF downloads are larger than a metadata call; use a generous timeout.
_PDF_TIMEOUT_SECONDS = 60.0

# Exported so ``search_arxiv``'s validation bound isn't a second spelling of it.
MAX_SEARCH_RESULTS = 50

# Long: a record is stable per version. Short enough that a revision still
# surfaces under a bare ("whatever is current") key.
_POSITIVE_TTL_SECONDS = 14 * 86400.0


def _build_headers() -> dict[str, str]:
    """Descriptive User-Agent, mailto or not: arXiv's edge throttles generic ones harder."""
    return useragent.headers(config.get("ARXIV_MAILTO"))


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``clients.get_client``."""
    return clients.get_client(NAMESPACE, headers=_build_headers(), timeout=30.0)


_throttle = Throttle(
    namespace=NAMESPACE,
    label=LABEL,
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
    # Above the shared default: arXiv penalty-boxes an IP for longer than one
    # retry's backoff covers.
    retry_attempts=3,
)


def _request_slot(url: str) -> AbstractAsyncContextManager[None]:
    """arXiv's rate-limit slot (``Throttle.slot``). Module-level: it is a test seam."""
    return _throttle.slot(url)


async def _throttled_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at arXiv's rate. Url-only: ``_get_client`` is the only place to configure it."""
    return await _throttle.get(_get_client(), url, **kwargs)


# ---------------------------------------------------------------------------
# ID normalization
# ---------------------------------------------------------------------------

# As permissive as ``doinorm._DOI_URL_RE``, and for the same reason: a spelling
# this misses is one ``manual`` files the same paper under a second time.
_ARXIV_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:\.pdf)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# arXiv's own DataCite DOI. Exported as ``acl.ACL_DOI_PREFIX`` is — ``bibtex``
# builds its ``eprint`` field from it.
ARXIV_DOI_PREFIX = "10.48550/arXiv."
_ARXIV_DOI_RE = re.compile(rf"^{re.escape(ARXIV_DOI_PREFIX)}(.+)$", re.IGNORECASE)

# The old-style pair is exported: ``cache_search`` matches it over
# ``safe_stem``'s ``_`` rather than ``/``. ``math.GT``/``cond-mat.stat-mech``
# are why the archive class carries ``.``.
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
      - an ``abs``/``pdf`` URL, either scheme (or none), optional ``www.`` /
        ``export.`` host label, optional ``.pdf`` extension and trailing slash
      - arXiv's DataCite DOI, ``10.48550/arXiv.2301.00001``, in any spelling
        ``doinorm.normalize`` accepts

    Case is preserved (``canonical_arxiv_id`` owns the fold) and an
    unrecognised string comes back stripped but untouched. **Idempotent for
    every input**; the step order below is load-bearing to keep it that way.
    """
    arxiv_id = arxiv_id.strip()

    while arxiv_id[:6].lower() == "arxiv:":
        arxiv_id = arxiv_id[6:].strip()

    if m := _ARXIV_URL_RE.match(arxiv_id):
        return m.group(1)

    # Via the shared normalizer, so the ``doi.org`` and ``doi:`` spellings
    # collapse too. Only an arXiv-shaped tail: an unrelated DataCite record
    # must survive, and a nested spelling must stay idempotent.
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
    """The shape test ``manual``'s two dispatchers route on, over the canonical form.

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


def id_from_entry(paper: dict[str, Any]) -> str:
    """The bare, versioned ID from an Atom entry's ``id`` URL. Never a local ``split``.

    ``isinstance``, not truthiness: a non-string ``id`` reaches ``.strip()``
    as an AttributeError rather than degrading to "".
    """
    raw = paper.get("id")
    return normalize_arxiv_id(raw) if isinstance(raw, str) else ""


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------


def _is_error_entry(entry: ET.Element) -> bool:
    """arXiv answers a bad id *or* a bad query with 200 and an ``api/errors`` entry.

    Shared so ``search_papers`` classifies it the way ``get_paper`` does.
    """
    id_el = entry.find(f"{{{_ATOM_NS}}}id")
    return id_el is not None and "api/errors" in (id_el.text or "")


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_paper(arxiv_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a paper by arXiv ID, cached, with concurrent callers coalesced.

    ``force_refresh=True`` drops both cache halves first — the way to re-pull a
    paper whose bare-id entry predates a new version.
    """
    canonical = canonical_arxiv_id(arxiv_id)

    async def _fetch() -> dict[str, Any]:
        def _not_found() -> dict[str, Any]:
            """Definitive absence — arXiv spells it three ways, all cached here."""
            err = http.not_found(f"No paper found for arXiv ID: {arxiv_id}")
            cache.put_negative(NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
            return err

        try:
            response = await _throttled_get(
                ARXIV_BASE_URL,
                params={"id_list": normalize_arxiv_id(arxiv_id)},
            )

            # Before raise_for_status, as every sibling does: via error_dict a
            # 404 would negative-cache a raw body snippet for the hour.
            if response.status_code == 404:
                return _not_found()

            response.raise_for_status()

            root = _safe_fromstring(response.text)
        # Neither branch caches: an unparseable body and a 5xx/timeout/429
        # both say nothing about whether the paper exists.
        except _PARSE_ERRORS:
            return _parse_error_dict()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        entries = root.findall(f"{{{_ATOM_NS}}}entry")

        if not entries or _is_error_entry(entries[0]):
            return _not_found()

        data = _parse_entry(entries[0])
        cache.put(NAMESPACE, "papers", canonical, data)
        return data

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="papers",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
    )


async def search_papers(
    query: str,
    max_results: int = 10,
) -> dict[str, Any]:
    """Search arXiv. ``query`` takes field prefixes (ti:, au:, abs:, cat:) and AND/OR/ANDNOT.

    Returns ``{total_results, entries}``. The result list is not cached
    (ad-hoc queries), but each hit warms the paper cache.
    """
    capped = min(max(max_results, 1), MAX_SEARCH_RESULTS)

    try:
        response = await _throttled_get(
            ARXIV_BASE_URL,
            params={
                "search_query": query,
                "start": "0",
                "max_results": str(capped),
            },
        )

        response.raise_for_status()

        # Inside the guard: a garbled or hostile body must return, not raise.
        root = _safe_fromstring(response.text)
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except http.HTTPX_ERRORS as e:
        return http.error_dict(LABEL, e)

    entries = root.findall(f"{{{_ATOM_NS}}}entry")

    # Not retryable: the query is what's wrong, and re-running it cannot help.
    if entries and _is_error_entry(entries[0]):
        summary_el = entries[0].find(f"{{{_ATOM_NS}}}summary")
        detail = " ".join((summary_el.text or "").split()) if summary_el is not None else ""
        return {
            "error": f"arXiv rejected the search query: {detail or query}",
            "retryable": False,
        }

    papers = [_parse_entry(e) for e in entries]

    # ``isdecimal``, not ``isdigit``: ``int()`` rejects the superscripts
    # ``isdigit`` accepts, and ValueError is in neither except clause above.
    total_el = root.find(f"{{{_OPENSEARCH_NS}}}totalResults")
    total_text = total_el.text.strip() if total_el is not None and total_el.text else ""

    # Search always returns the current version, so each hit is valid under
    # *both* the versioned key (that exact revision) and the bare key (whatever
    # is current). Warming only the versioned one leaves every bare lookup a miss.
    for paper in papers:
        paper_id = id_from_entry(paper)
        if not is_arxiv_id(paper_id):
            continue
        for key in {canonical_arxiv_id(paper_id), base_arxiv_id(paper_id)}:
            cache.warm(NAMESPACE, "papers", key, paper, max_age_seconds=_POSITIVE_TTL_SECONDS)

    return {
        "total_results": int(total_text) if total_text.isdecimal() else 0,
        "entries": papers,
    }


def pdf_path(arxiv_id: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet)."""
    canonical = canonical_arxiv_id(arxiv_id)
    return stems.pdf_path(NAMESPACE, canonical)


async def download_pdf(arxiv_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download and cache this paper's PDF; returns its path and size, or an error.

    ``force_refresh=True`` re-downloads and atomically replaces the cached
    file, keeping the old one if the re-download fails. Streaming, the byte cap
    and the atomic rename are ``streaming.stream_to_file``'s.
    """
    canonical = canonical_arxiv_id(arxiv_id)
    dest = stems.pdf_path(NAMESPACE, canonical)

    async def _fetch() -> dict[str, Any]:
        # force_refresh threaded through: resolving the URL from a stale
        # record would re-fetch the very bytes we were asked to replace.
        paper = await get_paper(arxiv_id, force_refresh=force_refresh)
        if "error" in paper:
            return paper

        pdf_url = None
        for link in paper.get("links", []):
            if link.get("title") == "pdf":
                pdf_url = link["href"]
                break

        if not pdf_url:
            # Definitive — this is how a withdrawn paper presents.
            return {
                "error": f"No PDF link found for arXiv ID: {arxiv_id}",
                "retryable": False,
            }

        return await streaming.stream_to_file(
            _get_client(),
            pdf_url,
            dest,
            slot_factory=lambda: _request_slot(pdf_url),
            namespace=NAMESPACE,
            provider_label=LABEL,
            timeout=_PDF_TIMEOUT_SECONDS,
            not_found_message=f"No PDF found for arXiv ID: {arxiv_id}",
        )

    # Tuple-keyed to stay distinct from get_paper's slot, which _fetch awaits.
    return await streaming.cached_download(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity=_NEG_ENTITY,
        canonical=canonical,
        dest=dest,
        fetch=_fetch,
        neg_ttl=_NEG_TTL_SECONDS,
        force_refresh=force_refresh,
        sf_key=("pdf", canonical),
    )
