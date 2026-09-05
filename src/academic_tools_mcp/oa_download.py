"""Open-access PDF download path for generic publisher DOIs.

from contextlib import AbstractAsyncContextManager
import httpx
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

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx

from . import _clients, _pdf_download, _singleflight, _useragent, manual
from ._throttle import Throttle
from .providers import openalex

NAMESPACE = "oa_download"


def _get_client() -> httpx.AsyncClient:
    """Return the persistent AsyncClient for open-access download calls.

    The descriptive User-Agent is baked in at construction so every call
    identifies this client. Previously no headers were passed here at all, so
    requests went out as ``python-httpx/x.y`` — the generic agent several
    upstreams throttle hardest, and the one that leaves an operator no way to
    reach us.
    """
    return _clients.get_client(
        NAMESPACE, headers=_useragent.headers(), timeout=_PDF_TIMEOUT_SECONDS
    )


# Conservative concurrency: OA URLs hit arbitrary publisher domains, not a
# single API with a documented budget. The cap stays *global* rather than
# per-host because it bounds our own egress — sockets, file descriptors, and
# (since stream_to_file holds the slot for the whole stream) simultaneous
# in-flight downloads. Per-host concurrency would let a 20-publisher reference
# walk open 40 parallel PDF streams, which is a resource bug on our side no
# matter how polite it is to each publisher.
_MAX_CONCURRENT = 2

# One request per second, **per publisher host** (``per_host=True`` below).
# This was 0.0 on the reasoning that every OA URL is a different host — an
# assumption, not a fact: the URLs come from OpenAlex, and a reference walk
# through one journal resolves many DOIs to the same domain, which then got
# fetched back-to-back with no gap at all. 1 req/s/host is the conventional
# Crawl-delay and matches wikipedia's gap, the most conservative we use for a
# host we actually have a relationship with; publishers are less friendly than
# a preprint server and PDF fetches are far heavier than JSON. Paced per host,
# so a walk spanning several publishers still runs at the concurrency cap.
_MIN_REQUEST_GAP = 1.0
_MAX_PENDING = 5

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


_throttle = Throttle(
    namespace=NAMESPACE,
    label="OA download",
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
    per_host=True,
)


def _request_slot(url: str) -> AbstractAsyncContextManager[None]:
    """OA-download rate-limit slot (see ``Throttle.slot``).

    Kept module-level so the streaming PDF download's ``slot_factory`` lambda
    and the test seam resolve a module attribute.
    """
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

    client = _get_client()
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
    target = manual.resolve_target(identifier)
    dest = Path(target["pdf_path"])

    async def _fetch() -> dict[str, Any]:
        return await _resolve_and_download(identifier, dest, force_refresh=force_refresh)

    # The negative half lives under this module's own namespace while the PDF
    # lands in ``manual`` — the artifact is shared with manual import, but the
    # "no OA copy exists" verdict is specific to this path.
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
