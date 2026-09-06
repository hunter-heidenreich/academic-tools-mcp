"""Open-access PDF download path for generic publisher DOIs.

arxiv/biorxiv/acl_anthology build a PDF URL from the identifier; a generic
publisher DOI has none, but OpenAlex often surfaces one. This module fetches
*only* that OpenAlex-surfaced URL, never a caller-supplied one, so the server
stays a metadata-gated fetcher rather than a general scraper.
"""

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx

from . import _clients, _pdf_download, _singleflight, _useragent, manual
from ._throttle import Throttle
from .providers import openalex

NAMESPACE = "oa_download"


def _get_client() -> httpx.AsyncClient:
    """Return the pooled AsyncClient for open-access download calls.

    Configured here only: ``_clients.get_client`` ignores kwargs on every later
    call for this namespace, so the UA and ``_PDF_TIMEOUT_SECONDS`` are set here
    or not at all. This client only ever downloads, so unlike arxiv/biorxiv it
    needs no per-call timeout override.
    """
    return _clients.get_client(
        NAMESPACE, headers=_useragent.headers(), timeout=_PDF_TIMEOUT_SECONDS
    )


# The slot is held for the whole stream: 2 concurrent downloads, not 2 requests.
_MAX_CONCURRENT = 2

# Per host (per_host=True below): one journal's DOIs all resolve to one domain.
_MIN_REQUEST_GAP = 1.0
_MAX_PENDING = 5

_single_flight = _singleflight.SingleFlight()

_PDF_TIMEOUT_SECONDS = 60.0

# 24h: long enough to stop a retrying agent's churn, short relative to how
# often a paper's OA status flips.
_NEG_ENTITY = "downloads"
_NEG_TTL_SECONDS = 24 * 60 * 60

_IMPORT_SUGGESTION = (
    "Fetch the PDF yourself (publisher site, institutional access, browser, "
    "curl) and call import_paper(file_path, identifier) with the same "
    "identifier."
)


_throttle = Throttle(
    namespace=NAMESPACE,
    label="OA download",
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
    per_host=True,
)


def _request_slot(url: str) -> AbstractAsyncContextManager[None]:
    """Rate-limit slot for one OA fetch (``Throttle.slot``); the test seam."""
    return _throttle.slot(url)


async def _resolve_and_download(
    identifier: str, dest: Path, *, force_refresh: bool
) -> dict[str, Any]:
    """Resolve the OA PDF URL via OpenAlex and stream it to ``dest``.

    Returns the raw success / error dict with no caching applied — the caller
    decides whether a failure is worth negative-caching.
    """
    work = await openalex.get_work(identifier, force_refresh=force_refresh)
    if "error" in work:
        # Allowlist: a non-retryable 4xx arrives unflagged, so unknown != definitive.
        if work.get("not_found") is True or work.get("retryable") is False:
            return {**work, "suggestion": _IMPORT_SUGGESTION}
        return work

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

    client = _get_client()
    result = await _pdf_download.stream_to_file(
        client,
        url,
        dest,
        slot_factory=lambda: _request_slot(url),
        namespace=NAMESPACE,
        provider_label="OA download",
        timeout=_PDF_TIMEOUT_SECONDS,
        require_pdf=True,
        not_found_message=(f"Open-access PDF not found at {url} for {identifier}"),
    )
    # Same hatch for a dead URL. The predicate excludes a cap abort and a 0-byte blip.
    if _pdf_download.is_definitive_failure(result):
        return {**result, "suggestion": _IMPORT_SUGGESTION}
    return result


async def download_pdf(identifier: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download a generic-DOI PDF via its OpenAlex open-access URL.

    Resolves the work through OpenAlex, takes ``best_pdf_url``, and streams it
    into the ``manual`` namespace so the rest of the pipeline finds it.

    Returns ``{path, size_bytes, cached}`` or ``{error, suggestion?}``. The
    failures *this module* establishes are negative-cached (24h); a DOI
    OpenAlex doesn't know rides OpenAlex's own entry. Concurrent callers for
    the same identifier share one fetch.
    """
    target = manual.resolve_target(identifier)
    dest = target["pdf_path"]

    async def _fetch() -> dict[str, Any]:
        return await _resolve_and_download(identifier, dest, force_refresh=force_refresh)

    # force_refresh does two jobs: _fetch re-resolves OpenAlex, cached_download re-streams.
    return await _pdf_download.cached_download(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity=_NEG_ENTITY,
        canonical=target["canonical"],
        dest=dest,
        fetch=_fetch,
        neg_ttl=_NEG_TTL_SECONDS,
        force_refresh=force_refresh,
    )
