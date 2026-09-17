"""arXiv client: Atom metadata, search and PDF download, keyed by a version-preserving ID."""

import re
import xml.etree.ElementTree as ET
from contextlib import AbstractAsyncContextManager
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from ..download import streaming
from ..net import clients, http, stats
from ..net.throttle import Throttle
from ..store import cache, singleflight, stems
from ..util import config, doinorm, useragent

# Both are transient, not "not found".
_PARSE_ERRORS = (ET.ParseError, DefusedXmlException)


def _parse_error_dict() -> dict[str, Any]:
    """Fresh transient error for an arXiv body that isn't parseable XML."""
    return http.parse_error_dict(LABEL, detail="could not be parsed as XML")


ARXIV_BASE_URL = "https://export.arxiv.org/api/query"

# License, submitter and revision history: the Atom API carries none of them.
_OAI_BASE_URL = "https://oaipmh.arxiv.org/oai"
NAMESPACE = "arxiv"

# The PDF host's path for a bare id, versioned or not: what an entry's pdf link names.
_PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{}"

# arXiv's LaTeXML rendering, versioned or not; not every paper has one.
_HTML_URL_TEMPLATE = "https://arxiv.org/html/{}"

# LaTeXML's root class: what separates a rendering from any other 200 page.
_HTML_MARKER = "ltx_document"

# Agent-facing provider name; every site that names us reads it.
LABEL = "arXiv"

_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"
_OAI_NS = "http://www.openarchives.org/OAI/2.0/"
_ARXIV_RAW_NS = "http://arxiv.org/OAI/arXivRaw/"

# concurrency=1 is arXiv's documented "single connection" rule; _MAX_PENDING x
# _MIN_REQUEST_GAP bounds how long a queued caller blocks before backpressure.
_MAX_CONCURRENT = 1
_MIN_REQUEST_GAP = 3.0
_MAX_PENDING = 5

_single_flight = singleflight.SingleFlight()

# Short: an arXiv id goes live mid-session, so a 404 at 9am should clear by 10am.
_NEG_TTL_SECONDS = 3600.0

# Definitive PDF-download failures negative-cache here, on that same short TTL.
_NEG_ENTITY = "downloads"

# A PDF is megabytes where a metadata call is kilobytes.
_PDF_TIMEOUT_SECONDS = 60.0

# Exported so ``search_arxiv``'s validation bound isn't a second spelling of it.
MAX_SEARCH_RESULTS = 50

# arXiv answers HTTP 500 once ``start + max_results`` passes this: a query's reachable
# window, not its match count. Exported for the same reason as ``MAX_SEARCH_RESULTS``.
MAX_SEARCH_WINDOW = 10_000

# Agent-facing sort names; ``_SORT_BY`` maps them to the API's ``sortBy`` values.
SearchSortBy = Literal["relevance", "submitted", "updated"]
SearchSortOrder = Literal["descending", "ascending"]
_SORT_BY: dict[str, str] = {
    "relevance": "relevance",
    "submitted": "submittedDate",
    "updated": "lastUpdatedDate",
}

# ``id_list`` fan-in size: one GET per chunk, with a URL well inside edge limits.
_BATCH_CHUNK_SIZE = 50

# Long: a record is stable per version — but bounded, so a revision still surfaces
# under a bare key.
_POSITIVE_TTL_SECONDS = 14 * 86400.0

# Short: the record changes exactly when a revision lands, which is what it reports.
_VERSIONS_TTL_SECONDS = 86400.0


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
    # Above the shared default: an arXiv penalty-box outlasts one retry's backoff.
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


def _rejection_root(response: httpx.Response) -> ET.Element | None:
    """The parsed feed of a 400 that carries arXiv's ``api/errors`` entry, else ``None``.

    ``None`` sends the caller to ``raise_for_status``: a 400 with any other body
    stays an unclassified ``error_dict``, not a rejection or a parse error.
    """
    return _error_entry_root(response) if response.status_code == 400 else None


