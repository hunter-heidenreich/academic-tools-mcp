"""ACL Anthology client: Anthology-ID identity, collection-XML metadata, PDFs, hosted DOIs."""

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
    """Fresh transient error for an Anthology body that isn't the XML we expect."""
    return http.parse_error_dict(LABEL, detail="could not be parsed as Anthology XML")


# The two registrants whose DOI suffix *is* the Anthology ID. Exported for the reason
# ``doinorm`` exports ``REGISTRANT_PATTERN``: build from them, never respell them.
ACL_DOI_PREFIX = "10.18653/v1/"
ACL_LEGACY_DOI_PREFIX = "10.3115/v1/"
_ID_DOI_PREFIXES = (ACL_DOI_PREFIX, ACL_LEGACY_DOI_PREFIX)

# The authoritative metadata: one XML file per collection (a venue-year), on the
# data repo's default branch. The site is built from these same files.
_COLLECTION_URL = "https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{}.xml"

# Every paper's BibTeX, rebuilt with the site. Only its url/doi pairs are read.
_DOI_INDEX_URL = "https://aclanthology.org/anthology.bib.gz"

# Generous: a PDF, a multi-MB collection file and the dump share the one client.
_TIMEOUT_SECONDS = 60.0

# Two static hosts (the Anthology CDN and raw.githubusercontent.com), neither with a
# documented limit, hence no gap. Past _MAX_PENDING a caller gets backpressure, not a queue.
_MAX_CONCURRENT = 4
_MIN_REQUEST_GAP = 0.0
_MAX_PENDING = 5

# Long, unlike the preprint servers': a camera-ready file is static, so a 404 means
# a wrong ID or a paper not posted yet — neither resolves in minutes.
_NEG_ENTITY = "downloads"
_NEG_TTL_SECONDS = 24 * 60 * 60

# A collection file changes only through corrections.
_POSITIVE_TTL_SECONDS = 7 * 86400.0

# Short: ingest adds a volume's papers to an existing collection file in a batch.
_MISSING_PAPER_TTL_SECONDS = 3600.0

# The dump is rebuilt daily; a week of lag costs only a just-minted opaque DOI.
_DOI_INDEX_MAX_AGE_SECONDS = 7 * 86400.0

# How long a failed index refresh keeps the next caller from paying for another.
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

# Full-matched. Every paper ID the Anthology publishes takes one of the two; the old
# format's letters are exactly those in use, so a label like ``b12-3456`` stays a label.
# Four digits after the hyphen keep volume IDs (``P16-1``) out.
_OLD_FORMAT_ID_RE = re.compile(r"[AC-FH-UWXY]\d{2}-\d{4}", re.IGNORECASE)
_NEW_FORMAT_ID_RE = re.compile(r"\d{4}\.[a-z0-9]+-[a-z0-9]+\.\d+", re.IGNORECASE)

# The site and its predecessor, with a rendering suffix; aclweb.org also nested old
# IDs under letter and collection directories (``anthology/P/P16/P16-1160.pdf``).
_ANTHOLOGY_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:aclanthology\.org|aclweb\.org/anthology)/"
    r"(?:[A-Za-z]/[A-Za-z]\d{2}/)?([^/?#\s]+?)(?:\.pdf|\.bib|\.xml)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)


def _strip_acl_prefix(bare: str) -> str | None:
    """Return the Anthology-ID suffix if ``bare`` carries an ID-bearing prefix, else ``None``.

    Case-insensitive (DOIs are). A *blank* suffix names no paper, so it must fall
    through to the generic-DOI route rather than become an empty Anthology ID.
    """
    for prefix in _ID_DOI_PREFIXES:
        if bare[: len(prefix)].lower() == prefix:
            suffix = bare[len(prefix) :]
            return suffix if suffix.strip() else None
    return None


def is_acl_doi(doi: str) -> bool:
    """Check if a DOI carries an Anthology ID as its suffix."""
    return _strip_acl_prefix(doinorm.normalize(doi)) is not None


