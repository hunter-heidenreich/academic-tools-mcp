import json
from typing import Any
from urllib.parse import quote

import httpx

from .. import _clients, _http, _singleflight, cache
from .._throttle import Throttle

OPENCITATIONS_BASE_URL = "https://api.opencitations.net/index/v2"
NAMESPACE = "opencitations"

# The OpenCitations Index API returns JSON; a malformed/truncated 200 body
# raises ``json.JSONDecodeError`` on ``.json()``. It is handled alongside the
# HTTP errors so the tool always returns the uniform ``{error}`` contract
# rather than crashing on a garbled response.
_PARSE_ERRORS = (json.JSONDecodeError,)


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable OpenCitations response.

    A new dict each call (like ``_http.error_dict``) so a caller — or a
    single-flight follower sharing the result — can't mutate a shared object.
    """
    return {
        "error": "OpenCitations returned a response that could not be parsed.",
        "retryable": True,
    }


# Rate limiting: 180 req/min = 3 req/sec. Enforce a minimum 334ms gap.
# Concurrency cap of 2 lets references + citations fetch in parallel
# (the common pattern for graph traversal) while staying conservative.
# Gating lives in ``_throttle``.
_MAX_CONCURRENT = 2
_MIN_REQUEST_GAP = 0.334  # ~3 req/sec max
_MAX_PENDING = 5

# Coalesces concurrent calls for the same canonical DOI on each
# direction (references / citations). Keyed by (kind, canonical) so a
# parallel references-and-citations fetch on the same paper runs as
# two distinct in-flight slots, not one.
_single_flight = _singleflight.SingleFlight()

# Positive cache TTL. The citation graph grows continuously — incoming
# citations especially. 7 days keeps repeated reads in a session cheap
# while making sure recent citation activity surfaces within a week.
_POSITIVE_TTL_SECONDS = 7 * 86400.0


_throttle = Throttle(
    namespace=NAMESPACE,
    label="OpenCitations",
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


async def _throttled_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Execute a GET respecting OpenCitations' rate limit (see ``Throttle.get``)."""
    return await _throttle.get(client, url, **kwargs)


# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------


def _normalize_doi(doi: str) -> str:
    """Normalize a DOI to bare form (e.g., 10.1234/example).

    Accepts:
      - bare DOI: 10.1234/example
      - prefixed: doi:10.1234/example
      - full URL: https://doi.org/10.1234/example
    """
    doi = doi.strip()
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/") :]
    elif doi.startswith("http://doi.org/"):
        doi = doi[len("http://doi.org/") :]
    elif doi.startswith("doi:"):
        doi = doi[len("doi:") :]
    return doi


def canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return _normalize_doi(doi).lower()


# ---------------------------------------------------------------------------
# ID parsing
# ---------------------------------------------------------------------------


def _parse_ids(raw: str | None) -> dict[str, str]:
    """Parse a space-delimited OpenCitations ID string into a dict.

    Input:  "omid:br/062102024238 doi:10.1103/physrevx.2.031001 openalex:W3101024234 pmid:20079334"
    Output: {"omid": "br/062102024238", "doi": "10.1103/physrevx.2.031001",
             "openalex": "W3101024234", "pmid": "20079334"}
    """
    if not raw:
        return {}
    ids: dict[str, str] = {}
    for token in raw.split():
        if ":" in token:
            prefix, _, value = token.partition(":")
            ids[prefix] = value
    return ids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _format_record(raw: dict[str, Any], id_field: str) -> dict[str, Any]:
    """Format a raw OpenCitations citation record into a clean dict."""
    record: dict[str, Any] = _parse_ids(raw.get(id_field))
    record["creation"] = raw.get("creation")
    record["journal_self_citation"] = raw.get("journal_sc") == "yes"
    record["author_self_citation"] = raw.get("author_sc") == "yes"
    return record


async def _fetch_direction(
    doi: str, *, kind: str, id_field: str, force_refresh: bool
) -> dict[str, Any]:
    """Fetch one citation direction (references / citations) for a DOI.

    The two directions are identical but for three knobs, so they share this
    body rather than two near-copies:
      - ``kind`` — both the API path segment and the cache entity / result key
        (``"references"`` or ``"citations"``).
      - ``id_field`` — the OpenCitations record field naming the *other* end of
        the link (``"cited"`` for outgoing references, ``"citing"`` for incoming
        citations).

    Returns ``{kind: [records], "count": N}``. Concurrent callers for the same
    (direction, DOI) share one fetch; the two directions for one DOI run as two
    distinct single-flight slots (tuple-keyed).
    """
    canonical = canonical_doi(doi)

    async def _fetch() -> dict[str, Any]:
        bare_doi = _normalize_doi(doi)

        try:
            client = _clients.get_client(NAMESPACE, timeout=30.0)
            response = await _throttled_get(
                client,
                # Percent-encode the DOI so reserved characters (#, ?, …)
                # aren't misread as a URL fragment/query and silently fetch
                # the wrong record. The "doi:" scheme prefix and the DOI's
                # own slash stay literal (safe="/").
                f"{OPENCITATIONS_BASE_URL}/{kind}/doi:{quote(bare_doi, safe='/')}",
            )

            if response.status_code == 404:
                err = {"error": f"No {kind} found on OpenCitations for DOI: {doi}"}
                cache.put_negative(NAMESPACE, kind, canonical, err)
                return err

            response.raise_for_status()
            records = response.json()
        except _PARSE_ERRORS:
            # A 200 body we couldn't parse — truncated/garbled. Transient,
            # not "not found": surface a retryable error and do NOT
            # negative-cache it so a retry re-fetches.
            return _parse_error_dict()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("OpenCitations", e)

        if not isinstance(records, list):
            # Anomalous 200 that isn't the expected list of records — treat
            # like a parse failure rather than iterating garbage (or caching
            # an empty result for the TTL).
            return _parse_error_dict()

        formatted = [_format_record(r, id_field) for r in records if isinstance(r, dict)]
        data: dict[str, Any] = {kind: formatted, "count": len(formatted)}

        cache.put(NAMESPACE, kind, canonical, data)
        return data

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity=kind,
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=(kind, canonical),
    )


async def get_references(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch outgoing references for a DOI from OpenCitations.

    Returns a dict with the list of citation records. Each record contains
    parsed IDs (doi, omid, openalex, pmid), creation date, and self-citation
    flags. Concurrent callers for the same DOI share one fetch.

    ``force_refresh=True`` drops both positive and negative cache entries
    before fetching — useful because the citation graph grows continuously,
    so an agent may want fresher reference coverage than the 7-day TTL.
    """
    return await _fetch_direction(
        doi, kind="references", id_field="cited", force_refresh=force_refresh
    )


async def get_citations(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch incoming citations for a DOI from OpenCitations.

    Returns a dict with the list of citation records (works that cite this DOI).
    Concurrent callers for the same DOI share one fetch.

    ``force_refresh=True`` drops both positive and negative cache entries
    before fetching — incoming citations grow continuously, so an agent may
    want a fresher count than the 7-day TTL would serve.
    """
    return await _fetch_direction(
        doi, kind="citations", id_field="citing", force_refresh=force_refresh
    )
