import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from .. import _clients, _http, _pdf_download, _singleflight, cache, config, papers
from .._throttle import Throttle

# Parsing the arXiv Atom feed can fail two ways: a malformed/truncated body
# (``ET.ParseError``) or a hostile entity-expansion payload that defusedxml
# refuses to expand (``DefusedXmlException``). Both are handled alongside the
# HTTP errors so the tool always returns the uniform ``{error}`` contract.
_PARSE_ERRORS = (ET.ParseError, DefusedXmlException)


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable arXiv response.

    A new dict each call (like ``_http.error_dict``) so a caller — or a
    single-flight follower sharing the result — can't mutate a shared object.
    """
    return {
        "error": "arXiv returned a response that could not be parsed as XML.",
        "retryable": True,
    }


ARXIV_BASE_URL = "https://export.arxiv.org/api/query"
NAMESPACE = "arxiv"

# XML namespaces
_ATOM_NS = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"

# Rate limiting: max 1 request per 3 seconds, single connection. arXiv's
# documented "single connection" rule means concurrency=1. Burst cap of 5:
# with a 3s gap, 5 pending = 15s of agent-blocking — past that we back off
# rather than queue forever (the 6th caller gets a structured backpressure
# error). The gating mechanism itself lives in ``_throttle.Throttle``.
_MAX_CONCURRENT = 1
_MIN_REQUEST_GAP = 3.0
_MAX_PENDING = 5

# Coalesces concurrent calls for the same canonical paper ID into one
# fetch. Without this, 4 parallel unified-paper tools (metadata, authors,
# abstract, bibtex) for one arXiv ID would each hit the network.
_single_flight = _singleflight.SingleFlight()

# Shorter than the cache.py default 24h. arXiv IDs go live mid-session
# (a paper just announced an hour ago) and an agent that 404'd at 9am
# should surface the new entry by 10am, not tomorrow at 9am.
_NEG_TTL_SECONDS = 3600.0

# Positive cache TTL. arXiv records are stable per-version, but our
# canonical key strips the version suffix, so v1 cached today wouldn't
# reflect a v2 uploaded next week. 14 days is long enough that an active
# session keeps hitting cache and short enough that revisions surface.
_POSITIVE_TTL_SECONDS = 14 * 86400.0


def _build_headers() -> dict[str, str]:
    """Build request headers with a descriptive User-Agent.

    Unlike the polite-pool opt-in providers (crossref/openalex, which only
    set a User-Agent when a mailto is configured), arXiv is sent a
    descriptive User-Agent *unconditionally*: its Fastly edge throttles
    generic library User-Agents (e.g. ``python-httpx/x.y``) far more
    aggressively, returning 429/503 on modest bursts. ``ARXIV_MAILTO``, when
    set, is appended as a contact so arXiv can reach the operator before
    tightening limits.
    """
    user_agent = "academic-tools-mcp/1.0 (https://github.com/academic-tools-mcp"
    mailto = config.get("ARXIV_MAILTO")
    if mailto:
        user_agent += f"; mailto:{mailto}"
    user_agent += ")"
    return {"User-Agent": user_agent}


def _get_client() -> httpx.AsyncClient:
    """Return the persistent AsyncClient for arXiv calls.

    The descriptive User-Agent is baked in at construction so every call
    (metadata, search, PDF download) identifies the client and avoids
    arXiv's heavier throttling of anonymous library traffic.
    """
    return _clients.get_client(NAMESPACE, headers=_build_headers(), timeout=30.0)


_throttle = Throttle(
    namespace=NAMESPACE,
    label="arXiv",
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
    # arXiv's Fastly edge returns 429/503 with no (or zero) Retry-After when an
    # IP is briefly penalty-boxed; a single retry often lands in the same
    # window. Two retries (three attempts total) let get_with_retry's
    # exponential backoff (≈3s then ≈6s) ride out a short cooldown instead of
    # surfacing the error to the agent.
    retry_attempts=3,
)


def _request_slot(url: str):
    """arXiv's rate-limit slot (see ``Throttle.slot``).

    Kept as a module-level wrapper so the streaming PDF download's
    ``slot_factory`` lambda and the test seam
    (``monkeypatch.setattr(arxiv, "_request_slot", ...)``) keep resolving
    a module attribute.
    """
    return _throttle.slot(url)


async def _throttled_get(client: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
    """Execute a GET respecting arXiv's rate limit (see ``Throttle.get``)."""
    return await _throttle.get(client, url, **kwargs)


