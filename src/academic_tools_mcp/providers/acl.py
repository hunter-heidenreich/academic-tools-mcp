"""ACL Anthology client: Anthology IDs, collection-XML metadata, PDFs, hosted DOIs."""

import asyncio
import gzip
import re
import time
import xml.etree.ElementTree as ET
import zlib
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from ..download import streaming
from ..net import clients, http
from ..net.throttle import Throttle
from ..store import cache, singleflight, stems
from ..util import doinorm, useragent

# Not "acl": this is the cache *directory* name, so renaming it needs a sweep.
NAMESPACE = "acl_anthology"

# Agent-facing provider name; every site that names us reads it.
LABEL = "ACL Anthology"

# Both are transient, not "not found".
_PARSE_ERRORS = (ET.ParseError, DefusedXmlException)


def _parse_error_dict() -> dict[str, Any]:
    """Fresh transient error for an unparseable Anthology body."""
    return http.parse_error_dict(LABEL, detail="could not be parsed as Anthology XML")


# DOI prefixes whose suffix is the Anthology ID.
ACL_DOI_PREFIX = "10.18653/v1/"
ACL_LEGACY_DOI_PREFIX = "10.3115/v1/"
_ID_DOI_PREFIXES = (ACL_DOI_PREFIX, ACL_LEGACY_DOI_PREFIX)

# One authoritative XML file per collection (a venue-year).
_COLLECTION_URL = "https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{}.xml"

# Every paper's BibTeX; only the url/doi pairs are read.
_DOI_INDEX_URL = "https://aclanthology.org/anthology.bib.gz"

# Generous: PDFs, collection files and the dump share one client.
_TIMEOUT_SECONDS = 60.0

# Two static hosts, neither with a documented limit, hence no gap.
_MAX_CONCURRENT = 4
_MIN_REQUEST_GAP = 0.0
_MAX_PENDING = 5

# Long: a camera-ready file is static, so a 404 won't resolve in minutes.
_NEG_ENTITY = "downloads"
_NEG_TTL_SECONDS = 24 * 60 * 60

_POSITIVE_TTL_SECONDS = 7 * 86400.0

# Short: ingest can add a volume to an existing collection file.
_MISSING_PAPER_TTL_SECONDS = 3600.0

_DOI_INDEX_MAX_AGE_SECONDS = 7 * 86400.0
_DOI_INDEX_RETRY_SECONDS = 3600.0

_INDEX_ENTITY = "doi_index"

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
    return clients.get_client(NAMESPACE, headers=useragent.headers(), timeout=_TIMEOUT_SECONDS)


def _request_slot(url: str) -> AbstractAsyncContextManager[None]:
    """ACL Anthology's rate-limit slot (``Throttle.slot``). Module-level: it is a test seam."""
    return _throttle.slot(url)


async def _throttled_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at the Anthology's rate. Url-only: ``_get_client`` is the only place to configure it."""
    return await _throttle.get(_get_client(), url, **kwargs)


# ---------------------------------------------------------------------------
# Anthology ID identity
# ---------------------------------------------------------------------------

# Full-matched against every published ID: only letters in use, and four digits so
# volume IDs (``P16-1``) stay out.
_OLD_FORMAT_ID_RE = re.compile(r"[AC-FH-UWXY]\d{2}-\d{4}", re.IGNORECASE)
_NEW_FORMAT_ID_RE = re.compile(r"\d{4}\.[a-z0-9]+-[a-z0-9]+\.\d+", re.IGNORECASE)

# The site and aclweb.org, which also nested IDs (``anthology/P/P16/P16-1160.pdf``).
_ANTHOLOGY_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:aclanthology\.org|aclweb\.org/anthology)/"
    r"(?:[A-Za-z]/[A-Za-z]\d{2}/)?([^/?#\s]+?)(?:\.pdf|\.bib|\.xml)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)


