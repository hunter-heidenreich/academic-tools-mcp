import asyncio
import contextlib
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import _clients, _http, _pdf_download, _singleflight, _stats, cache

NAMESPACE = "biorxiv"
_BASE_URL = "https://api.biorxiv.org"

# All bioRxiv/medRxiv DOIs use this prefix
_DOI_PREFIX = "10.1101/"

# Rate limiting: no documented limit, but be polite (~2 req/sec).
# Concurrency cap of 2 allows a metadata + PDF-URL chase to run in
# parallel without hammering the (unmonitored) API.
_MAX_CONCURRENT = 2
_request_sem = asyncio.Semaphore(_MAX_CONCURRENT)
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 0.5

# Burst cap. Same shape as the other providers: past 5 stacked callers,
# the next gets a structured backpressure error instead of silent queueing.
_MAX_PENDING = 5
_pending: int = 0

# Coalesces concurrent calls for the same canonical DOI so the unified
# paper tools called in parallel (metadata, authors, abstract, bibtex)
# don't all fetch independently.
_single_flight = _singleflight.SingleFlight()

# Shorter than the cache.py default 24h. bioRxiv DOIs are minted on
# upload — a paper that 404'd this morning may be visible an hour later
# and the agent shouldn't have to wait a day to see it.
_NEG_TTL_SECONDS = 3600.0

# Positive cache TTL. The published_doi field appears asynchronously
# when a preprint becomes a journal article — a 7-day TTL guarantees
# the agent sees that transition within a week without re-fetching the
# unchanging fields (title, abstract, authors) on every call.
_POSITIVE_TTL_SECONDS = 7 * 86400.0


@contextlib.asynccontextmanager
async def _request_slot(url: str):
    """Acquire bioRxiv's rate-limit slot for the lifetime of the with block.

    See ``arxiv._request_slot`` for the two-stage gating shape; same
    pattern here so streaming PDF downloads can hold the slot open
    while bytes flow.
    """
    global _last_request_time, _pending
    if _pending >= _MAX_PENDING:
        _stats.incr(NAMESPACE, "backpressure_refusals")
        raise _http.LocalBackpressureError(
            "bioRxiv", _pending, _MAX_PENDING, _MIN_REQUEST_GAP
        )
    _pending += 1
    try:
        async with _request_sem:
            async with _request_lock:
                now = time.monotonic()
                elapsed = now - _last_request_time
                wait_seconds = 0.0
                if _last_request_time > 0 and elapsed < _MIN_REQUEST_GAP:
                    wait_seconds = _MIN_REQUEST_GAP - elapsed
                    await asyncio.sleep(wait_seconds)
                _last_request_time = time.monotonic()
            _stats.log_request(NAMESPACE, url, wait_seconds)
            _stats.incr(NAMESPACE, "http_calls")
            yield
    finally:
        _pending -= 1


async def _throttled_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a GET request with polite rate limiting.

    Thin wrapper over ``_request_slot`` — the slot does the gating, this
    just fires the GET (with one transparent retry) inside it. Refuses
    past ``_MAX_PENDING`` queued callers via ``LocalBackpressureError``
    so an agent that fans out gets fast feedback rather than waiting
    half a second per slot.
    """
    async with _request_slot(url):
        return await _http.get_with_retry(
            client, url,
            backoff_seconds=max(_MIN_REQUEST_GAP, 1.0),
            provider=NAMESPACE,
            **kwargs,
        )


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


async def get_paper(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a paper by bioRxiv/medRxiv DOI, using cache when available.

    Tries bioRxiv first, then medRxiv if not found. Concurrent callers
    for the same DOI share one fetch via single-flight.

    ``force_refresh=True`` drops both positive and negative cache entries
    before fetching — useful when the agent wants the latest
    ``published_doi`` for a preprint that may have just been published.
    """
    bare = _normalize_doi(doi)
    canonical = _canonical_key(doi)

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
        cached = cache.get(NAMESPACE, "papers", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)
        if cached is not None:
            return cached
        neg = cache.get_negative(NAMESPACE, "papers", canonical)
        if neg is not None:
            return neg

        try:
            client = _clients.get_client(NAMESPACE, timeout=30.0)
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
            err = {"error": f"No paper found for DOI: {doi}"}
            cache.put_negative(NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
            return err

        raw = _pick_latest_version(collection)
        paper = _parse_paper(raw)
        cache.put(NAMESPACE, "papers", canonical, paper)
        return paper

    return await _single_flight.do(canonical, _fetch)


def _pdf_filename(canonical: str) -> str:
    """Build a human-readable PDF filename from a canonical DOI."""
    # Replace slashes and dots for filesystem safety
    return canonical.replace("/", "_") + ".pdf"


def pdf_path(doi: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet)."""
    canonical = _canonical_key(doi)
    return cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)


async def download_pdf(
    doi: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Download the PDF for a bioRxiv/medRxiv paper and cache it locally.

    ``force_refresh=True`` removes the cached PDF and re-downloads. Use
    when you suspect the cached file is corrupt or the preprint server
    replaced the PDF with a newer version under the same DOI.

    Streams the response to a temp file in chunks (peak memory = one
    chunk, not the whole PDF) and renames into place atomically. The
    download aborts mid-stream if it would exceed ``MAX_PDF_BYTES`` so a
    misrouted URL can't fill the disk.

    Returns a dict with the file path and size, or an error.
    """
    canonical = _canonical_key(doi)
    dest = cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)

    if force_refresh and dest.exists():
        dest.unlink()

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

    client = _clients.get_client(NAMESPACE, timeout=30.0)
    return await _pdf_download.stream_to_file(
        client,
        pdf_url,
        dest,
        slot_factory=lambda: _request_slot(pdf_url),
        provider_label="bioRxiv",
        timeout=60.0,
        not_found_message=f"PDF not found for DOI: {doi}",
    )
