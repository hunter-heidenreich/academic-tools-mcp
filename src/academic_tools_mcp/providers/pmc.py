"""Europe PMC client: open-access JATS XML full text for PMC-indexed papers.

Gives biomedical DOIs, PMIDs, and OpenAlex work IDs with a PMCID the same
seconds-fast structured markdown path as bioRxiv's JATS XML, reusing
:mod:`academic_tools_mcp.papers.jats` without requiring a PDF download or
MinerU/Marker conversion.
"""

import re
from typing import Any
from urllib.parse import quote

import httpx

from ..download import streaming
from ..net import clients, http
from ..net.throttle import Throttle
from ..store import cache, singleflight
from ..util import doinorm, useragent
from ..util.jsonshape import as_dict
from . import openalex

NAMESPACE = "pmc"

# Agent-facing provider name; every site that names us reads it.
LABEL = "Europe PMC"

_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_JATS_MARKER = "<article"
_JATS_TIMEOUT_SECONDS = 60.0

# Polite pacing against Europe PMC's public REST API.
_MAX_CONCURRENT = 2
_MIN_REQUEST_GAP = 0.35
_MAX_PENDING = 4

# A paper absent from PMC Open Access stays absent; 24h avoids repeating misses.
_NEG_TTL_SECONDS = 86400.0

_PMCID_RE = re.compile(r"\bPMC(\d+)\b", re.IGNORECASE)

_single_flight = singleflight.SingleFlight()

_throttle = Throttle(
    namespace=NAMESPACE,
    label=LABEL,
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``clients.get_client``."""
    return clients.get_client(NAMESPACE, headers=useragent.headers(), timeout=_JATS_TIMEOUT_SECONDS)


async def _throttled_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at Europe PMC's rate. Url-only: ``_get_client`` is the only place to configure it."""
    return await _throttle.get(_get_client(), url, **kwargs)


def normalize_pmcid(raw: str) -> str | None:
    """Extract a canonical ``PMC<digits>`` ID from a bare ID or PMC URL, else ``None``."""
    match = _PMCID_RE.search(raw.strip())
    if match is None:
        return None
    return f"PMC{match.group(1)}"


def extract_pmcid(work: dict[str, Any]) -> str | None:
    """Extract a canonical PMCID from an OpenAlex work dict's ``ids.pmcid``, else ``None``."""
    raw = as_dict(work.get("ids")).get("pmcid")
    if not isinstance(raw, str) or not raw:
        return None
    return normalize_pmcid(raw)


def _canonical_key(identifier: str) -> str:
    """Cache key for PMC negative-cache lookups."""
    if openalex.is_work_id(identifier):
        return openalex.canonical_work_id(identifier)
    return doinorm.canonical(identifier)


async def get_jats(identifier: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch open-access JATS XML from Europe PMC for a DOI or OpenAlex work ID.

    Resolves the paper's PMCID through OpenAlex's ``ids.pmcid`` (free on a work
    already in the OpenAlex cache), then fetches ``/{pmcid}/fullTextXML`` from
    Europe PMC and validates it via :func:`streaming.markup_dict`.

    Returns ``{"markup": text}`` on success, or an error dict:
      - ``not_found: True`` when the work has no PMCID or Europe PMC returns 404
        (negative-cached for 24h; the converted markdown is the only positive cache).
      - ``retryable: True`` on transient OpenAlex or Europe PMC failures.
    """
    canonical = _canonical_key(identifier)
    if force_refresh:
        cache.invalidate(NAMESPACE, "jats", canonical)
    elif (neg := cache.get_negative(NAMESPACE, "jats", canonical)) is not None:
        return neg

    async def _fetch() -> dict[str, Any]:
        work = await openalex.get_work(identifier, force_refresh=force_refresh)
        if "error" in work:
            if work.get("not_found") is True:
                err = http.not_found(f"No PMC JATS full text for: {identifier}")
                cache.put_negative(NAMESPACE, "jats", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
                return err
            return work

        pmcid = extract_pmcid(work)
        if pmcid is None:
            err = http.not_found(f"No PMCID recorded in OpenAlex for: {identifier}")
            cache.put_negative(NAMESPACE, "jats", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
            return err

        url = f"{_BASE_URL}/{quote(pmcid, safe='')}/fullTextXML"
        try:
            response = await _throttled_get(url, timeout=_JATS_TIMEOUT_SECONDS)
            if response.status_code == 404:
                err = http.not_found(f"{LABEL}: no open-access JATS full text for {pmcid}")
                cache.put_negative(NAMESPACE, "jats", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
                return err
            response.raise_for_status()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        return streaming.markup_dict(
            response,
            label=LABEL,
            kind="JATS XML",
            marker=_JATS_MARKER,
            parse_detail="was not a JATS article",
        )

    return await _single_flight.do(("jats", canonical, force_refresh), _fetch)