def _strip_acl_prefix(bare: str) -> str | None:
    """The suffix after an ID-bearing DOI prefix, or ``None``; a blank suffix is ``None``."""
    for prefix in _ID_DOI_PREFIXES:
        if bare[: len(prefix)].lower() == prefix:
            suffix = bare[len(prefix) :]
            return suffix if suffix.strip() else None
    return None


def is_acl_doi(doi: str) -> bool:
    """Check if a DOI carries an Anthology ID as its suffix."""
    return _strip_acl_prefix(doinorm.normalize(doi)) is not None


def normalize_anthology_id(identifier: str) -> str:
    """The Anthology ID an ID, URL or ID-bearing DOI spells; anything else ``doinorm``-normalized.

    Old-format IDs uppercase (the CDN is case-sensitive), new-format lowercase. Idempotent.
    """
    bare = doinorm.normalize(identifier)
    if m := _ANTHOLOGY_URL_RE.match(bare):
        bare = m.group(1)
    elif (suffix := _strip_acl_prefix(bare)) is not None:
        bare = suffix

    if _OLD_FORMAT_ID_RE.fullmatch(bare):
        return bare.upper()
    if _NEW_FORMAT_ID_RE.fullmatch(bare):
        return bare.lower()
    return bare


def is_anthology_id(identifier: str) -> bool:
    """Check if an identifier spells an ACL Anthology paper."""
    normalized = normalize_anthology_id(identifier)
    return bool(_OLD_FORMAT_ID_RE.fullmatch(normalized) or _NEW_FORMAT_ID_RE.fullmatch(normalized))


def canonical_key(identifier: str) -> str:
    """The cache key: the Anthology ID."""
    return normalize_anthology_id(identifier)


def _two_digit_volume(collection_id: str, volume_or_rest: str) -> bool:
    """Upstream's rule for which pre-2020 collections take a two-digit volume."""
    return (
        collection_id.startswith("W")
        or collection_id == "C69"
        or (collection_id == "D19" and int(volume_or_rest[0]) >= 5)
    )


def parse_id(anthology_id: str) -> tuple[str, str, str]:
    """``(collection, volume, paper)``, porting ``acl_anthology.utils.ids.parse_id``.

    ``P16-1160`` → ``("P16", "1", "160")``; ``W04-1013`` → ``("W04", "10", "13")``.
    """
    collection_id, _, rest = anthology_id.partition("-")
    if collection_id[:1].isdigit():
        volume_id, _, paper_id = rest.partition(".")
        return collection_id, volume_id, paper_id

    split = 2 if _two_digit_volume(collection_id, rest) else 1
    return collection_id, rest[:split].lstrip("0"), rest[split:].lstrip("0") or "0"


def _build_id(collection_id: str, volume_id: str, paper_id: str) -> str:
    """Inverse of :func:`parse_id`."""
    if collection_id[:1].isdigit():
        return f"{collection_id}-{volume_id}.{paper_id}"
    if _two_digit_volume(collection_id, volume_id):
        return f"{collection_id}-{int(volume_id):02d}{int(paper_id):02d}"
    return f"{collection_id}-{volume_id}{int(paper_id):03d}"


def pdf_url(anthology_id: str) -> str:
    """Build the direct PDF URL for an Anthology paper. ``safe=""``: an ID has no path."""
    return f"https://aclanthology.org/{quote(anthology_id, safe='')}.pdf"


def _landing_url(anthology_id: str) -> str:
    return f"https://aclanthology.org/{quote(anthology_id, safe='')}/"


# ---------------------------------------------------------------------------
# Collection XML parsing
# ---------------------------------------------------------------------------


def _text_of(el: ET.Element | None) -> str:
    """An element's text with inline markup flattened and whitespace collapsed."""
    if el is None:
        return ""
    return " ".join("".join(el.itertext()).split())


def _parse_person(el: ET.Element) -> dict[str, Any]:
    first = _text_of(el.find("first"))
    last = _text_of(el.find("last"))
    return {
        "name": " ".join(part for part in (first, last) if part),
        "first": first or None,
        "last": last or None,
        "orcid": el.get("orcid") or None,
        "affiliation": _text_of(el.find("affiliation")) or None,
    }


