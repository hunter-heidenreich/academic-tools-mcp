import asyncio
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from pathlib import Path

from . import _clients, _http, _singleflight, _stats, cache

ARXIV_BASE_URL = "https://export.arxiv.org/api/query"
NAMESPACE = "arxiv"

# XML namespaces
_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"

# Rate limiting: max 1 request per 3 seconds, single connection.
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 3.0

# Burst cap: refuse to stack more than this many requests behind the
# throttle. With a 3s gap, 5 pending = 15s of agent-blocking — past that
# we'd rather tell the agent to back off than silently queue forever.
# A 6th concurrent caller gets a structured backpressure error.
_MAX_PENDING = 5
_pending: int = 0

# Coalesces concurrent calls for the same canonical paper ID into one
# fetch. Without this, 4 parallel unified-paper tools (metadata, authors,
# abstract, bibtex) for one arXiv ID would each hit the network.
_single_flight = _singleflight.SingleFlight()

# Shorter than the cache.py default 24h. arXiv IDs go live mid-session
# (a paper just announced an hour ago) and an agent that 404'd at 9am
# should surface the new entry by 10am, not tomorrow at 9am.
_NEG_TTL_SECONDS = 3600.0

# Positive cache TTL. arXiv records are stable per-version, but our
# canonical key strips the version suffix, so v1 cached today wouldn't
# reflect a v2 uploaded next week. 14 days is long enough that an active
# session keeps hitting cache and short enough that revisions surface.
_POSITIVE_TTL_SECONDS = 14 * 86400.0


