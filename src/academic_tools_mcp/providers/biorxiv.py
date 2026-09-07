"""bioRxiv/medRxiv client. One API, two servers, distinguished by DOI prefix."""

import re
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .. import _clients, _doi, _http, _pdf_download, _singleflight, _stems, _useragent, cache
from .._throttle import Throttle

NAMESPACE = "biorxiv"
_BASE_URL = "https://api.biorxiv.org"

_PARSE_ERRORS = _http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable bioRxiv response."""
    return _http.parse_error_dict("bioRxiv")


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``_clients.get_client``."""
    return _clients.get_client(NAMESPACE, headers=_useragent.headers(), timeout=30.0)


# All bioRxiv/medRxiv DOIs share this prefix. Exported for the reason ``_doi``
# exports ``REGISTRANT_PATTERN``: ``cache_search`` inverts a stored filename
# stem and needs the prefix rather than the function.
DOI_PREFIX = "10.1101/"

# No documented limit; be polite (~2 req/sec). Concurrency 2 lets a metadata
# and a PDF fetch overlap without hammering an unmonitored API; 5 pending past
# which a stacked caller gets backpressure rather than silent queueing.
_MAX_CONCURRENT = 2
_MIN_REQUEST_GAP = 0.5
_MAX_PENDING = 5

_single_flight = _singleflight.SingleFlight()

# Short: bioRxiv DOIs are minted on upload, so a paper that 404'd this morning
# may be visible within the hour. Definitive PDF-download failures share it —
# a preprint whose PDF is not yet rendered is the same "live in an hour" case.
_NEG_TTL_SECONDS = 3600.0
_NEG_ENTITY = "downloads"

_PDF_TIMEOUT_SECONDS = 60.0

# Long, but not unbounded: ``published_doi`` appears asynchronously when a
# preprint becomes a journal article, and the agent should see that transition
# within a week without re-fetching the unchanging fields on every call.
_POSITIVE_TTL_SECONDS = 7 * 86400.0