def _parse_volume_meta(volume: ET.Element) -> dict[str, Any]:
    """Fields every paper in the volume inherits."""
    meta = volume.find("meta")
    if meta is None:
        meta = ET.Element("meta")
    return {
        "booktitle": _text_of(meta.find("booktitle")) or None,
        "editors": [_parse_person(e) for e in meta.findall("editor")],
        "volume_type": volume.get("type") or None,
        "journal_volume": _text_of(meta.find("journal-volume")) or None,
        "journal_issue": _text_of(meta.find("journal-issue")) or None,
        "venues": [v for v in (_text_of(e) for e in meta.findall("venue")) if v],
        "publisher": _text_of(meta.find("publisher")) or None,
        "address": _text_of(meta.find("address")) or None,
        "month": _text_of(meta.find("month")) or None,
        "year": _text_of(meta.find("year")) or None,
    }


def _parse_paper(paper: ET.Element, meta: dict[str, Any], anthology_id: str) -> dict[str, Any]:

    return {
        "anthology_id": anthology_id,
        "title": _text_of(paper.find("title")),
        "authors": [_parse_person(a) for a in paper.findall("author")],
        "abstract": _text_of(paper.find("abstract")) or None,
        **meta,
        "pages": _text_of(paper.find("pages")) or None,
        "doi": _text_of(paper.find("doi")) or None,
        "bibkey": _text_of(paper.find("bibkey")) or None,
        "url": _landing_url(anthology_id),
        "pdf_url": pdf_url(anthology_id),
    }


def _collection_of(root: Any, collection_id: str) -> list[ET.Element] | None:
    """The collection's ``<volume>`` elements; ``None`` for a wrong shape, never ``[]``.

    Reading an anomaly as empty would negative-cache every paper in it.
    """
    if not isinstance(root, ET.Element) or root.tag != "collection":
        return None
    if root.get("id") != collection_id:
        return None
    volumes = root.findall("volume")
    return volumes or None


def _parse_collection(body: bytes, collection_id: str) -> dict[str, dict[str, Any]] | None:
    """Every paper record keyed by Anthology ID (front matter is paper ``0``), or ``None``."""
    volumes = _collection_of(_safe_fromstring(body), collection_id)
    if volumes is None:
        return None

    records: dict[str, dict[str, Any]] = {}
    for volume in volumes:
        volume_id = volume.get("id")
        if not volume_id:
            continue
        meta = _parse_volume_meta(volume)
        for paper in volume:
            paper_id = "0" if paper.tag == "frontmatter" else paper.get("id") or ""
            if paper.tag not in ("paper", "frontmatter") or not paper_id:
                continue
            try:
                anthology_id = _build_id(collection_id, volume_id, paper_id)
            except ValueError:
                continue
            records[anthology_id] = _parse_paper(paper, meta, anthology_id)
    return records


# ---------------------------------------------------------------------------
# Public API: metadata
# ---------------------------------------------------------------------------


async def _fetch_collection(
    collection_id: str, *, force_refresh: bool
) -> dict[str, dict[str, Any]] | dict[str, Any]:
    """Fetch a collection and cache each paper separately: ``{"records": ...}`` or an error.

    Per paper, so a read never deep-copies a whole collection.
    """
    url = _COLLECTION_URL.format(quote(collection_id, safe=""))

    async def _runner() -> dict[str, Any]:
        if force_refresh:
            cache.invalidate(NAMESPACE, "collections", collection_id)
        elif (negative := cache.get_negative(NAMESPACE, "collections", collection_id)) is not None:
            return negative

        if not http.addresses_a_record(url):
            return http.not_found(f"No ACL Anthology collection: {collection_id}")

        try:
            response = await _throttled_get(url)
            if response.status_code == 404:
                err = http.not_found(f"No ACL Anthology collection: {collection_id}")
                cache.put_negative(
                    NAMESPACE, "collections", collection_id, err, ttl_seconds=_NEG_TTL_SECONDS
                )
                return err
            response.raise_for_status()
            records = await asyncio.to_thread(_parse_and_store, response.content, collection_id)
        except _PARSE_ERRORS:
            return _parse_error_dict()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        if records is None:
            return _parse_error_dict()
        return {"records": records}

    return await _single_flight.do(("collection", collection_id), _runner)


