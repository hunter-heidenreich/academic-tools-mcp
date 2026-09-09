"""bioRxiv/medRxiv client. One API, two servers the shared ``DOI_PREFIX`` can't tell apart."""

import re
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..download import streaming
from ..net import clients, http
from ..net.throttle import Throttle
from ..store import cache, singleflight, stems
from ..util import doinorm, useragent

NAMESPACE = "biorxiv"

# Agent-facing provider name; every site that names us reads it.
LABEL = "bioRxiv"
_BASE_URL = "https://api.biorxiv.org"

_PARSE_ERRORS = http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable bioRxiv response."""
    return http.parse_error_dict(LABEL)


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``clients.get_client``."""
    return clients.get_client(NAMESPACE, headers=useragent.headers(), timeout=30.0)


# Shared by both servers. Exported for the reason ``doinorm`` exports
# ``REGISTRANT_PATTERN``: ``corpus`` inverts a stored stem and needs the prefix,
# not the function.
DOI_PREFIX = "10.1101/"

# No documented limit, so pace conservatively. Concurrency 2 lets a metadata and a
# PDF fetch overlap; past ``_MAX_PENDING`` a stacked caller gets backpressure, not
# silent queueing.
_MAX_CONCURRENT = 2
_MIN_REQUEST_GAP = 0.5
_MAX_PENDING = 5

_single_flight = singleflight.SingleFlight()

# Short: a DOI is minted on upload, so a miss at 9am can be live by 10. Definitive
# PDF-download failures share it — an unrendered PDF is the same "live soon" case.
_NEG_TTL_SECONDS = 3600.0
_NEG_ENTITY = "downloads"

_PDF_TIMEOUT_SECONDS = 60.0

# Long, but not unbounded: ``published_doi`` appears asynchronously when a preprint
# becomes a journal article, and the TTL bounds how long an agent can miss that.
_POSITIVE_TTL_SECONDS = 7 * 86400.0