_throttle = Throttle(
    namespace=NAMESPACE,
    label="bioRxiv",
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


def _request_slot(url: str) -> AbstractAsyncContextManager[None]:
    """bioRxiv's rate-limit slot (``Throttle.slot``). Module-level: it is a test seam."""
    return _throttle.slot(url)


async def _throttled_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Execute a GET respecting bioRxiv's polite rate limit (see ``Throttle.get``)."""
    return await _throttle.get(client, url, **kwargs)


# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------

# As permissive as ``_doi._DOI_URL_RE``, and for the same reason: a spelling
# this misses is one ``manual`` files the same paper under a second time. The
# capture is everything after ``/content/``; ``_RENDER_TAIL_RE`` trims it.
_BIORXIV_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:bio|med)rxiv\.org/content/([^\s?#]+?)/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# A rendering of the paper rather than the paper: a version suffix and anything
# after it (``v2``, ``v2.full.pdf``), or a trailing dotted *word* segment
# (``.full``, ``.abstract``, ``.supplementary-material``). The second
# alternative requires a non-digit first character, which is what keeps it off
# the DOI's own dotted suffix — every segment of ``2024.01.01.573838`` is
# numeric.
#
# Invariant: none of this is part of the identity here. bioRxiv mints one DOI
# for all versions, the details API is only ever asked for the whole set
# (``/na/``), and ``_pick_latest_version`` then takes the newest — so a
# suffixed key names no distinct record and upstream rejects it outright ("DOI
# not recognizable"), which the details endpoint reports as a well-formed empty
# collection and this module would negative-cache as a definitive miss.
# Deliberately the opposite of ``arxiv``, where the version *is* the identity.
_RENDER_TAIL_RE = re.compile(r"(?:v\d+[^/]*|(?:\.[^./\d][^./]*)+)/?$", re.IGNORECASE)


def _normalize_doi(doi: str) -> str:
    """Normalize a bioRxiv/medRxiv DOI to bare, versionless form.

    Accepts a bare DOI, a ``doi:`` prefix, a doi.org URL, and a bioRxiv or
    medRxiv content URL in any case, with or without a scheme, a version
    suffix and any rendering tail::

        10.1101/2024.01.01.573838
        doi:10.1101/2024.01.01.573838
        https://doi.org/10.1101/2024.01.01.573838
        www.biorxiv.org/content/10.1101/2024.01.01.573838v1.abstract
        https://www.medrxiv.org/content/10.1101/2020.01.01.12345v2.full.pdf

    Generic forms are handled once in :mod:`_doi`; only the content URL and the
    version rule are bioRxiv's own. Idempotent.
    """
    doi = _doi.normalize(doi)

    if m := _BIORXIV_URL_RE.match(doi):
        doi = m.group(1)

    # Gated on the prefix: trimming a ``v\d+`` tail is bioRxiv's rule, and
    # ``_normalize_doi`` also sees every non-bioRxiv DOI through ``is_biorxiv_doi``.
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

    ``.get(k, default)`` returns None when the key is present as JSON ``null``,
    so every field the PDF URL is built from needs ``or``, not a default.
    """
    server = "medrxiv" if "medrxiv" in (raw.get("server") or "").lower() else "biorxiv"
    version = raw.get("version") or "1"
    doi = raw.get("doi") or requested_doi

    # bioRxiv uses the literal "NA" for an unpublished preprint, but an empty
    # string can also appear — treat both (and a missing field) as "no journal
    # DOI yet" so a falsy-but-present "" doesn't leak out as published_doi.
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

    Tries bioRxiv first, then medRxiv if not found. Concurrent callers
    for the same DOI share one fetch via single-flight.

    ``force_refresh=True`` drops both positive and negative cache entries
    before fetching — useful when the agent wants the latest
    ``published_doi`` for a preprint that may have just been published.
    """
    bare = _normalize_doi(doi)
    canonical = bare.lower()

    async def _fetch() -> dict[str, Any]:
        try:
            client = _get_client()
            # Quoted so a reserved character in the DOI can't split the path.
            path_doi = quote(bare, safe="/")

            response = await _throttled_get(
                client, f"{_BASE_URL}/details/biorxiv/{path_doi}/na/json"
            )
            response.raise_for_status()
            collection = _collection_of(response.json())

            if not collection:
                # The details API answers an unknown DOI with 200 and an empty
                # collection rather than a 404, so the medRxiv fallback always
                # gets a chance — nothing in the shared 10.1101/ prefix tells
                # the two servers apart. A wrong-shape first response falls
                # through here too: medRxiv may still answer cleanly. (A body
                # that doesn't parse at all never reaches this branch — it
                # raises out to `_PARSE_ERRORS` at `.json()`.)
                response = await _throttled_get(
                    client, f"{_BASE_URL}/details/medrxiv/{path_doi}/na/json"
                )
                response.raise_for_status()
                fallback = _collection_of(response.json())

                if not fallback:
                    # "Not found" needs *both* servers to have answered
                    # well-formed. If either returned garbage nothing was
                    # established, so stay retryable and cache nothing.
                    if collection is None or fallback is None:
                        return _parse_error_dict()
                    err = {"error": f"No paper found for DOI: {doi}", "not_found": True}
                    cache.put_negative(
                        NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS
                    )
                    return err

                collection = fallback

            paper = _parse_paper(_pick_latest_version(collection), bare)
        except _PARSE_ERRORS:
            # A 200 body we couldn't parse is transient, not "not found":
            # surface a retryable error and do NOT negative-cache it.
            return _parse_error_dict()
        except _http.HTTPX_ERRORS as e:
            return _http.error_dict("bioRxiv", e)

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
    return _stems.pdf_path(NAMESPACE, canonical_key(doi))


async def download_pdf(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download the PDF for a bioRxiv/medRxiv paper and cache it locally.

    ``force_refresh=True`` re-downloads and atomically replaces the cached PDF.
    Use when you suspect the cached file is corrupt or the preprint server
    replaced the PDF under the same DOI.

    Returns a dict with the file path and size, or an error. Concurrent
    callers for the same DOI share one download via single-flight.
    """
    canonical = canonical_key(doi)
    dest = _stems.pdf_path(NAMESPACE, canonical)

    async def _fetch() -> dict[str, Any]:
        # force_refresh is threaded through so a forced re-download doesn't
        # build its URL from the stale version number it was asked to replace.
        paper = await get_paper(doi, force_refresh=force_refresh)
        if "error" in paper:
            return paper

        pdf_url = paper.get("pdf_url")
        if not pdf_url:
            # Definitive: the record exists but carries no PDF URL.
            return {"error": f"No PDF URL found for DOI: {doi}", "retryable": False}

        return await _pdf_download.stream_to_file(
            _get_client(),
            pdf_url,
            dest,
            slot_factory=lambda: _request_slot(pdf_url),
            namespace=NAMESPACE,
            provider_label="bioRxiv",
            timeout=_PDF_TIMEOUT_SECONDS,
            not_found_message=f"PDF not found for DOI: {doi}",
        )

    # Tuple-keyed so this slot is distinct from get_paper's (keyed on the bare
    # canonical id): _fetch calls get_paper, which would otherwise await this
    # very slot's future and deadlock.
    return await _pdf_download.cached_download(
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