async def _throttled_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a GET request respecting arXiv's rate limit.

    Enforces: max 1 concurrent request, minimum 3-second gap between
    requests, and a burst cap of ``_MAX_PENDING`` queued callers. The
    burst cap raises ``LocalBackpressureError`` rather than queueing
    indefinitely, so an agent that fans out 10 calls in microseconds
    gets fast feedback on the 6th.
    """
    global _last_request_time, _pending
    if _pending >= _MAX_PENDING:
        _stats.incr(NAMESPACE, "backpressure_refusals")
        raise _http.LocalBackpressureError(
            "arXiv", _pending, _MAX_PENDING, _MIN_REQUEST_GAP
        )
    _pending += 1
    try:
        async with _request_lock:
            now = time.monotonic()
            elapsed = now - _last_request_time
            wait_seconds = 0.0
            if _last_request_time > 0 and elapsed < _MIN_REQUEST_GAP:
                wait_seconds = _MIN_REQUEST_GAP - elapsed
                await asyncio.sleep(wait_seconds)
            _stats.log_request(NAMESPACE, url, wait_seconds)
            _stats.incr(NAMESPACE, "http_calls")
            response = await _http.get_with_retry(
                client, url,
                # arXiv's 3s gap must apply to the retry too — a 1s
                # retry would violate their "1 req per 3s" policy.
                backoff_seconds=max(_MIN_REQUEST_GAP, 1.0),
                provider=NAMESPACE,
                **kwargs,
            )
            _last_request_time = time.monotonic()
            return response
    finally:
        _pending -= 1


# ---------------------------------------------------------------------------
# ID normalization
# ---------------------------------------------------------------------------

_ARXIV_URL_RE = re.compile(
    r"https?://arxiv\.org/(?:abs|pdf)/(.+?)(?:\.pdf)?$"
)


def _normalize_arxiv_id(arxiv_id: str) -> str:
    """Normalize an arXiv identifier to a bare ID (with version if present).

    Accepts:
      - bare ID: 2301.00001, 2301.00001v2, hep-th/9901001
      - abstract URL: https://arxiv.org/abs/2301.00001v2
      - PDF URL: https://arxiv.org/pdf/2301.00001v2.pdf
      - PDF URL without extension: https://arxiv.org/pdf/2301.00001v2
    """
    arxiv_id = arxiv_id.strip()
    m = _ARXIV_URL_RE.match(arxiv_id)
    if m:
        return m.group(1)
    return arxiv_id


def _canonical_arxiv_id(arxiv_id: str) -> str:
    """Return a canonical arXiv ID for cache keying.

    Strips version suffix and lowercases so that v1/v2/latest share one
    cache entry.
    """
    bare = _normalize_arxiv_id(arxiv_id)
    return re.sub(r"v\d+$", "", bare).lower()


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------


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
            aff.text.strip()
            for aff in author_el.findall(f"{{{_ARXIV_NS}}}affiliation")
            if aff.text
        ]
        authors.append({"name": name, "affiliations": affiliations})

    # Links
    links = []
    for link_el in entry.findall(f"{{{_ATOM_NS}}}link"):
        links.append({
            "href": link_el.get("href", ""),
            "rel": link_el.get("rel", ""),
            "title": link_el.get("title") or None,
        })

    # Categories
    categories = [
        cat.get("term", "")
        for cat in entry.findall(f"{{{_ATOM_NS}}}category")
        if cat.get("term")
    ]

    # Primary category
    primary_cat_el = entry.find(f"{{{_ARXIV_NS}}}primary_category")
    primary_category = (
        primary_cat_el.get("term", "") if primary_cat_el is not None else ""
    )

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
    canonical = _canonical_arxiv_id(arxiv_id)

    if force_refresh:
        cache.invalidate(NAMESPACE, "papers", canonical)
    else:
        cached = cache.get(NAMESPACE, "papers", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
        if cached is not None:
            return cached
        neg = cache.get_negative(NAMESPACE, "papers", canonical)
        if neg is not None:
            return neg

    async def _fetch() -> dict[str, Any]:
        # Re-check cache inside the single-flight slot: the leader for
        # this key may have already finished and populated the cache by
        # the time a follower's coroutine resumed past the outer check.
        # On a forced refresh we still re-check so concurrent forced
        # callers share one fetch (the leader writes, the followers see
        # the fresh entry).
        cached = cache.get(NAMESPACE, "papers", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
        if cached is not None:
            return cached
        neg = cache.get_negative(NAMESPACE, "papers", canonical)
        if neg is not None:
            return neg

        api_id = _normalize_arxiv_id(arxiv_id)

        try:
            client = _clients.get_client(NAMESPACE, timeout=30.0)
            response = await _throttled_get(
                client,
                ARXIV_BASE_URL,
                params={"id_list": api_id},
            )

            response.raise_for_status()

            root = ET.fromstring(response.text)
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("arXiv", e)

        entries = root.findall(f"{{{_ATOM_NS}}}entry")

        if not entries:
            err = {"error": f"No paper found for arXiv ID: {arxiv_id}"}
            cache.put_negative(NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
            return err

        # arXiv returns HTTP 200 with an error entry for invalid IDs.
        # Cache it the same way as a real 404 — both mean "definitively
        # not found", which is what negative caching is for.
        entry = entries[0]
        id_el = entry.find(f"{{{_ATOM_NS}}}id")
        if id_el is not None and id_el.text and "api/errors" in id_el.text:
            err = {"error": f"No paper found for arXiv ID: {arxiv_id}"}
            cache.put_negative(NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
            return err

        data = _parse_entry(entry)
        cache.put(NAMESPACE, "papers", canonical, data)
        return data

    return await _single_flight.do(canonical, _fetch)


async def search_papers(
    query: str,
    max_results: int = 10,
) -> dict[str, Any]:
    """Search arXiv papers by query string.

    The query supports field prefixes: ti:, au:, abs:, cat:, etc.
    Boolean operators: AND, OR, ANDNOT.
    Returns a dict with total_results and a list of parsed entries.
    """
    capped = min(max(max_results, 1), 50)

    try:
        client = _clients.get_client(NAMESPACE, timeout=30.0)
        response = await _throttled_get(
            client,
            ARXIV_BASE_URL,
            params={
                "search_query": query,
                "start": "0",
                "max_results": str(capped),
            },
        )

        response.raise_for_status()

        root = ET.fromstring(response.text)
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("arXiv", e)

    total_el = root.find(f"{{{_OPENSEARCH_NS}}}totalResults")
    total_results = int(total_el.text) if total_el is not None and total_el.text else 0

    entries = root.findall(f"{{{_ATOM_NS}}}entry")
    papers = [_parse_entry(e) for e in entries]

    # Opportunistically cache individual papers
    for paper in papers:
        raw_id = paper.get("id", "")
        if "/abs/" in raw_id:
            paper_id = raw_id.split("/abs/")[-1]
            paper_canonical = _canonical_arxiv_id(paper_id)
            if not cache.has(NAMESPACE, "papers", paper_canonical):
                cache.put(NAMESPACE, "papers", paper_canonical, paper)

    return {
        "total_results": total_results,
        "entries": papers,
    }


def _pdf_filename(canonical: str) -> str:
    """Build a human-readable PDF filename from a canonical arXiv ID."""
    return canonical.replace("/", "_") + ".pdf"


def pdf_path(arxiv_id: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet)."""
    canonical = _canonical_arxiv_id(arxiv_id)
    return cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)


async def download_pdf(
    arxiv_id: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Download the PDF for an arXiv paper and cache it locally.

    ``force_refresh=True`` removes the cached PDF and re-downloads. Use
    when you suspect the cached file is corrupt or arXiv replaced the
    PDF (a v2 upload that landed under the same canonical key).

    Returns a dict with the file path and size, or an error.
    """
    canonical = _canonical_arxiv_id(arxiv_id)
    dest = cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)

    if force_refresh and dest.exists():
        dest.unlink()

    if dest.exists():
        return {
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "cached": True,
        }

    # Need the paper metadata to find the PDF URL
    paper = await get_paper(arxiv_id)
    if "error" in paper:
        return paper

    pdf_url = None
    for link in paper.get("links", []):
        if link.get("title") == "pdf":
            pdf_url = link["href"]
            break

    if not pdf_url:
        return {"error": f"No PDF link found for arXiv ID: {arxiv_id}"}

    try:
        # Pool client: the per-call timeout overrides the client default
        # because PDF downloads can be much larger than metadata calls.
        client = _clients.get_client(NAMESPACE, timeout=30.0)
        response = await _throttled_get(client, pdf_url, timeout=60.0)

        response.raise_for_status()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("arXiv", e)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)

    return {
        "path": str(dest),
        "size_bytes": len(response.content),
        "cached": False,
    }
