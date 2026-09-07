"""arXiv client: Atom search and metadata, keyed by canonical (versionless) ID."""

import re
import xml.etree.ElementTree as ET
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from .. import (
    _clients,
    _doi,
    _http,
    _pdf_download,
    _singleflight,
    _stems,
    _useragent,
    cache,
    config,
)
from .._throttle import Throttle

# Both are transient, not "not found" — .claude/rules/providers.md § arxiv.py.
_PARSE_ERRORS = (ET.ParseError, DefusedXmlException)


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable arXiv response — it speaks XML, not JSON."""
    return _http.parse_error_dict("arXiv", detail="could not be parsed as XML")


ARXIV_BASE_URL = "https://export.arxiv.org/api/query"
NAMESPACE = "arxiv"

# XML namespaces
_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"

# concurrency=1 is arXiv's documented "single connection" rule; 5 pending at a
# 3s gap is 15s of agent-blocking, past which callers get backpressure.
_MAX_CONCURRENT = 1
_MIN_REQUEST_GAP = 3.0
_MAX_PENDING = 5

_single_flight = _singleflight.SingleFlight()

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
    """Build request headers with a descriptive User-Agent.

    Unlike the polite-pool opt-in providers (crossref/openalex, which only
    set a User-Agent when a mailto is configured), arXiv is sent a
    descriptive User-Agent *unconditionally*: its Fastly edge throttles
    generic library User-Agents (e.g. ``python-httpx/x.y``) far more
    aggressively, returning 429/503 on modest bursts. ``ARXIV_MAILTO``, when
    set, is appended as a contact so arXiv can reach the operator before
    tightening limits.
    """
    return _useragent.headers(config.get("ARXIV_MAILTO"))


def _get_client() -> httpx.AsyncClient:
    """Return the pooled AsyncClient for arXiv calls.

    Configured here only: ``_clients.get_client`` ignores kwargs on every later
    call for this namespace, so ``_build_headers`` (which carries
    ``ARXIV_MAILTO``) runs here or nowhere — arXiv throttles anonymous library
    traffic harder. The 30s default paces metadata and search; ``download_pdf``
    overrides it per call with ``_PDF_TIMEOUT_SECONDS``.
    """
    return _clients.get_client(NAMESPACE, headers=_build_headers(), timeout=30.0)


_throttle = Throttle(
    namespace=NAMESPACE,
    label="arXiv",
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
    # arXiv's Fastly edge returns 429/503 with no (or zero) Retry-After when an
    # IP is briefly penalty-boxed; a single retry often lands in the same
    # window. Two retries (three attempts total) let get_with_retry's
    # exponential backoff (≈3s then ≈6s) ride out a short cooldown instead of
    # surfacing the error to the agent.
    retry_attempts=3,
)


def _request_slot(url: str) -> AbstractAsyncContextManager[None]:
    """arXiv's rate-limit slot (see ``Throttle.slot``).

    Kept as a module-level wrapper so the streaming PDF download's
    ``slot_factory`` lambda and the test seam
    (``monkeypatch.setattr(arxiv, "_request_slot", ...)``) keep resolving
    a module attribute.
    """
    return _throttle.slot(url)


async def _throttled_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Execute a GET respecting arXiv's rate limit (see ``Throttle.get``)."""
    return await _throttle.get(client, url, **kwargs)


# ---------------------------------------------------------------------------
# ID normalization
# ---------------------------------------------------------------------------

# Scheme and host label optional, matched case-insensitively, for the reason
# ``_doi._DOI_URL_RE`` is: pasted citations carry ``www.``/``export.`` hosts, a
# bare ``arxiv.org/abs/...`` and upstream's varying case, and every spelling
# this misses is one ``manual`` files the same paper under a second time. The
# ID stops at the first ``?`` or ``#`` so a query string / fragment doesn't end
# up baked into the canonical cache key.
_ARXIV_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|export\.)?arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:\.pdf)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# arXiv's own DataCite DOI. Exported for the reason ``acl.ACL_DOI_PREFIX`` is:
# ``bibtex`` needs the prefix for its ``eprint`` field, and a second spelling
# would let the router and the BibTeX writer disagree about which papers are
# arXiv's.
ARXIV_DOI_PREFIX = "10.48550/arXiv."
_ARXIV_DOI_RE = re.compile(rf"^{re.escape(ARXIV_DOI_PREFIX)}(.+)$", re.IGNORECASE)