def normalize_anthology_id(identifier: str) -> str:
    """The bare Anthology ID an identifier spells, in the Anthology's own case.

    Accepts a bare ID, an aclanthology.org or aclweb.org URL (page, ``.pdf``,
    ``.bib``, ``.xml``) and a ``10.18653/v1/`` or ``10.3115/v1/`` DOI in any
    :mod:`doinorm` spelling. Old-format IDs uppercase (the CDN is case-sensitive:
    ``p16-1160.pdf`` 404s, and Crossref hands them back lowercased); new-format IDs
    lowercase, which is how upstream mints every one. Anything else comes back
    ``doinorm``-normalized. Idempotent.
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
    """Return the canonical cache key — the Anthology ID — for any spelling of a paper."""
    return normalize_anthology_id(identifier)


def _two_digit_volume(collection_id: str, volume_or_rest: str) -> bool:
    """Whether a pre-2020 collection spends two digits on the volume, per upstream's rule."""
    return (
        collection_id.startswith("W")
        or collection_id == "C69"
        or (collection_id == "D19" and int(volume_or_rest[0]) >= 5)
    )


def parse_id(anthology_id: str) -> tuple[str, str, str]:
    """Split a canonical Anthology ID into ``(collection, volume, paper)``.

    Ports ``acl_anthology.utils.ids.parse_id``: ``2023.acl-long.1`` →
    ``("2023.acl", "long", "1")``, ``P16-1160`` → ``("P16", "1", "160")``, and
    ``W04-1013`` → ``("W04", "10", "13")`` — ``W*``, ``C69`` and ``D19`` volumes
    from 5 up take two digits. Leading zeros drop; an emptied paper is ``"0"``.
    Takes only IDs :func:`is_anthology_id` accepts.
    """
    collection_id, _, rest = anthology_id.partition("-")
    if collection_id[:1].isdigit():
        volume_id, _, paper_id = rest.partition(".")
        return collection_id, volume_id, paper_id

    split = 2 if _two_digit_volume(collection_id, rest) else 1
    return collection_id, rest[:split].lstrip("0"), rest[split:].lstrip("0") or "0"


def _build_id(collection_id: str, volume_id: str, paper_id: str) -> str:
    """Inverse of :func:`parse_id`, for naming every paper a collection file holds."""
    if collection_id[:1].isdigit():
        return f"{collection_id}-{volume_id}.{paper_id}"
    if _two_digit_volume(collection_id, volume_id):
        return f"{collection_id}-{int(volume_id):02d}{int(paper_id):02d}"
    return f"{collection_id}-{volume_id}{int(paper_id):03d}"


def pdf_url(anthology_id: str) -> str:
    """Build the direct PDF URL for an Anthology paper.

    ``safe=""``: an Anthology ID has no path structure, so a stray ``/`` is an
    escape, not a separator.
    """
    return f"https://aclanthology.org/{quote(anthology_id, safe='')}.pdf"


def _landing_url(anthology_id: str) -> str:
    return f"https://aclanthology.org/{quote(anthology_id, safe='')}/"


# ---------------------------------------------------------------------------
# Collection XML parsing
# ---------------------------------------------------------------------------


def _text_of(el: ET.Element | None) -> str:
    """An element's text, inline markup flattened and whitespace collapsed; ``""`` if absent.

    Markup is ``<fixed-case>``, ``<i>``, ``<tex-math>`` and the like; ``itertext``
    keeps the words and drops the tags.
    """
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
    """The volume-level fields every paper in it inherits."""
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
    """One normalized record: the paper's own fields over its volume's."""
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
    """The ``<volume>`` elements of a well-formed collection file, ``None`` for a wrong shape.

    A 200 that parses but is not *this* collection — another root, another id, no
    volumes — is an anomaly, not an empty collection: an empty one would
    negative-cache every paper in it.
    """
    if not isinstance(root, ET.Element) or root.tag != "collection":
        return None
    if root.get("id") != collection_id:
        return None
    volumes = root.findall("volume")
    return volumes or None


