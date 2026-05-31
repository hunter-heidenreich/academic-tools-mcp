import asyncio
import contextlib
import re
import time
from pathlib import Path
from typing import Any

from .. import _clients, _http, _pdf_download, _singleflight, _stats, cache, papers

NAMESPACE = "acl_anthology"

# ACL Anthology DOI prefix — all ACL venue papers use this
_ACL_DOI_PREFIX = "10.18653/v1/"

# Pooled client + canonical throttle shape. ACL Anthology has no documented
# rate limit so the gap is zero, but the burst cap, retry plumbing, and
# pooled connection still apply — same robustness primitives every other
# provider gets, just without the per-second pacing.
# Concurrency cap of 4 — ACL Anthology is a static-file CDN with no
# documented rate limit, so we let multiple PDF downloads run in
# parallel; the burst cap still applies past _MAX_PENDING.
_MAX_CONCURRENT = 4
_request_sem = asyncio.Semaphore(_MAX_CONCURRENT)
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


@contextlib.asynccontextmanager
async def _request_slot(url: str):
    """Acquire ACL Anthology's rate-limit slot for the with-block lifetime.

    No throttle gap (the site has no documented rate limit), but the
    concurrency cap, burst cap, and stats counters still apply so a
    misbehaving fan-out fails fast and a streaming download holds an
    open connection that counts toward the cap.
    """
    global _last_request_time, _pending
    if _pending >= _MAX_PENDING:
        _stats.incr(NAMESPACE, "backpressure_refusals")
        raise _http.LocalBackpressureError(
            "ACL Anthology", _pending, _MAX_PENDING, _MIN_REQUEST_GAP
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


# ---------------------------------------------------------------------------
# DOI → Anthology ID resolution
# ---------------------------------------------------------------------------


def _strip_acl_prefix(bare: str) -> str | None:
    """Return the anthology-id suffix if ``bare`` carries the ACL prefix.

    DOIs are officially case-insensitive, so the prefix is matched
    case-insensitively (``10.18653/V1/...`` is just as valid as
    ``10.18653/v1/...``). The prefix is fixed-length, so we lowercase only
    the leading slice and cut at ``len(prefix)`` — the suffix is returned
    untouched for ``_normalize_anthology_id`` to handle. Returns ``None``
    when ``bare`` is not an ACL DOI.
    """
    if bare[: len(_ACL_DOI_PREFIX)].lower() == _ACL_DOI_PREFIX:
        return bare[len(_ACL_DOI_PREFIX) :]
    return None


def is_acl_doi(doi: str) -> bool:
    """Check if a DOI belongs to the ACL Anthology."""
    return _strip_acl_prefix(_normalize_doi(doi)) is not None


def _normalize_doi(doi: str) -> str:
    """Normalize a DOI to bare form (e.g., 10.18653/v1/2023.acl-long.1)."""
    doi = doi.strip()
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/") :]
    elif doi.startswith("http://doi.org/"):
        doi = doi[len("http://doi.org/") :]
    elif doi.startswith("doi:"):
        doi = doi[len("doi:") :]
    return doi


# Old-format Anthology IDs (pre-2020) are <LETTER><2-digit-year>-<digits>, e.g.
# P16-1160, W04-1013, D14-1162. The aclanthology.org CDN serves them under a
# case-sensitive path (P16-1160.pdf resolves, p16-1160.pdf 404s), but Crossref
# hands these DOIs back lowercased. New-format IDs (YYYY.venue-track.n) are
# lowercase and must stay untouched.
_OLD_FORMAT_ID_RE = re.compile(r"^[A-Za-z]\d{2}-\d+$")


def _normalize_anthology_id(anthology_id: str) -> str:
    """Uppercase old-format Anthology IDs so the CDN URL resolves.

    Old-format IDs (P16-1160) are case-sensitive on aclanthology.org; new-format
    IDs (2023.acl-long.1) contain lowercase venue letters that must be preserved.
    Old-format IDs are letter + digits only, so .upper() is safe for them.
    """
    if _OLD_FORMAT_ID_RE.match(anthology_id):
        return anthology_id.upper()
    return anthology_id


def doi_to_anthology_id(doi: str) -> str | None:
    """Extract an ACL Anthology ID from a DOI.

    e.g., "10.18653/v1/2023.acl-long.1" -> "2023.acl-long.1"
    Returns None if the DOI is not an ACL Anthology DOI.
    """
    suffix = _strip_acl_prefix(_normalize_doi(doi))
    if suffix is None:
        return None
    return _normalize_anthology_id(suffix)


def canonical_key(doi: str) -> str:
    """Return a canonical cache key from an ACL DOI."""
    return _normalize_doi(doi).lower()


def pdf_url(anthology_id: str) -> str:
    """Build the direct PDF URL for an Anthology paper."""
    return f"https://aclanthology.org/{anthology_id}.pdf"


# ---------------------------------------------------------------------------
# PDF download
# ---------------------------------------------------------------------------


def _pdf_filename(anthology_id: str) -> str:
    """Build a filesystem/shell-safe PDF filename from an Anthology ID.

    Routes through ``papers._safe_stem`` (same as the manual import path) so
    the filename — which reaches the ``bash -c`` converter — can't carry shell
    metacharacters. Real Anthology IDs are ``[A-Za-z0-9.-]`` only, so they map
    to the same name as before (no cache migration); ``/`` still folds to ``_``.
    """
    return papers._safe_stem(anthology_id) + ".pdf"


def pdf_path(doi: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet).

    Raises ``ValueError`` for a non-ACL DOI rather than returning a sentinel
    path: a path whose ``.exists()`` is truthy (e.g. ``/dev/null``) would let
    a non-PDF slip past ``convert_paper``'s existence guard.
    """
    aid = doi_to_anthology_id(doi)
    if aid is None:
        raise ValueError(f"Not an ACL Anthology DOI: {doi}")
    return cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(aid)


async def download_pdf(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download the PDF for an ACL Anthology paper and cache it locally.

    ``force_refresh=True`` re-downloads and atomically replaces the
    cached PDF. The Anthology occasionally re-issues camera-ready PDFs at
    the same URL, so this is the escape hatch when the cached file is
    wrong. The existing cached file is kept if the re-download fails, so a
    flaky network can't leave you worse off than before.

    Returns a dict with the file path and size, or an error. Concurrent
    callers for the same DOI share one fetch via single-flight.
    """
    aid = doi_to_anthology_id(doi)
    if aid is None:
        return {"error": f"Not an ACL Anthology DOI: {doi}"}

    canonical = canonical_key(doi)
    dest = cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(aid)

    if not force_refresh and dest.exists():
        return {
            "anthology_id": aid,
            "pdf_url": pdf_url(aid),
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "cached": True,
        }

    async def _fetch() -> dict[str, Any]:
        # Re-check after acquiring the slot — a concurrent leader may
        # have just written the file. Skip the short-circuit under
        # force_refresh: the caller explicitly wants fresh bytes, and the
        # streaming download replaces dest atomically on success.
        if not force_refresh and dest.exists():
            return {
                "anthology_id": aid,
                "pdf_url": pdf_url(aid),
                "path": str(dest),
                "size_bytes": dest.stat().st_size,
                "cached": True,
            }

        url = pdf_url(aid)
        client = _clients.get_client(NAMESPACE, timeout=_PDF_TIMEOUT_SECONDS)
        result = await _pdf_download.stream_to_file(
            client,
            url,
            dest,
            slot_factory=lambda: _request_slot(url),
            provider_label="ACL Anthology",
            timeout=_PDF_TIMEOUT_SECONDS,
            not_found_message=f"PDF not found on ACL Anthology for: {aid}",
        )
        if "error" in result:
            return result
        # Add ACL-specific provenance fields on top of the helper's
        # canonical {path, size_bytes, cached} payload.
        return {
            "anthology_id": aid,
            "pdf_url": url,
            **result,
        }

    return await _single_flight.do(canonical, _fetch)
