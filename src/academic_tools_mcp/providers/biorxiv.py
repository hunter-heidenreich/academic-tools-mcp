"""bioRxiv/medRxiv client. One API, two servers the shared ``DOI_PREFIX`` can't tell apart."""

import re
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..download import protocol, streaming
from ..net import clients, http
from ..net.throttle import SubGap, Throttle
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


# Cloudflare fronts the content hosts (www.biorxiv.org, www.medrxiv.org) and
# rate-limits (429, error 1015) or challenges (403) at the API's pace.
_CONTENT_REQUEST_GAP = 3.0
_content_gap = SubGap(_throttle, min_gap_seconds=_CONTENT_REQUEST_GAP)


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


def _parse_authors(author_str: Any) -> list[dict[str, str]]:
    """Parse bioRxiv's ``"Last, First; Last, First; ..."`` into structured dicts.

    ``Any``, not ``str``: the value arrives from untyped JSON, where a list reaches
    ``.split`` as an ``AttributeError`` that is in neither ``_PARSE_ERRORS`` nor
    ``HTTPX_ERRORS`` — so it would escape the provider instead of surfacing as an error.
    """
    authors: list[dict[str, str]] = []
    if not isinstance(author_str, str) or not author_str:
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


def _text_or_none(raw: dict[str, Any], key: str) -> str | None:
    """A stripped string field, ``None`` when missing, blank or not a string."""
    value = raw.get(key)
    return value.strip() or None if isinstance(value, str) else None