def _parse_collection(body: bytes, collection_id: str) -> dict[str, dict[str, Any]] | None:
    """Every paper record in a collection file, keyed by Anthology ID; ``None`` for a wrong shape.

    Front matter is paper ``0``. A volume or paper without an ``id`` is skipped,
    not guessed at.
    """
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
    """Fetch, parse and cache one collection file: ``{"records": {...}}`` or an error.

    Writes **one cache entry per paper**, so a reader never deep-copies a whole
    collection. Single-flighted per collection, under a key distinct from
    :func:`get_paper`'s, which awaits this. A 404 is negative-cached per collection.
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
    """Parse a collection and write each paper's entry. Off the event loop: both are heavy."""
    records = _parse_collection(body, collection_id)
    for anthology_id, record in (records or {}).items():
        cache.put(NAMESPACE, "papers", anthology_id, record)
    return records


async def get_paper(identifier: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a paper's record by any spelling of its Anthology ID, using cache when available.

    The record is the Anthology's own: title, abstract, authors and editors
    (``{name, first, last, orcid, affiliation}``), booktitle, volume_type,
    journal_volume, journal_issue, venues, publisher, address, month, year, pages,
    doi, bibkey, url, pdf_url. ``force_refresh=True`` re-fetches the paper's whole
    collection, refreshing every sibling's entry with it.
    """
    anthology_id = canonical_key(identifier)

    async def _fetch() -> dict[str, Any]:
        # Uncached: no request was spent, and the key is not an Anthology ID.
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

# gzip raises OSError (BadGzipFile), EOFError (truncated) or zlib.error; a bad byte
# in the text is a UnicodeDecodeError, a ValueError.
_DUMP_ERRORS = (OSError, EOFError, zlib.error, ValueError)


def _parse_doi_index(body: bytes) -> dict[str, dict[str, str]]:
    """``{registrant: {canonical DOI: Anthology ID}}`` for every DOI the ID grammar can't derive.

    A derivable DOI (``10.18653/v1/…``) is left out: the router already claims it,
    so the index holds only what shape cannot say (~10k rows).
    """
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
    """The index's ``{fetched_at, registrants}`` record, shape-guarded; never expires."""
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
    """Rebuild the index from the dump; the fresh meta record, or ``None`` if it failed.

    **A failed refresh keeps the index it was replacing** — so a hosted DOI's
    identity cannot flip for the length of an outage — and records the failure so
    callers for the next ``_DOI_INDEX_RETRY_SECONDS`` skip straight past it. The
    dump is buffered whole (12.6 MB), then decompressed and parsed off the loop.
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
            # A local refusal says nothing about upstream; the next caller may try at once.
            if not isinstance(e, http.LocalBackpressureError):
                cache.put(NAMESPACE, _INDEX_ENTITY, "last_failure", http.error_dict(LABEL, e))
            return None
        if not shards:
            # An empty dump is an anomaly, not an Anthology with no hosted DOIs.
            cache.put(NAMESPACE, _INDEX_ENTITY, "last_failure", _parse_error_dict())
            return None

        for registrant, rows in shards.items():
            cache.put(NAMESPACE, _INDEX_ENTITY, registrant, rows)
        fresh = {"fetched_at": time.time(), "registrants": sorted(shards)}
        cache.put(NAMESPACE, _INDEX_ENTITY, "meta", fresh)
        return fresh

    return await _single_flight.do(("doi_index",), _runner)


async def anthology_id_for_doi(doi: str) -> str | None:
    """The Anthology ID of a paper the Anthology hosts under an opaque DOI, else ``None``.

    For DOIs whose suffix is not the ID — ``10.1162/tacl…``, ``10.63317/…``, the
    ACM-era ``10.3115/…``. Never an error: a DOI the index cannot place, for any
    reason, proceeds on its existing route. No ``force_refresh``: a caller's refresh
    must not re-download the dump. An index older than its max age is refreshed
    inline, once per age, by whichever caller finds it stale.
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
    """Return the expected cache path for a PDF (may or may not exist yet).

    Raises ``ValueError`` for anything not an Anthology ID rather than returning a
    path: every stem in this namespace is one, so any other answer names a file for
    a paper the Anthology does not own.
    """
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

    # Tuple-keyed: the module's one single-flight also carries papers and collections.
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