_throttle = Throttle(
    namespace=NAMESPACE,
    label=LABEL,
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


def _request_slot(url: str) -> AbstractAsyncContextManager[None]:
    """bioRxiv's rate-limit slot (``Throttle.slot``). Module-level: it is a test seam."""
    return _throttle.slot(url)


async def _throttled_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at bioRxiv's rate. Url-only: ``_get_client`` is the only place to configure it."""
    return await _throttle.get(_get_client(), url, **kwargs)


# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------

# As permissive as ``doinorm._DOI_URL_RE``, and for the same reason: a spelling this
# misses is one ``manual`` files the same paper under a second time. The capture is the
# path after ``/content/``, minus query, fragment and trailing slash; ``_RENDER_TAIL_RE``
# trims its tail.
_BIORXIV_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:bio|med)rxiv\.org/content/([^\s?#]+?)/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# A rendering of the paper rather than the paper: a version suffix and anything after
# it (``v2``, ``v2.full.pdf``), or a trailing dotted *word* segment (``.full``,
# ``.abstract``, ``.supplementary-material``). The word alternative's non-digit first
# character is what keeps it off the DOI's own dotted suffix — every segment of
# ``2024.01.01.573838`` is numeric.
#
# Invariant: the version is not identity here, deliberately the opposite of ``arxiv``.
# bioRxiv mints one DOI for all versions, the details API is only ever asked for the
# whole set (``/na/``) and ``_pick_latest_version`` takes the newest; a suffixed key
# names no record and upstream rejects it ("DOI not recognizable") as a well-formed
# empty collection, which this module would negative-cache as a definitive miss.
_RENDER_TAIL_RE = re.compile(r"(?:v\d+[^/]*|(?:\.[^./\d][^./]*)+)/?$", re.IGNORECASE)


def _normalize_doi(doi: str) -> str:
    """Normalize a bioRxiv/medRxiv DOI to bare, versionless form.

    Generic spellings (bare, ``doi:``, doi.org URL) are :mod:`doinorm`'s; bioRxiv's own
    is the content URL, any case, with or without scheme, version suffix and rendering
    tail::

        www.biorxiv.org/content/10.1101/2024.01.01.573838v1.abstract
        https://www.medrxiv.org/content/10.1101/2020.01.01.12345v2.full.pdf

    Idempotent.
    """
    doi = doinorm.normalize(doi)

    if m := _BIORXIV_URL_RE.match(doi):
        doi = m.group(1)

    # Gated: the version rule is bioRxiv's, and ``is_biorxiv_doi`` shows this every DOI.
    if doi.startswith(DOI_PREFIX):
        doi = _RENDER_TAIL_RE.sub("", doi)

    return doi


def canonical_key(doi: str) -> str:
    """Return a canonical cache key from a bioRxiv/medRxiv DOI."""
    return _normalize_doi(doi).lower()


def is_biorxiv_doi(doi: str) -> bool:
    """Check if a DOI belongs to bioRxiv or medRxiv."""
    return _normalize_doi(doi).startswith(DOI_PREFIX)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_authors(author_str: str) -> list[dict[str, str]]:
    """Parse bioRxiv's ``"Last, First; Last, First; ..."`` into structured dicts."""
    authors: list[dict[str, str]] = []
    if not author_str:
        return authors

    for raw_part in author_str.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        # "Last, First M." -> {"name": "First M. Last"}
        if "," in part:
            last, _, first = part.partition(",")
            last, first = last.strip(), first.strip()
            name = f"{first} {last}".strip() if first else last
        else:
            name = part
        authors.append({"name": name})
    return authors


def _safe_version(entry: dict[str, Any]) -> int:
    """Parse a collection entry's version as int, treating junk as 0.

    A malformed entry must not crash ``_pick_latest_version`` (and with it the
    whole ``get_paper`` call) — an unparseable version just sorts to the bottom.
    """
    try:
        return int(entry.get("version", "0"))
    except (TypeError, ValueError):
        return 0


def _collection_of(data: Any) -> list[dict[str, Any]] | None:
    """``None`` for a wrong-*shape* body, a list (possibly empty) for a well-formed one.

    The shape check is load-bearing, not defensive padding: ``data.get(...)``
    raises AttributeError on a JSON ``null``/scalar body and a collection of
    non-dicts raises inside ``_pick_latest_version``, and **neither
    AttributeError nor TypeError is in ``_PARSE_ERRORS`` or ``HTTPX_ERRORS``**
    — so an unguarded body escapes the provider entirely instead of surfacing
    as the uniform ``{error, retryable}`` contract.
    """
    if not isinstance(data, dict):
        return None
    collection = data.get("collection")
    if not isinstance(collection, list):
        return None
    entries = [entry for entry in collection if isinstance(entry, dict)]
    if collection and not entries:
        # Non-empty but nothing usable in it — malformed, not "no results".
        return None
    return entries


def _pick_latest_version(collection: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the latest version from a bioRxiv API collection array."""
    return max(collection, key=_safe_version)


def _parse_paper(raw: dict[str, Any], requested_doi: str = "") -> dict[str, Any]:
    """Convert a raw bioRxiv API entry into a normalized paper dict.

    A key present as JSON ``null`` defeats ``.get(k, default)``, so every field the PDF
    URL is built from uses ``or``.
    """
    server = "medrxiv" if "medrxiv" in (raw.get("server") or "").lower() else "biorxiv"
    version = raw.get("version") or "1"
    doi = raw.get("doi") or requested_doi

    # bioRxiv writes the literal "NA" for an unpublished preprint, and sometimes "" —
    # both, and a missing key, mean "no journal DOI yet", never a published_doi of "".
    published = (raw.get("published") or "").strip()

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
        "published_doi": published if published and published != "NA" else None,
        "jatsxml": raw.get("jatsxml"),
        "pdf_url": f"https://www.{server}.org/content/{doi}v{version}.full.pdf" if doi else None,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_paper(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a paper by bioRxiv/medRxiv DOI, using cache when available.

    Tries bioRxiv, falling back to medRxiv unless bioRxiv answered with a non-empty
    well-formed collection. Concurrent callers for one DOI share a fetch.

    ``force_refresh=True`` drops both cache halves before fetching — how the agent picks
    up a ``published_doi`` that has just appeared.
    """
    bare = _normalize_doi(doi)
    canonical = bare.lower()
    not_found_error = f"No paper found for DOI: {doi}"

    async def _fetch() -> dict[str, Any]:
        # Quoted so a reserved character in the DOI can't split the path.
        path_doi = quote(bare, safe="/")
        biorxiv_url = f"{_BASE_URL}/details/biorxiv/{path_doi}/na/json"

        # The DOI is a *middle* segment, so an empty one needs catching too: both
        # shorten the path to a live route. Uncached — no request was spent.
        if "" in bare.split("/") or not http.addresses_a_record(biorxiv_url):
            return http.not_found(not_found_error)

        try:
            response = await _throttled_get(biorxiv_url)
            response.raise_for_status()
            collection = _collection_of(response.json())

            if not collection:
                # The details API answers an unknown DOI with 200 and an empty
                # collection, never a 404, so the medRxiv fallback always gets a chance.
                # A wrong-shape first response falls through here too: medRxiv may still
                # answer cleanly. (A body that doesn't parse at all never reaches this
                # branch — it raises at `.json()`.)
                response = await _throttled_get(f"{_BASE_URL}/details/medrxiv/{path_doi}/na/json")
                response.raise_for_status()
                fallback = _collection_of(response.json())

                if not fallback:
                    # Definitive only if *both* servers answered well-formed; either one
                    # garbled and nothing was established — retryable, uncached.
                    if collection is None or fallback is None:
                        return _parse_error_dict()
                    err = http.not_found(not_found_error)
                    cache.put_negative(
                        NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS
                    )
                    return err

                collection = fallback

            paper = _parse_paper(_pick_latest_version(collection), bare)
        except _PARSE_ERRORS:
            # A 200 body we couldn't parse is transient, not "not found" — never
            # negative-cached.
            return _parse_error_dict()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        cache.put(NAMESPACE, "papers", canonical, paper)
        return paper

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="papers",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
    )


def pdf_path(doi: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet)."""
    return stems.pdf_path(NAMESPACE, canonical_key(doi))


async def download_pdf(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download the PDF for a bioRxiv/medRxiv paper and cache it locally.

    Returns the file path and size, or an error. ``force_refresh=True`` re-downloads
    and atomically replaces the cached file — one DOI covers every version, so a revision
    lands under the same key. Concurrent callers for one DOI share a download.
    """
    canonical = canonical_key(doi)
    dest = stems.pdf_path(NAMESPACE, canonical)

    async def _fetch() -> dict[str, Any]:
        # Threaded through: the URL embeds ``v{version}``, so a stale record would
        # re-download the old bytes.
        paper = await get_paper(doi, force_refresh=force_refresh)
        if "error" in paper:
            return paper

        pdf_url = paper.get("pdf_url")
        if not pdf_url:
            # Definitive: the record exists but carries no PDF URL.
            return {"error": f"No PDF URL found for DOI: {doi}", "retryable": False}

        return await streaming.stream_to_file(
            _get_client(),
            pdf_url,
            dest,
            slot_factory=lambda: _request_slot(pdf_url),
            namespace=NAMESPACE,
            provider_label=LABEL,
            timeout=_PDF_TIMEOUT_SECONDS,
            not_found_message=f"PDF not found for DOI: {doi}",
        )

    # Tuple-keyed to stay distinct from get_paper's slot, which _fetch awaits — one key
    # for both deadlocks.
    return await streaming.cached_download(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity=_NEG_ENTITY,
        canonical=canonical,
        dest=dest,
        fetch=_fetch,
        neg_ttl=_NEG_TTL_SECONDS,
        force_refresh=force_refresh,
        sf_key=("pdf", canonical),
    )
