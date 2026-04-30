import asyncio
import re
import time
from pathlib import Path
from typing import Any

import httpx

from . import _clients, _http, _singleflight, _stats, cache

NAMESPACE = "acl_anthology"

# ACL Anthology DOI prefix — all ACL venue papers use this
_ACL_DOI_PREFIX = "10.18653/v1/"

# Pooled client + canonical throttle shape. ACL Anthology has no documented
# rate limit so the gap is zero, but the burst cap, retry plumbing, and
# pooled connection still apply — same robustness primitives every other
# provider gets, just without the per-second pacing.
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
_MIN_REQUEST_GAP = 0.0
_MAX_PENDING = 5
_pending: int = 0

# Coalesces concurrent download_pdf calls for the same paper so two
# tools racing for the same PDF don't both fetch it.
_single_flight = _singleflight.SingleFlight()

# PDF downloads are larger than a metadata call; use a generous timeout.
_PDF_TIMEOUT_SECONDS = 60.0


async def _throttled_get(
    client: httpx.AsyncClient, url: str, **kwargs: Any
) -> httpx.Response:
    """Execute a GET against ACL Anthology with the canonical pooled shape.

    No throttle gap (the site has no documented rate limit), but the
    burst cap, retry plumbing, and stats counters still apply so a
    misbehaving fan-out fails fast and a transient blip surfaces a
    structured error.
    """
    global _last_request_time, _pending
    if _pending >= _MAX_PENDING:
        _stats.incr(NAMESPACE, "backpressure_refusals")
        raise _http.LocalBackpressureError(
            "ACL Anthology", _pending, _MAX_PENDING, _MIN_REQUEST_GAP
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
                backoff_seconds=1.0,
                provider=NAMESPACE,
                **kwargs,
            )
            _last_request_time = time.monotonic()
            return response
    finally:
        _pending -= 1


# ---------------------------------------------------------------------------
# DOI → Anthology ID resolution
# ---------------------------------------------------------------------------


def is_acl_doi(doi: str) -> bool:
    """Check if a DOI belongs to the ACL Anthology."""
    return _normalize_doi(doi).startswith(_ACL_DOI_PREFIX)


def _normalize_doi(doi: str) -> str:
    """Normalize a DOI to bare form (e.g., 10.18653/v1/2023.acl-long.1)."""
    doi = doi.strip()
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    elif doi.startswith("http://doi.org/"):
        doi = doi[len("http://doi.org/"):]
    elif doi.startswith("doi:"):
        doi = doi[len("doi:"):]
    return doi


def doi_to_anthology_id(doi: str) -> str | None:
    """Extract an ACL Anthology ID from a DOI.

    e.g., "10.18653/v1/2023.acl-long.1" -> "2023.acl-long.1"
    Returns None if the DOI is not an ACL Anthology DOI.
    """
    bare = _normalize_doi(doi)
    if not bare.startswith(_ACL_DOI_PREFIX):
        return None
    return bare[len(_ACL_DOI_PREFIX):]


def _canonical_key(doi: str) -> str:
    """Return a canonical cache key from an ACL DOI."""
    return _normalize_doi(doi).lower()


def pdf_url(anthology_id: str) -> str:
    """Build the direct PDF URL for an Anthology paper."""
    return f"https://aclanthology.org/{anthology_id}.pdf"


# ---------------------------------------------------------------------------
# PDF download
# ---------------------------------------------------------------------------


def _pdf_filename(anthology_id: str) -> str:
    """Build a human-readable PDF filename from an Anthology ID."""
    return anthology_id.replace("/", "_") + ".pdf"


def pdf_path(doi: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet)."""
    aid = doi_to_anthology_id(doi)
    if aid is None:
        return Path("/dev/null")
    return cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(aid)


async def download_pdf(
    doi: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Download the PDF for an ACL Anthology paper and cache it locally.

    ``force_refresh=True`` removes the cached PDF and re-downloads. The
    Anthology occasionally re-issues camera-ready PDFs at the same URL,
    so this is the escape hatch when the cached file is wrong.

    Returns a dict with the file path and size, or an error. Concurrent
    callers for the same DOI share one fetch via single-flight.
    """
    aid = doi_to_anthology_id(doi)
    if aid is None:
        return {"error": f"Not an ACL Anthology DOI: {doi}"}

    canonical = _canonical_key(doi)
    dest = cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(aid)

    if force_refresh and dest.exists():
        dest.unlink()

    if dest.exists():
        return {
            "anthology_id": aid,
            "pdf_url": pdf_url(aid),
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "cached": True,
        }

    async def _fetch() -> dict[str, Any]:
        # Re-check after acquiring the slot — a concurrent leader may
        # have just written the file.
        if dest.exists():
            return {
                "anthology_id": aid,
                "pdf_url": pdf_url(aid),
                "path": str(dest),
                "size_bytes": dest.stat().st_size,
                "cached": True,
            }

        url = pdf_url(aid)

        try:
            client = _clients.get_client(
                NAMESPACE, timeout=_PDF_TIMEOUT_SECONDS
            )
            response = await _throttled_get(client, url)

            if response.status_code == 404:
                return {"error": f"PDF not found on ACL Anthology for: {aid}"}

            response.raise_for_status()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("ACL Anthology", e)

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)

        return {
            "anthology_id": aid,
            "pdf_url": url,
            "path": str(dest),
            "size_bytes": len(response.content),
            "cached": False,
        }

    return await _single_flight.do(canonical, _fetch)