# The two ID forms. The old-style pair is exported because ``cache_search``
# matches it over ``safe_stem``'s ``_`` rather than ``/``; a second spelling
# there would let it invert a stem that never routes here.
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
      - an ``abs``/``pdf`` URL, either scheme (or none), optional ``www.`` /
        ``export.`` host label, optional ``.pdf`` extension and trailing slash
      - arXiv's DataCite DOI, ``10.48550/arXiv.2301.00001``, in any spelling
        ``_doi.normalize`` accepts

    Case is preserved; ``canonical_arxiv_id`` owns the fold, exactly as
    ``_doi.canonical`` does for DOIs. A string that is not recognisably an
    arXiv ID is returned stripped but otherwise untouched — the caller decides
    whether that is an error.

    Idempotent for every input, which is what the prefix loop buys: a single
    pass leaves ``arXiv:arXiv:2301.00001`` keying separately from its own
    output. The prefix is stripped **before** the URL and DOI handling for the
    same reason ``_doi.normalize`` strips ``doi:`` first — ``arXiv:https://...``
    occurs in pasted citations.
    """
    arxiv_id = arxiv_id.strip()

    while arxiv_id[:6].lower() == "arxiv:":
        arxiv_id = arxiv_id[6:].strip()

    if m := _ARXIV_URL_RE.match(arxiv_id):
        return m.group(1)

    # Through the shared normalizer, so ``https://doi.org/10.48550/...`` and
    # ``doi:10.48550/...`` collapse onto the bare form's key. The tail must
    # itself be arXiv-shaped: that keeps the rewrite from mangling an unrelated
    # DataCite record, and keeps this pass idempotent for a nested spelling.
    if (m := _ARXIV_DOI_RE.match(_doi.normalize(arxiv_id))) and _is_arxiv_shape(m.group(1)):
        return m.group(1)

    return arxiv_id


def canonical_arxiv_id(arxiv_id: str) -> str:
    """Return a canonical arXiv ID for cache keying, **version included**.

    Only a lowercase fold is layered on ``normalize_arxiv_id`` (old-style IDs
    like ``math.GT/0309136`` vary in case upstream); the API request keeps the
    caller's case.

    **Do not strip the version here.** The *fetch* keeps it, so a stripped key
    silently serves the wrong paper: whichever version is requested first wins
    the shared entry, and ``force_refresh`` cannot rescue it — it invalidates
    that same shared key.
    """
    return normalize_arxiv_id(arxiv_id).lower()


def is_arxiv_id(identifier: str) -> bool:
    """Whether *identifier* is an arXiv ID in any of its spellings.

    The shape test ``manual``'s two dispatchers route on, beside
    ``biorxiv.is_biorxiv_doi`` and ``acl.is_acl_doi``. Tests the canonical
    form, so every prefix, URL and DOI spelling is resolved before the shape is
    looked at. An id this rejects still keys exactly as arXiv would, so it
    lands in ``manual`` and the same paper caches, downloads and converts twice.
    """
    return _is_arxiv_shape(canonical_arxiv_id(identifier))


def strip_version(arxiv_id: str) -> str:
    """Drop a trailing ``v<n>`` from an already-bare ID, preserving case.

    Case-preserving because BibTeX's ``eprint`` field must keep an old-style
    id's archive class (``math.GT/0309136``); ``base_arxiv_id`` is the folded
    cache-key form of the same rule.
    """
    return _VERSION_SUFFIX_RE.sub("", arxiv_id)


def base_arxiv_id(arxiv_id: str) -> str:
    """Return the version-stripped ("latest") cache key for an arXiv ID.

    This is the key a bare request uses. Kept separate from
    ``canonical_arxiv_id`` so callers must say which they mean.
    """
    return strip_version(canonical_arxiv_id(arxiv_id))


def id_from_entry(paper: dict[str, Any]) -> str:
    """The bare, versioned arXiv ID from a parsed Atom entry's ``id`` URL.

    One home for it: the tool layer echoes this as ``arxiv_id`` and
    ``search_papers`` warms the cache on it. A local ``split("/abs/")`` is what
    this replaces, and it would not survive an ``export.arxiv.org`` id URL.
    """
    return normalize_arxiv_id(paper.get("id") or "")


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------


def _is_error_entry(entry: ET.Element) -> bool:
    """Whether an Atom entry is arXiv's HTTP-200 stand-in for an error.

    arXiv answers an invalid id *and* a malformed ``search_query`` with 200 and
    a single entry whose ``<id>`` points at ``api/errors``. Shared so
    ``search_papers`` classifies it the same way ``get_paper`` does.
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

    # Authors with optional affiliations
    authors = []
    for author_el in entry.findall(f"{{{_ATOM_NS}}}author"):
        name_el = author_el.find(f"{{{_ATOM_NS}}}name")
        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        affiliations = [
            aff.text.strip() for aff in author_el.findall(f"{{{_ARXIV_NS}}}affiliation") if aff.text
        ]
        authors.append({"name": name, "affiliations": affiliations})

    # Links
    links = []
    for link_el in entry.findall(f"{{{_ATOM_NS}}}link"):
        links.append(
            {
                "href": link_el.get("href", ""),
                "rel": link_el.get("rel", ""),
                "title": link_el.get("title") or None,
            }
        )

    # Categories
    categories = [
        cat.get("term", "") for cat in entry.findall(f"{{{_ATOM_NS}}}category") if cat.get("term")
    ]

    # Primary category
    primary_cat_el = entry.find(f"{{{_ARXIV_NS}}}primary_category")
    primary_category = primary_cat_el.get("term", "") if primary_cat_el is not None else ""

    # ID (URL form: http://arxiv.org/abs/2301.00001v1)
    raw_id = _text("id") or ""

    # Title and summary: collapse embedded whitespace/newlines
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
    """Fetch a paper by arXiv ID, using cache when available.

    Returns a parsed dict with paper metadata. Concurrent callers for
    the same ID share one fetch via single-flight — without this, four
    unified-paper tools (metadata, authors, abstract, bibtex) called in
    parallel would all hit arXiv and burn ~12s of throttle gap between
    them for a paper that ends up in cache after the first call.

    ``force_refresh=True`` drops both positive and negative cache entries
    for this canonical ID before fetching, so an agent can re-pull a
    paper whose cached entry might be stale (e.g. a new version uploaded).
    """
    canonical = canonical_arxiv_id(arxiv_id)

    async def _fetch() -> dict[str, Any]:
        def _not_found() -> dict[str, Any]:
            """Definitive absence — arXiv spells it three ways, all cached here."""
            err = {"error": f"No paper found for arXiv ID: {arxiv_id}", "not_found": True}
            cache.put_negative(NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
            return err

        try:
            response = await _throttled_get(
                _get_client(),
                ARXIV_BASE_URL,
                params={"id_list": normalize_arxiv_id(arxiv_id)},
            )

            # Before raise_for_status, as every sibling does: routing a 404
            # through error_dict would negative-cache a raw body snippet where
            # the other two not-found shapes cache a clean message.
            if response.status_code == 404:
                return _not_found()

            response.raise_for_status()

            root = _safe_fromstring(response.text)
        except _PARSE_ERRORS:
            # A 200 body we couldn't parse: a truncated/garbled response, or a
            # hostile entity-expansion payload defusedxml refused. Transient
            # (or adversarial), not a definitive "not found" — surface a
            # retryable error and do NOT negative-cache it, so a retry
            # re-fetches rather than serving a poisoned entry.
            return _parse_error_dict()
        except _http.HTTPX_ERRORS as e:
            # Transient failures (5xx / timeout / 429 / backpressure) must NOT
            # be cached.
            return _http.error_dict("arXiv", e)

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
    """Search arXiv papers by query string.

    The query supports field prefixes: ti:, au:, abs:, cat:, etc.
    Boolean operators: AND, OR, ANDNOT.
    Returns a dict with total_results and a list of parsed entries.

    The result list is not cached (ad-hoc queries); individual hits warm the
    paper cache, so a follow-up ``get_paper`` on a hit is free.
    """
    capped = min(max(max_results, 1), MAX_SEARCH_RESULTS)

    try:
        response = await _throttled_get(
            _get_client(),
            ARXIV_BASE_URL,
            params={
                "search_query": query,
                "start": "0",
                "max_results": str(capped),
            },
        )

        response.raise_for_status()

        # Parse inside the guarded block: a truncated/garbled 200 body
        # (ParseError) or a hostile entity payload (defusedxml) must surface a
        # structured error, not raise.
        root = _safe_fromstring(response.text)
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("arXiv", e)

    entries = root.findall(f"{{{_ATOM_NS}}}entry")

    # arXiv rejects a malformed search_query with HTTP 200 and one error entry.
    # Classified here as get_paper classifies it, or the agent is handed a hit
    # whose id is an errors URL. Not retryable: the query is what's wrong.
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
    return _stems.pdf_path(NAMESPACE, canonical)


async def download_pdf(arxiv_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download the PDF for an arXiv paper and cache it locally.

    ``force_refresh=True`` re-downloads and atomically replaces the
    cached PDF. Use when you suspect the cached file is corrupt or arXiv
    replaced the PDF (a v2 upload that landed under the same canonical
    key). The existing cached file is kept if the re-download fails, so a
    flaky network can't leave you worse off than before.

    Streams the response to a temp file in chunks (peak memory = one
    chunk, not the whole PDF) and renames into place atomically. The
    download aborts mid-stream if it would exceed ``MAX_PDF_BYTES`` so a
    misrouted URL can't fill the disk.

    Returns a dict with the file path and size, or an error. Concurrent
    callers for the same ID share one download via single-flight.
    """
    canonical = canonical_arxiv_id(arxiv_id)
    dest = _stems.pdf_path(NAMESPACE, canonical)

    async def _fetch() -> dict[str, Any]:
        # Need the paper metadata to find the PDF URL. force_refresh is
        # threaded through: a forced re-download that resolved its URL from a
        # stale cached record would fetch the bytes it was asked to replace.
        paper = await get_paper(arxiv_id, force_refresh=force_refresh)
        if "error" in paper:
            return paper

        pdf_url = None
        for link in paper.get("links", []):
            if link.get("title") == "pdf":
                pdf_url = link["href"]
                break

        if not pdf_url:
            # Definitive: the Atom entry for this paper has no PDF link, which
            # is how a withdrawn paper presents.
            return {
                "error": f"No PDF link found for arXiv ID: {arxiv_id}",
                "retryable": False,
            }

        return await _pdf_download.stream_to_file(
            _get_client(),
            pdf_url,
            dest,
            slot_factory=lambda: _request_slot(pdf_url),
            namespace=NAMESPACE,
            provider_label="arXiv",
            timeout=_PDF_TIMEOUT_SECONDS,
            not_found_message=f"No PDF found for arXiv ID: {arxiv_id}",
        )

    # Tuple-keyed so this slot is distinct from get_paper's (keyed on the
    # bare canonical id): _fetch calls get_paper, which would otherwise
    # await this very slot's future and deadlock.
    return await _pdf_download.cached_download(
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