def _error_entry_root(response: httpx.Response) -> ET.Element | None:
    """The parsed feed if the body carries arXiv's ``api/errors`` entry, else ``None``."""
    try:
        root = _safe_fromstring(response.text)
    except _PARSE_ERRORS:
        return None
    entry = root.find(f"{{{_ATOM_NS}}}entry")
    return root if entry is not None and _is_error_entry(entry) else None


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

            # Before raise_for_status, as every sibling does: through error_dict a 404
            # is an uncached body snippet carrying no ``not_found``.
            if response.status_code == 404:
                return _not_found()

            # A rejected id is a 400 whose entry the not-found branch below classifies.
            root = _rejection_root(response)
            if root is None:
                response.raise_for_status()
                root = _safe_fromstring(response.text)
        # Neither caches: a garbled body and a 5xx/timeout/429 say nothing about existence.
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
    *,
    start: int = 0,
    sort_by: SearchSortBy = "relevance",
    sort_order: SearchSortOrder = "descending",
) -> dict[str, Any]:
    """Search arXiv. ``query`` takes field prefixes (ti:, au:, abs:, cat:) and AND/OR/ANDNOT.

    ``max_results`` is clamped to ``MAX_SEARCH_RESULTS`` and ``start`` to 0. Returns
    ``{total_results, entries}``; the result list is not cached (ad-hoc queries), but
    each hit warms the paper cache. A page past ``MAX_SEARCH_WINDOW`` is refused
    without a request, as ``{error, retryable: False, beyond_window: True}``.
    """
    capped = min(max(max_results, 1), MAX_SEARCH_RESULTS)
    start = max(start, 0)

    # arXiv's answer here is a 500, which would read as transient and be retried.
    if start + capped > MAX_SEARCH_WINDOW:
        return {
            "error": (
                f"arXiv serves only the first {MAX_SEARCH_WINDOW} results of a query; "
                f"this page starts at result {start + 1}."
            ),
            "retryable": False,
            "beyond_window": True,
        }

    try:
        response = await _throttled_get(
            ARXIV_BASE_URL,
            params={
                "search_query": query,
                "start": str(start),
                "max_results": str(capped),
                "sortBy": _SORT_BY[sort_by],
                "sortOrder": sort_order,
            },
        )

        # A rejected query is a 400 whose entry the rejection branch below classifies.
        root = _rejection_root(response)
        if root is None:
            response.raise_for_status()
            # Inside the guard: a garbled or hostile body must return, not raise.
            root = _safe_fromstring(response.text)
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except http.HTTPX_ERRORS as e:
        return http.error_dict(LABEL, e)

    entries = root.findall(f"{{{_ATOM_NS}}}entry")

    # Not retryable: the query is what's wrong.
    if entries and _is_error_entry(entries[0]):
        summary_el = entries[0].find(f"{{{_ATOM_NS}}}summary")
        detail = " ".join((summary_el.text or "").split()) if summary_el is not None else ""
        return {
            "error": f"arXiv rejected the search query: {detail or query}",
            "retryable": False,
        }

    papers = [_parse_entry(e) for e in entries]

    # Search returns the current version, so each hit is valid under both the versioned
    # key and the bare one; warming only the versioned key leaves every bare lookup a miss.
    for paper in papers:
        paper_id = id_from_entry(paper)
        if not is_arxiv_id(paper_id):
            continue
        for key in {canonical_arxiv_id(paper_id), base_arxiv_id(paper_id)}:
            cache.warm(NAMESPACE, "papers", key, paper, max_age_seconds=_POSITIVE_TTL_SECONDS)

    return {
        "total_results": _total_results(root) or 0,
        "entries": papers,
    }


