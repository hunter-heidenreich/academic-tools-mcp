import asyncio
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from pathlib import Path

from . import _http, cache

ARXIV_BASE_URL = "https://export.arxiv.org/api/query"
NAMESPACE = "arxiv"

# XML namespaces
_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"

# Rate limiting: max 1 request per 3 seconds, single connection
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 3.0


async def _throttled_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a GET request respecting arXiv's rate limit.

    Enforces: max 1 concurrent request, minimum 3-second gap between requests.
    """
    global _last_request_time
    async with _request_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time
        if _last_request_time > 0 and elapsed < _MIN_REQUEST_GAP:
            await asyncio.sleep(_MIN_REQUEST_GAP - elapsed)
        response = await client.get(url, **kwargs)
        _last_request_time = time.monotonic()
        return response


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


async def get_paper(arxiv_id: str) -> dict[str, Any]:
    """Fetch a paper by arXiv ID, using cache when available.

    Returns a parsed dict with paper metadata.
    """
    canonical = _canonical_arxiv_id(arxiv_id)

    cached = cache.get(NAMESPACE, "papers", canonical)
    if cached is not None:
        return cached

    api_id = _normalize_arxiv_id(arxiv_id)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
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
        return {"error": f"No paper found for arXiv ID: {arxiv_id}"}

    # arXiv returns HTTP 200 with an error entry for invalid IDs
    entry = entries[0]
    id_el = entry.find(f"{{{_ATOM_NS}}}id")
    if id_el is not None and id_el.text and "api/errors" in id_el.text:
        return {"error": f"No paper found for arXiv ID: {arxiv_id}"}

    data = _parse_entry(entry)
    cache.put(NAMESPACE, "papers", canonical, data)
    return data


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
        async with httpx.AsyncClient(timeout=30.0) as client:
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


async def download_pdf(arxiv_id: str) -> dict[str, Any]:
    """Download the PDF for an arXiv paper and cache it locally.

    Returns a dict with the file path and size, or an error.
    """
    canonical = _canonical_arxiv_id(arxiv_id)
    dest = cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)

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
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await _throttled_get(client, pdf_url)

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