# ---------------------------------------------------------------------------
# ID normalization
# ---------------------------------------------------------------------------

# The ID stops at the first ``?`` or ``#`` so a query string / fragment
# (e.g. ``/abs/2301.00001?context=cs``) doesn't end up baked into the
# canonical cache key.
_ARXIV_URL_RE = re.compile(r"https?://arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:\.pdf)?(?:[?#].*)?$")


def _normalize_arxiv_id(arxiv_id: str) -> str:
    """Normalize an arXiv identifier to a bare ID (with version if present).

    Accepts:
      - bare ID: 2301.00001, 2301.00001v2, hep-th/9901001
      - abstract URL: https://arxiv.org/abs/2301.00001v2
      - PDF URL: https://arxiv.org/pdf/2301.00001v2.pdf
      - PDF URL without extension: https://arxiv.org/pdf/2301.00001v2
    """
    arxiv_id = arxiv_id.strip()
    m = _ARXIV_URL_RE.match(arxiv_id)
    if m:
        return m.group(1)
    return arxiv_id


def canonical_arxiv_id(arxiv_id: str) -> str:
    """Return a canonical arXiv ID for cache keying, **version included**.

    The version is part of a paper's identity, so it is part of the key. A
    bare ``2301.00001`` means "whatever is current" and keys on the bare
    form; an explicit ``2301.00001v2`` means that revision and keys on
    ``2301.00001v2``.

    This used to strip the version so v1/v2/latest shared one entry, which
    silently served the wrong paper: the key was stripped but the *fetch*
    (``_normalize_arxiv_id``) kept the version, so whichever version was
    requested first won the shared key and every later version was a cache
    hit returning the earlier one's title, abstract, and authors —
    and ``download_pdf`` then handed back the earlier one's bytes marked
    ``cached: True``. ``force_refresh`` could not help, because it
    invalidated that same shared key.

    Only lowercasing is applied (old-style IDs like ``math.GT/0309136``
    vary in case upstream); the API request itself keeps the original case.
    """
    return _normalize_arxiv_id(arxiv_id).lower()


def base_arxiv_id(arxiv_id: str) -> str:
    """Return the version-stripped ("latest") form of an arXiv ID.

    This is the key a bare request uses. Kept separate from
    ``canonical_arxiv_id`` so callers must say which they mean.
    """
    return re.sub(r"v\d+$", "", canonical_arxiv_id(arxiv_id))


# ---------------------------------------------------------------------------
# XML parsing
# ---------------------------------------------------------------------------