async def _fetch_batch_chunk(
    chunk: list[str],
    *,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch one ``id_list=a,b,c`` chunk, coalesced by single-flight.

    Returns ``{canonical: paper_or_error}`` for every id in ``chunk``; the
    single-flight key sorts it, so argument order can't defeat coalescing.
    """
    # Tuple-keyed to stay distinct from get_paper's slot, which a rejected chunk awaits.
    sf_key = ("papers_batch", tuple(sorted(chunk)), force_refresh)

    async def _runner() -> dict[str, dict[str, Any]]:
        # Bypasses ``cache.cached_lookup``, so it books its own misses — one per id.
        for _ in chunk:
            stats.incr(NAMESPACE, "cache_misses")
        return await _fetch_batch_chunk_uncoalesced(chunk, force_refresh=force_refresh)

    return await _single_flight.do(sf_key, _runner)


async def _fetch_singletons(chunk: list[str], *, force_refresh: bool) -> dict[str, dict[str, Any]]:
    """A failed chunk's ids one at a time, so one bad id fails alone.

    Sequential: a fan-out past the throttle's burst cap is refused.
    """
    return {c: await get_paper(c, force_refresh=force_refresh) for c in chunk}


async def _fetch_batch_chunk_uncoalesced(
    chunk: list[str], *, force_refresh: bool
) -> dict[str, dict[str, Any]]:
    """The actual ``id_list`` GET + result mapping for one chunk."""
    try:
        response = await _throttled_get(
            ARXIV_BASE_URL,
            # The default page is 10; a short page would read as omitted ids.
            params={"id_list": ",".join(chunk), "max_results": str(len(chunk))},
        )
        root = _rejection_root(response)
        if root is None:
            # Some records always 500 with the error entry; a plain 5xx stays chunk-wide.
            if (
                len(chunk) > 1
                and response.status_code >= 500
                and _error_entry_root(response) is not None
            ):
                return await _fetch_singletons(chunk, force_refresh=force_refresh)
            response.raise_for_status()
            root = _safe_fromstring(response.text)
    # One fresh dict per key: ``dict.fromkeys`` would alias one error across the chunk.
    except _PARSE_ERRORS:
        return {c: _parse_error_dict() for c in chunk}
    except http.HTTPX_ERRORS as e:
        return {c: http.error_dict(LABEL, e) for c in chunk}

    entries = root.findall(f"{{{_ATOM_NS}}}entry")

    # One id arXiv won't accept rejects the whole request; singletons isolate it.
    if entries and _is_error_entry(entries[0]):
        return await _fetch_singletons(chunk, force_refresh=force_refresh)

    chunk_set = set(chunk)
    out: dict[str, dict[str, Any]] = {}
    # A bare request is "whatever is current": the newest version returned, which is
    # not the only one when the same chunk also asks for an older revision.
    newest: dict[str, tuple[int, dict[str, Any]]] = {}
    unattributed = 0
    for entry in entries:
        paper = _parse_entry(entry)
        returned = canonical_arxiv_id(id_from_entry(paper))
        bare = strip_version(returned)
        if returned not in chunk_set and bare not in chunk_set:
            unattributed += 1
            continue
        if returned in chunk_set:
            out[returned] = paper
        if bare in chunk_set:
            version = _version_number(returned)
            if bare not in newest or version > newest[bare][0]:
                newest[bare] = (version, paper)
    for bare, (_, paper) in newest.items():
        out[bare] = paper
    for canonical, paper in out.items():
        cache.put(NAMESPACE, "papers", canonical, paper)

    # arXiv omits a missing id silently. That is a definitive miss only if the feed
    # accounted for itself; otherwise negative-caching could poison a live id.
    trustworthy = unattributed == 0 and _total_results(root) == len(entries)
    for canonical in chunk:
        if canonical in out:
            continue
        err = http.not_found(f"No paper found for arXiv ID: {canonical}")
        if trustworthy:
            cache.put_negative(NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
        else:
            # Inconclusive rather than absent — let the caller retry.
            err = {"error": err["error"], "retryable": True}
        out[canonical] = err

    return out


async def get_papers_batch(
    arxiv_ids: list[str],
    *,
    force_refresh: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fetch many arXiv papers in batched ``id_list`` calls.

    Returns ``{canonical_id: paper_or_error_dict}`` for every input id, keyed in
    first-appearance order. Cached entries (positive or negative) are served
    without a network call; the *misses* are grouped into ``id_list`` calls of up
    to ``_BATCH_CHUNK_SIZE``, and each resolved paper is written under the key
    ``get_paper`` reads, so a later singleton is a free hit. ``force_refresh=True``
    drops cached entries first.

    As ``openalex.get_works_batch``: a transient failure contaminates its whole
    chunk, and entries are not deep-copied.
    """
    canonicals_in_order = list(dict.fromkeys(canonical_arxiv_id(i) for i in arxiv_ids))

    out: dict[str, dict[str, Any]] = {}
    misses: list[str] = []

    for canonical in canonicals_in_order:
        if force_refresh:
            cache.invalidate(NAMESPACE, "papers", canonical)
            misses.append(canonical)
            continue
        cached = cache.get(NAMESPACE, "papers", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
        if cached is not None:
            out[canonical] = cached
            continue
        neg = cache.get_negative(NAMESPACE, "papers", canonical)
        if neg is not None:
            out[canonical] = neg
            continue
        misses.append(canonical)

    # Sequential, unlike openalex's gather: arXiv serves one connection at a time, so
    # concurrency buys nothing and a gather past the burst cap is refused.
    for start in range(0, len(misses), _BATCH_CHUNK_SIZE):
        chunk = misses[start : start + _BATCH_CHUNK_SIZE]
        out.update(await _fetch_batch_chunk(chunk, force_refresh=force_refresh))

    return {c: out[c] for c in canonicals_in_order}


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


async def get_versions(arxiv_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """License, submitter and revision history from arXiv's OAI-PMH ``arXivRaw`` record.

    Keyed by the bare id: every revision shares one record. ``license`` is a URL, or
    ``None`` for papers that predate arXiv recording one; each version's ``date`` is
    ISO 8601 UTC. Through the same throttle as the Atom API: arXiv's single-connection
    rule covers both.
    """
    canonical = base_arxiv_id(arxiv_id)

    async def _fetch() -> dict[str, Any]:
        # Shape-gated: only a grammar-valid id is interpolated into the OAI identifier.
        if not _is_arxiv_shape(canonical):
            return http.not_found(f"Not an arXiv ID: {arxiv_id}")

        try:
            response = await _throttled_get(
                _OAI_BASE_URL,
                params={
                    "verb": "GetRecord",
                    "identifier": f"oai:arXiv.org:{canonical}",
                    "metadataPrefix": "arXivRaw",
                },
            )
            response.raise_for_status()
            root = _safe_fromstring(response.text)
        except _PARSE_ERRORS:
            return _parse_error_dict()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        # OAI-PMH reports errors in a 200 body.
        error_el = root.find(f"{{{_OAI_NS}}}error")
        if error_el is not None:
            if error_el.get("code") == "idDoesNotExist":
                err = http.not_found(f"No paper found for arXiv ID: {arxiv_id}")
                cache.put_negative(
                    NAMESPACE, "versions", canonical, err, ttl_seconds=_NEG_TTL_SECONDS
                )
                return err
            detail = " ".join((error_el.text or "").split())
            return {
                "error": f"{LABEL} OAI-PMH rejected the request ({error_el.get('code')}): {detail}",
                "retryable": False,
            }

        record = _parse_versions(root)
        if record is None:
            return _parse_error_dict()
        cache.put(NAMESPACE, "versions", canonical, record)
        return record

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="versions",
        canonical=canonical,
        positive_ttl=_VERSIONS_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=("versions", canonical),
    )


async def get_html(arxiv_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """arXiv's LaTeXML HTML rendering of a paper, as ``{"markup": text}``.

    arXiv answers 404 when a paper has none — non-LaTeX source or a failed
    conversion — which is negative-cached on the short arXiv TTL, since a rendering
    can appear after announcement. There is no positive cache: the markdown made
    from it is the cache.
    """
    canonical = canonical_arxiv_id(arxiv_id)

    if force_refresh:
        cache.invalidate(NAMESPACE, "html", canonical)
    elif (neg := cache.get_negative(NAMESPACE, "html", canonical)) is not None:
        return neg

    async def _fetch() -> dict[str, Any]:
        bare = normalize_arxiv_id(arxiv_id)
        # Shape-gated: only a grammar-valid id is interpolated into the HTML host's path.
        if not _is_arxiv_shape(bare):
            return http.not_found(f"Not an arXiv ID: {arxiv_id}")

        try:
            response = await _throttled_get(
                _HTML_URL_TEMPLATE.format(bare), timeout=_PDF_TIMEOUT_SECONDS
            )
            if response.status_code == 404:
                err = http.not_found(f"No HTML rendering for arXiv ID: {arxiv_id}")
                cache.put_negative(NAMESPACE, "html", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
                return err
            response.raise_for_status()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        max_bytes = streaming.resolve_max_pdf_bytes()
        if max_bytes is not None and len(response.content) > max_bytes:
            return {
                "error": f"{LABEL} HTML exceeds MAX_PDF_BYTES ({max_bytes} bytes).",
                "retryable": False,
                "max_bytes": max_bytes,
            }

        text = response.text
        # A 200 of the wrong shape is transient, as a garbled body is.
        if _HTML_MARKER not in text:
            return http.parse_error_dict(LABEL, detail="was not a LaTeXML rendering")
        return {"markup": text}

    # Tuple-keyed to stay distinct from get_paper's and download_pdf's slots.
    return await _single_flight.do(("html", canonical), _fetch)


def pdf_path(arxiv_id: str) -> Path:
    """The PDF's cache path, whether or not it exists yet."""
    canonical = canonical_arxiv_id(arxiv_id)
    return stems.pdf_path(NAMESPACE, canonical)


async def download_pdf(arxiv_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download and cache this paper's PDF; returns its path and size, or an error.

    ``force_refresh=True`` re-downloads and atomically replaces the cached
    file, keeping the old one if the re-download fails. Streaming, the byte cap
    and the atomic rename are ``streaming.stream_to_file``'s.

    The URL comes from the metadata record's pdf link. When that lookup fails
    *transiently*, the PDF is streamed from ``arxiv.org/pdf/<id>`` instead: the
    export API throttles independently of the PDF host, so a 429 there says
    nothing about whether the PDF is being served. A definitive miss (or an
    unclassified error) is returned as-is, with no request to the PDF host.
    """
    canonical = canonical_arxiv_id(arxiv_id)
    dest = stems.pdf_path(NAMESPACE, canonical)

    async def _fetch() -> dict[str, Any]:
        # Threaded through: a stale record's link re-fetches the bytes we were told to replace.
        paper = await get_paper(arxiv_id, force_refresh=force_refresh)
        pdf_url: str | None = None
        if "error" in paper:
            bare = normalize_arxiv_id(arxiv_id)
            # Shape-gated: only a grammar-valid id is interpolated into the PDF host's path.
            if paper.get("retryable") is not True or not _is_arxiv_shape(bare):
                return paper
            pdf_url = _PDF_URL_TEMPLATE.format(bare)
        else:
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
