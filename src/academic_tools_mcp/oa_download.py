"""Open-access PDF download path for generic publisher DOIs.

The native PDF providers (arxiv, biorxiv, acl_anthology) build a known
CDN URL from the identifier. Generic publisher DOIs have no such direct
URL — but OpenAlex metadata often surfaces an open-access PDF link
(``best_oa_location.pdf_url`` etc.). This module fetches *only* that
OpenAlex-surfaced URL, never an arbitrary caller-supplied one, so the
server stays a metadata-gated fetcher rather than a general scraper.

It mirrors the canonical provider shape (pooled client, ``_request_slot``
gating, single-flight, streaming download via ``_pdf_download``) but the
fetched URL points at arbitrary publisher domains rather than one API, so
it gets its own conservative concurrency cap rather than borrowing
OpenAlex's api-tuned slot. The downloaded PDF lands in the ``manual``
cache namespace so ``convert_paper`` and the rest of the pipeline find it
with no duplicate download.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

from . import _clients, _http, _pdf_download, _singleflight, _stats, cache, manual
from .providers import openalex

NAMESPACE = "oa_download"

# Conservative concurrency: OA URLs hit arbitrary publisher domains, not a
# single API with a documented budget, so we keep simultaneous fetches low
# to avoid hammering any one host. No inter-start gap (hosts differ), but
# the burst cap and pooled connection still apply — the same robustness
# primitives every other provider gets.
_MAX_CONCURRENT = 2
_request_sem = asyncio.Semaphore(_MAX_CONCURRENT)
_request_lock = asyncio.Lock()
_last_request_time: float = 0.0
# Vestigial for parity with the other PDF providers: with no gap the
# gap-lock computation below never sleeps. Kept so the slot keeps the
# uniform three-layer shape (burst cap → sem → gap-lock).
_MIN_REQUEST_GAP = 0.0
_MAX_PENDING = 5
_pending: int = 0

# Coalesces concurrent download_pdf calls for the same paper.
_single_flight = _singleflight.SingleFlight()

# PDF downloads are larger than a metadata call; use a generous timeout.
_PDF_TIMEOUT_SECONDS = 60.0

# Negative-cache definitive download failures (closed-access, dead or
# landing-page-only OA URL) so a retrying agent doesn't re-resolve OpenAlex
# and re-fetch the same non-PDF on every call. 24h TTL — long enough to stop
# the churn, short relative to how often a paper's OA status flips.
# force_refresh clears the entry.
_NEG_ENTITY = "downloads"
_NEG_TTL_SECONDS = 24 * 60 * 60

# Escape hatch appended to definitive failures: the agent fetches the PDF out
# of band and re-imports it under the same identifier.
_IMPORT_SUGGESTION = (
    "Fetch the PDF yourself (publisher site, institutional access, browser, "
    "curl) and call import_paper(file_path, identifier) with the same "
    "identifier."
)


def _is_definitive_failure(result: dict[str, Any]) -> bool:
    """Whether a download result is a permanent, paper-intrinsic failure.

    Only these are worth negative-caching. A transient transport / OpenAlex
    error (``retryable: True``) will succeed on retry, and a ``MAX_PDF_BYTES``
    abort (carries ``max_bytes``) is a config choice a cap bump fixes — caching
    either would strand the caller behind a stale miss until the TTL expires or
    a force_refresh. What remains (no OA URL, dead/landing-page URL) is genuinely
    about the paper, not the request.
    """
    return "error" in result and result.get("retryable") is not True and "max_bytes" not in result


def _cached_hit(dest: Path) -> dict[str, Any]:
    """Build the ``cached: True`` response for an existing on-disk PDF."""
    return {"path": str(dest), "size_bytes": dest.stat().st_size, "cached": True}


@contextlib.asynccontextmanager
async def _request_slot(url: str):
    """Acquire the OA-download rate-limit slot for the with-block lifetime.

    No throttle gap (each URL is a different host), but the concurrency
    cap, burst cap, and stats counters still apply so a misbehaving
    fan-out fails fast and a streaming download holds an open connection
    that counts toward the cap.
    """
    global _last_request_time, _pending
    if _pending >= _MAX_PENDING:
        _stats.incr(NAMESPACE, "backpressure_refusals")
        raise _http.LocalBackpressureError("OA download", _pending, _MAX_PENDING, _MIN_REQUEST_GAP)
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


async def _resolve_and_download(
    identifier: str, dest: Path, *, force_refresh: bool
) -> dict[str, Any]:
    """Resolve the OA PDF URL via OpenAlex and stream it to ``dest``.

    Returns the raw success / error dict with no caching applied — the caller
    decides whether a failure is worth negative-caching.
    """
    work = await openalex.get_work(identifier, force_refresh=force_refresh)
    if "error" in work:
        # A transient (retryable) lookup error is surfaced as-is so the agent
        # retries — telling it to go fetch the PDF by hand would be wrong. Only
        # a definitive miss (404 / not in OpenAlex) gets the import hatch.
        if work.get("retryable") is True:
            return work
        return {**work, "suggestion": _IMPORT_SUGGESTION}

    url = openalex.best_pdf_url(work)
    if not url:
        oa_status = (work.get("open_access") or {}).get("oa_status")
        return {
            "error": (
                f"No open-access PDF URL available for {identifier!r} "
                f"(oa_status={oa_status!r}). The paper may be "
                "closed-access, or OpenAlex only knows a landing page."
            ),
            "retryable": False,
            "suggestion": _IMPORT_SUGGESTION,
        }

    client = _clients.get_client(NAMESPACE, timeout=_PDF_TIMEOUT_SECONDS)
    return await _pdf_download.stream_to_file(
        client,
        url,
        dest,
        slot_factory=lambda: _request_slot(url),
        provider_label="OA download",
        timeout=_PDF_TIMEOUT_SECONDS,
        require_pdf=True,
        not_found_message=(f"Open-access PDF not found at {url} for {identifier}"),
    )


async def download_pdf(identifier: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download a generic-DOI PDF via its OpenAlex open-access URL.

    Resolves the paper's metadata through OpenAlex, picks the best
    open-access PDF URL (``openalex.best_pdf_url``), and streams it into
    the ``manual`` cache namespace so the rest of the pipeline finds it.

    Returns ``{path, size_bytes, cached}`` on success or a structured
    ``{error, suggestion?}`` on failure: the paper isn't in OpenAlex, has
    no open-access PDF URL, or the fetched URL isn't actually a PDF.
    Definitive failures are negative-cached (24h) so a retrying agent
    doesn't re-resolve and re-fetch on every call; ``force_refresh`` clears
    that entry. Concurrent callers for the same identifier share one fetch
    via single-flight.
    """
    target = manual._resolve_target(identifier)
    canonical = target["canonical"]
    dest = Path(target["pdf_path"])

    if force_refresh:
        cache.invalidate(NAMESPACE, _NEG_ENTITY, canonical)
    else:
        # A 0-byte / pre-header leftover from an interrupted write is treated as
        # a miss (mirrors the manual import path), not served as a stale hit.
        if manual._looks_like_cached_pdf(dest):
            return _cached_hit(dest)
        neg = cache.get_negative(NAMESPACE, _NEG_ENTITY, canonical)
        if neg is not None:
            return neg

    async def _fetch() -> dict[str, Any]:
        # Re-check after acquiring the slot — a concurrent leader may have just
        # written the file or recorded a definitive failure. Skip under
        # force_refresh: the caller wants fresh bytes, and stream_to_file
        # replaces dest atomically.
        if not force_refresh:
            if manual._looks_like_cached_pdf(dest):
                return _cached_hit(dest)
            neg = cache.get_negative(NAMESPACE, _NEG_ENTITY, canonical)
            if neg is not None:
                return neg

        result = await _resolve_and_download(identifier, dest, force_refresh=force_refresh)
        if _is_definitive_failure(result):
            cache.put_negative(
                NAMESPACE, _NEG_ENTITY, canonical, result, ttl_seconds=_NEG_TTL_SECONDS
            )
        return result

    return await _single_flight.do(canonical, _fetch)