def _parse_entry(entry: ET.Element) -> dict[str, Any]:
    """Parse a single Atom <entry> element into a dict."""

    def _text(tag: str, ns: str = _ATOM_NS) -> str | None:
        el = entry.find(f"{{{ns}}}{tag}")
        if el is not None and el.text:
            return el.text.strip()
        return None

    # Authors with optional affiliations
    authors = []
    for author_el in entry.findall(f"{{{_ATOM_NS}}}author"):
        name_el = author_el.find(f"{{{_ATOM_NS}}}name")
        name = name_el.text.strip() if name_el is not None and name_el.text else ""
        affiliations = [
            aff.text.strip() for aff in author_el.findall(f"{{{_ARXIV_NS}}}affiliation") if aff.text
        ]
        authors.append({"name": name, "affiliations": affiliations})

    # Links
    links = []
    for link_el in entry.findall(f"{{{_ATOM_NS}}}link"):
        links.append(
            {
                "href": link_el.get("href", ""),
                "rel": link_el.get("rel", ""),
                "title": link_el.get("title") or None,
            }
        )

    # Categories
    categories = [
        cat.get("term", "") for cat in entry.findall(f"{{{_ATOM_NS}}}category") if cat.get("term")
    ]

    # Primary category
    primary_cat_el = entry.find(f"{{{_ARXIV_NS}}}primary_category")
    primary_category = primary_cat_el.get("term", "") if primary_cat_el is not None else ""

    # ID (URL form: http://arxiv.org/abs/2301.00001v1)
    raw_id = _text("id") or ""

    # Title and summary: collapse embedded whitespace/newlines
    raw_title = _text("title") or ""
    raw_summary = _text("summary") or ""

    return {
        "id": raw_id,
        "title": " ".join(raw_title.split()),
        "summary": " ".join(raw_summary.split()),
        "published": _text("published") or "",
        "updated": _text("updated") or "",
        "authors": authors,
        "categories": categories,
        "primary_category": primary_category,
        "links": links,
        "comment": _text("comment", _ARXIV_NS),
        "journal_ref": _text("journal_ref", _ARXIV_NS),
        "doi": _text("doi", _ARXIV_NS),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_paper(arxiv_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a paper by arXiv ID, using cache when available.

    Returns a parsed dict with paper metadata. Concurrent callers for
    the same ID share one fetch via single-flight — without this, four
    unified-paper tools (metadata, authors, abstract, bibtex) called in
    parallel would all hit arXiv and burn ~12s of throttle gap between
    them for a paper that ends up in cache after the first call.

    ``force_refresh=True`` drops both positive and negative cache entries
    for this canonical ID before fetching, so an agent can re-pull a
    paper whose cached entry might be stale (e.g. a new version uploaded).
    """
    canonical = canonical_arxiv_id(arxiv_id)

    async def _fetch() -> dict[str, Any]:
        api_id = _normalize_arxiv_id(arxiv_id)

        try:
            client = _get_client()
            response = await _throttled_get(
                client,
                ARXIV_BASE_URL,
                params={"id_list": api_id},
            )

            response.raise_for_status()

            root = _safe_fromstring(response.text)
        except _PARSE_ERRORS:
            # A 200 body we couldn't parse: a truncated/garbled response,
            # or a hostile entity-expansion payload defusedxml refused.
            # Transient (or adversarial), not a definitive "not found" —
            # surface a retryable error and do NOT negative-cache it, so a
            # retry re-fetches rather than serving a poisoned entry.
            return _parse_error_dict()
        except _http.HTTPX_ERRORS as e:
            err = _http.error_dict("arXiv", e)
            # A genuine HTTP 404 is a definitive "not found" — negative-cache
            # it (same as arXiv's 200-with-error-entry shape) so a retrying
            # agent doesn't re-hit the network every call. Transient failures
            # (5xx / timeout / 429 / backpressure) must NOT be cached.
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
                cache.put_negative(
                    NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS
                )
            return err

        entries = root.findall(f"{{{_ATOM_NS}}}entry")

        if not entries:
            err = {"error": f"No paper found for arXiv ID: {arxiv_id}"}
            cache.put_negative(NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
            return err

        # arXiv returns HTTP 200 with an error entry for invalid IDs.
        # Cache it the same way as a real 404 — both mean "definitively
        # not found", which is what negative caching is for.
        entry = entries[0]
        id_el = entry.find(f"{{{_ATOM_NS}}}id")
        if id_el is not None and id_el.text and "api/errors" in id_el.text:
            err = {"error": f"No paper found for arXiv ID: {arxiv_id}"}
            cache.put_negative(NAMESPACE, "papers", canonical, err, ttl_seconds=_NEG_TTL_SECONDS)
            return err

        data = _parse_entry(entry)
        cache.put(NAMESPACE, "papers", canonical, data)
        return data

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="papers",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
    )


async def search_papers(
    query: str,
    max_results: int = 10,
) -> dict[str, Any]:
    """Search arXiv papers by query string.

    The query supports field prefixes: ti:, au:, abs:, cat:, etc.
    Boolean operators: AND, OR, ANDNOT.
    Returns a dict with total_results and a list of parsed entries.
    """
    capped = min(max(max_results, 1), 50)

    try:
        client = _get_client()
        response = await _throttled_get(
            client,
            ARXIV_BASE_URL,
            params={
                "search_query": query,
                "start": "0",
                "max_results": str(capped),
            },
        )

        response.raise_for_status()

        # Parse and extract inside the guarded block: a truncated/garbled
        # 200 body (ParseError), a hostile entity payload (defusedxml), or a
        # non-numeric totalResults must surface a structured error, not raise.
        root = _safe_fromstring(response.text)
        total_el = root.find(f"{{{_OPENSEARCH_NS}}}totalResults")
        total_text = total_el.text.strip() if total_el is not None and total_el.text else ""
        total_results = int(total_text) if total_text.isdigit() else 0
        entries = root.findall(f"{{{_ATOM_NS}}}entry")
        papers = [_parse_entry(e) for e in entries]
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("arXiv", e)

    # Opportunistically cache individual papers. Refresh stale entries too:
    # cache.has ignores the TTL, so a TTL-aware get() ensures fresher search
    # data replaces an entry that's already past the positive TTL.
    for paper in papers:
        raw_id = paper.get("id", "")
        if "/abs/" in raw_id:
            paper_id = raw_id.split("/abs/")[-1]
            # Search always returns the current version, so this entry is
            # valid under *both* the versioned key (that exact revision) and
            # the bare key (whatever is current). Warming only the versioned
            # one would leave every bare lookup a miss.
            keys = {canonical_arxiv_id(paper_id), base_arxiv_id(paper_id)}
            for paper_canonical in keys:
                if (
                    cache.get(
                        NAMESPACE, "papers", paper_canonical, max_age_seconds=_POSITIVE_TTL_SECONDS
                    )
                    is None
                ):
                    cache.put(NAMESPACE, "papers", paper_canonical, paper)

    return {
        "total_results": total_results,
        "entries": papers,
    }


def _pdf_filename(canonical: str) -> str:
    """Build a PDF filename from a canonical arXiv ID.

    Routes through ``papers.safe_stem`` — the single sanitizer shared by the
    PDF, markdown, and sections paths — so this provider can't disagree with
    the others about which file belongs to which paper. Real arXiv ids are
    ``[A-Za-z0-9./-]`` only, so they map to the same name as before.
    """
    return papers.safe_stem(canonical) + ".pdf"


def pdf_path(arxiv_id: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet)."""
    canonical = canonical_arxiv_id(arxiv_id)
    return cache.cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)


async def download_pdf(arxiv_id: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download the PDF for an arXiv paper and cache it locally.

    ``force_refresh=True`` re-downloads and atomically replaces the
    cached PDF. Use when you suspect the cached file is corrupt or arXiv
    replaced the PDF (a v2 upload that landed under the same canonical
    key). The existing cached file is kept if the re-download fails, so a
    flaky network can't leave you worse off than before.

    Streams the response to a temp file in chunks (peak memory = one
    chunk, not the whole PDF) and renames into place atomically. The
    download aborts mid-stream if it would exceed ``MAX_PDF_BYTES`` so a
    misrouted URL can't fill the disk.

    Returns a dict with the file path and size, or an error. Concurrent
    callers for the same ID share one download via single-flight.
    """
    canonical = canonical_arxiv_id(arxiv_id)
    dest = cache.cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)

    if not force_refresh and (hit := _pdf_download.cached_hit(dest)) is not None:
        return hit

    async def _fetch() -> dict[str, Any]:
        # Re-check after winning the slot — a concurrent leader may have
        # just written the file. Skip the short-circuit under force_refresh:
        # the caller wants fresh bytes, and stream_to_file replaces dest
        # atomically on success.
        if not force_refresh and (hit := _pdf_download.cached_hit(dest)) is not None:
            return hit

        # Need the paper metadata to find the PDF URL
        paper = await get_paper(arxiv_id)
        if "error" in paper:
            return paper

        pdf_url = None
        for link in paper.get("links", []):
            if link.get("title") == "pdf":
                pdf_url = link["href"]
                break

        if not pdf_url:
            return {"error": f"No PDF link found for arXiv ID: {arxiv_id}"}

        client = _get_client()
        return await _pdf_download.stream_to_file(
            client,
            pdf_url,
            dest,
            slot_factory=lambda: _request_slot(pdf_url),
            provider_label="arXiv",
            timeout=60.0,
            not_found_message=f"No PDF found for arXiv ID: {arxiv_id}",
        )

    # Tuple-keyed so this slot is distinct from get_paper's (keyed on the
    # bare canonical id): _fetch calls get_paper, which would otherwise
    # await this very slot's future and deadlock.
    return await _single_flight.do(("pdf", canonical), _fetch)