def _parse_and_store(body: bytes, collection_id: str) -> dict[str, dict[str, Any]] | None:
    """Parse and write each paper's entry; run off the event loop."""
    records = _parse_collection(body, collection_id)
    for anthology_id, record in (records or {}).items():
        cache.put(NAMESPACE, "papers", anthology_id, record)
    return records


async def get_paper(identifier: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a paper's record by any spelling of its Anthology ID, using cache when available.

    ``force_refresh=True`` re-fetches the whole collection.
    """
    anthology_id = canonical_key(identifier)

    async def _fetch() -> dict[str, Any]:
        if not is_anthology_id(anthology_id):
            return http.not_found(f"Not an ACL Anthology ID: {identifier}")

        collection_id, _, _ = parse_id(anthology_id)
        fetched = await _fetch_collection(collection_id, force_refresh=force_refresh)
        if "error" in fetched:
            return fetched

        record = fetched["records"].get(anthology_id)
        if record is None:
            err = http.not_found(f"No ACL Anthology paper: {anthology_id}")
            cache.put_negative(
                NAMESPACE, "papers", anthology_id, err, ttl_seconds=_MISSING_PAPER_TTL_SECONDS
            )
            return err
        return record

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="papers",
        canonical=anthology_id,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=("paper", anthology_id),
    )


# ---------------------------------------------------------------------------
# Hosted-DOI index
# ---------------------------------------------------------------------------

_BIB_URL_RE = re.compile(r'^\s*url = "https://aclanthology\.org/([^/"]+)/"', re.MULTILINE)
_BIB_DOI_RE = re.compile(r'^\s*doi = "([^"]+)"', re.MULTILINE)

# gzip: OSError, EOFError, zlib.error; bad UTF-8: ValueError.
_DUMP_ERRORS = (OSError, EOFError, zlib.error, ValueError)


def _parse_doi_index(body: bytes) -> dict[str, dict[str, str]]:
    """``{registrant: {DOI: Anthology ID}}`` for DOIs the router can't already derive."""
    shards: dict[str, dict[str, str]] = {}
    for entry in gzip.decompress(body).decode("utf-8").split("\n@"):
        url = _BIB_URL_RE.search(entry)
        doi = _BIB_DOI_RE.search(entry)
        if url is None or doi is None:
            continue
        canonical = doinorm.canonical(doi.group(1))
        if not doinorm.looks_like_doi(canonical) or is_anthology_id(canonical):
            continue
        if not is_anthology_id(url.group(1)):
            continue
        registrant = canonical.split("/", 1)[0]
        shards.setdefault(registrant, {})[canonical] = canonical_key(url.group(1))
    return shards


def _index_meta() -> dict[str, Any] | None:
    """The index's ``{fetched_at, registrants}`` record, shape-guarded."""
    meta = cache.get(NAMESPACE, _INDEX_ENTITY, "meta", count=False)
    if meta is None:
        return None
    if not isinstance(meta.get("fetched_at"), (int, float)):
        return None
    if not isinstance(meta.get("registrants"), list):
        return None
    return meta


def _is_stale(meta: dict[str, Any] | None) -> bool:
    return meta is None or time.time() - meta["fetched_at"] > _DOI_INDEX_MAX_AGE_SECONDS


async def _refresh_doi_index() -> dict[str, Any] | None:
    """Rebuild the index from the dump; the new meta record, or ``None`` on failure.

    A failure keeps the old index and is recorded, so callers skip retrying for a while.
    """

    async def _runner() -> dict[str, Any] | None:
        meta = _index_meta()
        if not _is_stale(meta):
            return meta
        failed = cache.get(
            NAMESPACE, _INDEX_ENTITY, "last_failure", max_age_seconds=_DOI_INDEX_RETRY_SECONDS
        )
        if failed is not None:
            return None

        try:
            response = await _throttled_get(_DOI_INDEX_URL)
            response.raise_for_status()
            shards = await asyncio.to_thread(_parse_doi_index, response.content)
        except _DUMP_ERRORS:
            cache.put(NAMESPACE, _INDEX_ENTITY, "last_failure", _parse_error_dict())
            return None
        except http.HTTPX_ERRORS as e:
            # Local backpressure says nothing about upstream.
            if not isinstance(e, http.LocalBackpressureError):
                cache.put(NAMESPACE, _INDEX_ENTITY, "last_failure", http.error_dict(LABEL, e))
            return None
        if not shards:
            cache.put(NAMESPACE, _INDEX_ENTITY, "last_failure", _parse_error_dict())
            return None

        for registrant, rows in shards.items():
            cache.put(NAMESPACE, _INDEX_ENTITY, registrant, rows)
        fresh = {"fetched_at": time.time(), "registrants": sorted(shards)}
        cache.put(NAMESPACE, _INDEX_ENTITY, "meta", fresh)
        return fresh

    return await _single_flight.do(("doi_index",), _runner)


async def anthology_id_for_doi(doi: str) -> str | None:
    """The Anthology ID for a hosted DOI whose suffix isn't the ID (``10.1162/…``), else ``None``.

    Never errors. No ``force_refresh``: that would re-download the dump. A stale
    index is refreshed inline.
    """
    canonical = doinorm.canonical(doi)
    if not doinorm.looks_like_doi(canonical):
        return None

    meta = _index_meta()
    if _is_stale(meta):
        meta = await _refresh_doi_index() or meta
    registrant = canonical.split("/", 1)[0]
    if meta is None or registrant not in meta["registrants"]:
        return None

    shard = cache.get(NAMESPACE, _INDEX_ENTITY, registrant, count=False) or {}
    found = shard.get(canonical)
    return canonical_key(found) if isinstance(found, str) and is_anthology_id(found) else None


# ---------------------------------------------------------------------------
# PDF download
# ---------------------------------------------------------------------------


def pdf_path(identifier: str) -> Path:
    """Return the expected cache path for a PDF. Raises ``ValueError`` for a non-Anthology ID."""
    if not is_anthology_id(identifier):
        raise ValueError(f"Not an ACL Anthology ID: {identifier}")
    return stems.pdf_path(NAMESPACE, canonical_key(identifier))


async def download_pdf(identifier: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download the PDF for an ACL Anthology paper and cache it locally.

    Returns the file path, size and ACL provenance (``anthology_id``, ``pdf_url``) on a
    cached hit and a fresh download alike, or an error. ``force_refresh=True``
    re-downloads and atomically replaces the cached PDF — the Anthology occasionally
    re-issues a camera-ready at the same URL — keeping the existing file if the
    re-download fails.
    """
    if not is_anthology_id(identifier):
        # Definitive: unflagged, every classifier downstream reads it as unknown.
        return http.not_found(f"Not an ACL Anthology ID: {identifier}")

    aid = canonical_key(identifier)
    dest = stems.pdf_path(NAMESPACE, aid)
    url = pdf_url(aid)

    async def _fetch() -> dict[str, Any]:
        return await streaming.stream_to_file(
            _get_client(),
            url,
            dest,
            slot_factory=lambda: _request_slot(url),
            namespace=NAMESPACE,
            provider_label=LABEL,
            timeout=_TIMEOUT_SECONDS,
            not_found_message=f"PDF not found on ACL Anthology for: {aid}",
        )

    # Tuple-keyed: the single-flight also carries papers and collections.
    return await streaming.cached_download(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity=_NEG_ENTITY,
        canonical=aid,
        dest=dest,
        fetch=_fetch,
        neg_ttl=_NEG_TTL_SECONDS,
        force_refresh=force_refresh,
        extra_fields={"anthology_id": aid, "pdf_url": url},
        sf_key=("pdf", aid),
    )
