import asyncio
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import _http, cache

NAMESPACE = "biorxiv"
_BASE_URL = "https://api.biorxiv.org"

# All bioRxiv/medRxiv DOIs use this prefix
_DOI_PREFIX = "10.1101/"

# Rate limiting: no documented limit, but be polite (~2 req/sec)
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 0.5


async def _throttled_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a GET request with polite rate limiting."""
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
# DOI normalization
# ---------------------------------------------------------------------------

_DOI_URL_RE = re.compile(
    r"https?://(?:dx\.)?doi\.org/(10\.\d{4,}/\S+)$"
)

_BIORXIV_URL_RE = re.compile(
    r"https?://(?:www\.)?(bio|med)rxiv\.org/content/(10\.\d{4,}/\S+?)(?:v\d+)?(?:\.full(?:\.pdf)?)?$"
)


def _normalize_doi(doi: str) -> str:
    """Normalize a bioRxiv/medRxiv DOI to bare form.

    Accepts:
      - bare DOI: 10.1101/2024.01.01.573838
      - doi: prefix: doi:10.1101/2024.01.01.573838
      - URL: https://doi.org/10.1101/2024.01.01.573838
      - bioRxiv URL: https://www.biorxiv.org/content/10.1101/2024.01.01.573838v1
      - medRxiv URL: https://www.medrxiv.org/content/10.1101/2020.01.01.12345v2.full.pdf
    """
    doi = doi.strip()
    if doi.lower().startswith("doi:"):
        doi = doi[4:]

    m = _DOI_URL_RE.match(doi)
    if m:
        return m.group(1)

    m = _BIORXIV_URL_RE.match(doi)
    if m:
        return m.group(2)

    return doi


def _canonical_key(doi: str) -> str:
    """Return a canonical cache key from a bioRxiv/medRxiv DOI."""
    return _normalize_doi(doi).lower()


def is_biorxiv_doi(doi: str) -> bool:
    """Check if a DOI belongs to bioRxiv or medRxiv."""
    return _normalize_doi(doi).startswith(_DOI_PREFIX)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_authors(author_str: str) -> list[dict[str, str]]:
    """Parse a semicolon-separated author string into structured dicts.

    bioRxiv format: "Last, First; Last, First; ..."
    """
    authors = []
    if not author_str:
        return authors

    for part in author_str.split(";"):
        part = part.strip()
        if not part:
            continue
        # "Last, First M." -> {"name": "First M. Last"}
        if "," in part:
            pieces = part.split(",", 1)
            last = pieces[0].strip()
            first = pieces[1].strip() if len(pieces) > 1 else ""
            name = f"{first} {last}".strip() if first else last
        else:
            name = part
        authors.append({"name": name})
    return authors


def _pick_latest_version(collection: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the latest version from a bioRxiv API collection array."""
    if len(collection) == 1:
        return collection[0]
    # Sort by version number (string -> int) and take the highest
    return max(collection, key=lambda e: int(e.get("version", "0")))


def _parse_paper(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw bioRxiv API entry into a normalized paper dict."""
    server = raw.get("server", "").lower()
    if "medrxiv" in server:
        server = "medrxiv"
    else:
        server = "biorxiv"

    version = raw.get("version", "1")
    doi = raw.get("doi", "")

    # Build PDF URL from DOI and version
    domain = "medrxiv.org" if server == "medrxiv" else "biorxiv.org"
    pdf_url = f"https://www.{domain}/content/{doi}v{version}.full.pdf"

    return {
        "doi": doi,
        "title": raw.get("title", ""),
        "authors": _parse_authors(raw.get("authors", "")),
        "author_corresponding": raw.get("author_corresponding"),
        "author_corresponding_institution": raw.get("author_corresponding_institution"),
        "abstract": raw.get("abstract", ""),
        "date": raw.get("date"),
        "version": version,
        "type": raw.get("type"),
        "license": raw.get("license"),
        "category": raw.get("category"),
        "server": server,
        "published_doi": raw.get("published") if raw.get("published") != "NA" else None,
        "jatsxml": raw.get("jatsxml"),
        "pdf_url": pdf_url,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_paper(doi: str) -> dict[str, Any]:
    """Fetch a paper by bioRxiv/medRxiv DOI, using cache when available.

    Tries bioRxiv first, then medRxiv if not found.
    """
    bare = _normalize_doi(doi)
    canonical = _canonical_key(doi)

    cached = cache.get(NAMESPACE, "papers", canonical)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Try bioRxiv first
            url = f"{_BASE_URL}/details/biorxiv/{bare}/na/json"
            response = await _throttled_get(client, url)
            response.raise_for_status()

            data = response.json()
            collection = data.get("collection", [])

            if not collection:
                # Try medRxiv
                url = f"{_BASE_URL}/details/medrxiv/{bare}/na/json"
                response = await _throttled_get(client, url)
                response.raise_for_status()
                data = response.json()
                collection = data.get("collection", [])
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("bioRxiv", e)

    if not collection:
        return {"error": f"No paper found for DOI: {doi}"}

    raw = _pick_latest_version(collection)
    paper = _parse_paper(raw)
    cache.put(NAMESPACE, "papers", canonical, paper)
    return paper


def _pdf_filename(canonical: str) -> str:
    """Build a human-readable PDF filename from a canonical DOI."""
    # Replace slashes and dots for filesystem safety
    return canonical.replace("/", "_") + ".pdf"


def pdf_path(doi: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet)."""
    canonical = _canonical_key(doi)
    return cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)


async def download_pdf(doi: str) -> dict[str, Any]:
    """Download the PDF for a bioRxiv/medRxiv paper and cache it locally.

    Returns a dict with the file path and size, or an error.
    """
    canonical = _canonical_key(doi)
    dest = cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)

    if dest.exists():
        return {
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "cached": True,
        }

    # Need paper metadata to get the PDF URL
    paper = await get_paper(doi)
    if "error" in paper:
        return paper

    pdf_url = paper.get("pdf_url")
    if not pdf_url:
        return {"error": f"No PDF URL found for DOI: {doi}"}

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await _throttled_get(client, pdf_url)

        if response.status_code == 404:
            return {"error": f"PDF not found for DOI: {doi}"}

        response.raise_for_status()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("bioRxiv", e)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)

    return {
        "path": str(dest),
        "size_bytes": len(response.content),
        "cached": False,
    }