def _parse_publication(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a raw ``/pubs`` entry into the journal version's DOI, name and date."""
    return {
        "published_doi": _text_or_none(raw, "published_doi"),
        "published_journal": _text_or_none(raw, "published_journal"),
        "published_date": _text_or_none(raw, "published_date"),
    }


def _parse_funding(raw: dict[str, Any]) -> list[dict[str, str | None]]:
    """Funders on a details entry as ``[{name, id, id_type, award}]``; ``[]`` for ``"NA"`` or junk.

    The documented shape is unverified live (funders exist only for papers posted since
    2025-04-10), so either key, and one object or a list, are accepted.
    """
    value = raw.get("funder", raw.get("funding"))
    entries = [value] if isinstance(value, dict) else value if isinstance(value, list) else []
    funders: list[dict[str, str | None]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        funder = {
            "name": _text_or_none(entry, "name"),
            "id": _text_or_none(entry, "id"),
            "id_type": _text_or_none(entry, "id-type") or _text_or_none(entry, "id_type"),
            "award": _text_or_none(entry, "award"),
        }
        if any(funder.values()):
            funders.append(funder)
    return funders


def _pdf_url(server: str, doi: str, version: str) -> str | None:
    """The content host's PDF URL for one revision, or ``None`` when it wouldn't name one.

    The identifier here is an upstream *body* field, not the caller's argument, and this
    URL goes straight to ``stream_to_file`` — so it is encoded and re-checked exactly as
    ``_get_details`` does for the API path, rather than interpolated raw.
    """
    if not doi or "" in doi.split("/"):
        return None
    path = f"{quote(doi, safe='/')}v{quote(version, safe='')}.full.pdf"
    url = f"https://www.{server}.org/content/{path}"
    return url if http.addresses_a_record(url) else None


def _parse_versions(collection: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every revision in a details collection, oldest first, as ``{version, date, license}``."""
    return [
        {
            "version": _text_or_none(entry, "version"),
            "date": _text_or_none(entry, "date"),
            "license": _text_or_none(entry, "license"),
        }
        for entry in sorted(collection, key=_safe_version)
    ]


def _pick_latest_version(collection: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the latest version from a bioRxiv API collection array."""
    return max(collection, key=_safe_version)


def _parse_paper(raw: dict[str, Any], requested_doi: str = "") -> dict[str, Any]:
    """Convert a raw bioRxiv API entry into a normalized paper dict.

    A key present as JSON ``null`` defeats ``.get(k, default)``, so every field read here
    goes through ``_text_or_none``: it folds ``null``, a blank and a wrong type alike, and
    the fields the PDF URL is built from must be strings before they reach it.
    """
    server = "medrxiv" if "medrxiv" in (_text_or_none(raw, "server") or "").lower() else "biorxiv"
    version = _text_or_none(raw, "version") or "1"
    doi = _text_or_none(raw, "doi") or requested_doi

    # bioRxiv writes the literal "NA" for an unpublished preprint, and sometimes "" —
    # both, and a missing key, mean "no journal DOI yet", never a published_doi of "".
    published = _text_or_none(raw, "published") or ""

    return {
        "doi": doi,
        "title": _text_or_none(raw, "title") or "",
        "authors": _parse_authors(raw.get("authors")),
        "author_corresponding": raw.get("author_corresponding"),
        "author_corresponding_institution": raw.get("author_corresponding_institution"),
        "abstract": _text_or_none(raw, "abstract") or "",
        "date": raw.get("date"),
        "version": version,
        "type": raw.get("type"),
        "license": raw.get("license"),
        "category": raw.get("category"),
        "server": server,
        "published_doi": published if published and published != "NA" else None,
        "jatsxml": raw.get("jatsxml"),
        "funding": _parse_funding(raw),
        "pdf_url": _pdf_url(server, doi, version),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_paper(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a paper by bioRxiv/medRxiv DOI, with its journal version's name and date.

    ``published_journal`` / ``published_date`` come from ``get_publication``: ``None``
    when unpublished or that lookup fails. Merged outside the details cache, so a
    ``/pubs`` failure is not kept for the record's TTL.

    ``force_refresh=True`` drops every cache half before fetching — how the agent picks
    up a ``published_doi`` that has just appeared.
    """
    paper = await _get_details(doi, force_refresh=force_refresh)
    if "error" in paper:
        return paper

    venue: dict[str, Any] = {"published_journal": None, "published_date": None}
    if paper.get("published_doi") and paper.get("doi"):
        publication = await get_publication(
            paper["doi"], paper.get("server") or "biorxiv", force_refresh=force_refresh
        )
        if "error" not in publication:
            venue = {key: publication.get(key) for key in venue}
    return {**paper, **venue}


async def get_publication(doi: str, server: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """The journal version bioRxiv links a preprint to: ``{published_doi, published_journal, published_date}``.

    ``server`` is the record's own, as ``/pubs`` is per-server. "No journal version yet"
    is negative-cached on the short TTL.
    """
    bare = _normalize_doi(doi)
    canonical = bare.lower()
    server = "medrxiv" if server == "medrxiv" else "biorxiv"
    not_found_error = f"No published version found for DOI: {doi}"

    async def _fetch() -> dict[str, Any]:
        url = f"{_BASE_URL}/pubs/{server}/{quote(bare, safe='/')}/na/json"
        if "" in bare.split("/") or not http.addresses_a_record(url):
            return http.not_found(not_found_error)

        try:
            response = await _throttled_get(url)
            response.raise_for_status()
            collection = _collection_of(response.json())
            if collection is None:
                return _parse_error_dict()
            if not collection:
                err = http.not_found(not_found_error)
                cache.put_negative(NAMESPACE, "pubs", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
                return err
            publication = _parse_publication(collection[0])
        except _PARSE_ERRORS:
            return _parse_error_dict()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        cache.put(NAMESPACE, "pubs", canonical, publication)
        return publication

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="pubs",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
        sf_key=("pubs", canonical),
    )


# medRxiv ids are 8 digits, bioRxiv's 6 (900 sampled DOIs, 2019-2025). Orders the
# requests only; a miss still needs both servers.
_MEDRXIV_ID_RE = re.compile(r"(?:^|\.)\d{8}$")


def _servers_for(bare: str) -> tuple[str, str]:
    """The two servers in the order to ask them: the likelier one first."""
    suffix = bare.removeprefix(DOI_PREFIX)
    return ("medrxiv", "biorxiv") if _MEDRXIV_ID_RE.search(suffix) else ("biorxiv", "medrxiv")


async def _get_details(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """The ``/details`` record for a bioRxiv/medRxiv DOI, using cache when available.

    Asks the likelier server first, then the other unless the first returned a
    non-empty well-formed collection. Concurrent callers share a fetch.
    """
    bare = _normalize_doi(doi)
    canonical = bare.lower()
    not_found_error = f"No paper found for DOI: {doi}"

    async def _fetch() -> dict[str, Any]:
        # Quoted so a reserved character in the DOI can't split the path.
        path_doi = quote(bare, safe="/")
        first, second = (
            f"{_BASE_URL}/details/{server}/{path_doi}/na/json" for server in _servers_for(bare)
        )

        # The DOI is a *middle* segment, so an empty one needs catching too: both
        # shorten the path to a live route. Uncached — no request was spent.
        if "" in bare.split("/") or not http.addresses_a_record(first):
            return http.not_found(not_found_error)

        try:
            response = await _throttled_get(first)
            response.raise_for_status()
            collection = _collection_of(response.json())

            if not collection:
                # The details API answers an unknown DOI with 200 and an empty
                # collection, never a 404, so the other server always gets a chance.
                # A wrong-shape first response falls through here too: the other may
                # still answer cleanly. (A body that doesn't parse at all never reaches
                # this branch — it raises at `.json()`.)
                response = await _throttled_get(second)
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
            paper["versions"] = _parse_versions(collection)
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


# The only hosts a ``jatsxml`` URL is fetched from.
_JATS_HOSTS = frozenset({"www.biorxiv.org", "www.medrxiv.org"})

# In every JATS article; a 200 without it is a challenge or error page.
_JATS_MARKER = "<article"


async def get_jats(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """bioRxiv's JATS XML full text, from the record's ``jatsxml``, as ``{"markup": text}``.

    A 404 is negative-cached on the short TTL; the markdown made from it is the only
    positive cache.
    """
    canonical = canonical_key(doi)
    if force_refresh:
        cache.invalidate(NAMESPACE, "jats", canonical)
    elif (neg := cache.get_negative(NAMESPACE, "jats", canonical)) is not None:
        return neg

    async def _fetch() -> dict[str, Any]:
        # Threaded through: a stale record's URL names the revision being replaced.
        paper = await get_paper(doi, force_refresh=force_refresh)
        if "error" in paper:
            return paper

        url = paper.get("jatsxml")
        if not isinstance(url, str) or (
            (parts := urlsplit(url)).scheme != "https" or parts.hostname not in _JATS_HOSTS
        ):
            err = http.not_found(f"No JATS full text for DOI: {doi}")
            cache.put_negative(NAMESPACE, "jats", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
            return err

        try:
            # In the try: the gap admits through the throttle, so it can refuse
            # with an ``HTTPX_ERRORS`` member.
            await _content_gap.wait()
            response = await _throttled_get(url, timeout=_PDF_TIMEOUT_SECONDS)
            if response.status_code == 404:
                err = http.not_found(f"No JATS full text for DOI: {doi}")
                cache.put_negative(NAMESPACE, "jats", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
                return err
            response.raise_for_status()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        max_bytes = streaming.resolve_max_pdf_bytes()
        if max_bytes is not None and len(response.content) > max_bytes:
            return {
                "error": f"{LABEL} JATS XML exceeds MAX_PDF_BYTES ({max_bytes} bytes).",
                "retryable": False,
                "max_bytes": max_bytes,
            }
        text = response.text
        if _JATS_MARKER not in text:
            return http.parse_error_dict(LABEL, detail="was not a JATS article")
        return {"markup": text}

    # Tuple-keyed to stay distinct from get_paper's slot, which _fetch awaits.
    return await _single_flight.do(("jats", canonical), _fetch)


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

        # The gap admits through the throttle, so it refuses with an ``HTTPX_ERRORS``
        # member — and ``stream_to_file``'s own try starts too late to catch it.
        try:
            await _content_gap.wait()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

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
    return await protocol.cached_download(
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
