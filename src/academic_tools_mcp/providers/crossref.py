"""Crossref client. Title search and metadata, with a tighter pace for search."""

import html
import re
from typing import Any
from urllib.parse import quote

import httpx

from ..net import clients, http
from ..net.throttle import SubGap, Throttle
from ..store import cache, singleflight
from ..util import config, doinorm, useragent

CROSSREF_BASE_URL = "https://api.crossref.org/v1"
NAMESPACE = "crossref"

# Agent-facing provider name; every site that names us reads it.
LABEL = "Crossref"

_PARSE_ERRORS = http.JSON_PARSE_ERRORS


def _parse_error_dict() -> dict[str, Any]:
    """Fresh structured error for an unparseable Crossref response."""
    return http.parse_error_dict(LABEL)


# A ceiling `_observe_pool` promotes to, never a starting point: the rate we take
# must follow the identity Crossref *confirms* it received.
_POLITE_MAX_CONCURRENT = 3
_POLITE_REQUEST_GAP = 0.1  # 100ms -> 10 req/sec
_POLITE_SEARCH_GAP = 0.334  # ~3 req/sec

_PUBLIC_MAX_CONCURRENT = 1
_PUBLIC_REQUEST_GAP = 0.2  # 200ms -> 5 req/sec
_PUBLIC_SEARCH_GAP = 1.0  # 1 req/sec

_MAX_PENDING = 5

# Our own ceiling on a triage list, not Crossref's (it allows 1000).
MAX_SEARCH_ROWS = 20


def in_polite_pool() -> bool:
    """Whether a contact address survives scrubbing, making the polite pool reachable.

    Through ``normalize_mailto``, so a value that scrubs to nothing — ``"()"`` — cannot
    buy the rate that identifying ourselves earns.
    """
    return useragent.normalize_mailto(config.get("CROSSREF_MAILTO")) is not None


def _resolve_policy() -> tuple[int, float, float]:
    """Return ``(max_concurrent, request_gap, search_gap)`` for the tier we may reach."""
    if in_polite_pool():
        return _POLITE_MAX_CONCURRENT, _POLITE_REQUEST_GAP, _POLITE_SEARCH_GAP
    return _PUBLIC_MAX_CONCURRENT, _PUBLIC_REQUEST_GAP, _PUBLIC_SEARCH_GAP


_single_flight = singleflight.SingleFlight()

# Same span as OpenAlex works: a reference list grows as publishers re-deposit.
_POSITIVE_TTL_SECONDS = 30 * 86400.0


def _build_headers() -> dict[str, str]:
    """Request headers: User-Agent always, mailto only when configured.

    Gating the whole header on ``CROSSREF_MAILTO`` would leave the default
    configuration identifying as ``python-httpx/x.y``.
    """
    return useragent.headers(config.get("CROSSREF_MAILTO"))


def _build_params() -> dict[str, str]:
    """Query params carrying the contact address, or empty without one.

    Either spelling admits us to the pool, but Crossref meters it *by address*, so the
    header alone leaves us anonymous in its accounting. Scrubbed like the header —
    ``openalex`` sends its raw value, which is not a licence for a new caller.
    """
    contact = useragent.normalize_mailto(config.get("CROSSREF_MAILTO"))
    return {"mailto": contact} if contact else {}


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``clients.get_client``."""
    return clients.get_client(NAMESPACE, headers=_build_headers(), timeout=30.0)


# Public whatever the config says; a Semaphore cannot shed permits, so starting wide
# would be unrecoverable.
_throttle = Throttle(
    namespace=NAMESPACE,
    label=LABEL,
    max_concurrent=_PUBLIC_MAX_CONCURRENT,
    min_gap_seconds=_PUBLIC_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


def in_confirmed_polite_pool() -> bool:
    """Whether Crossref has confirmed the polite pool served us, and we took its rate."""
    return _throttle.max_concurrent == _POLITE_MAX_CONCURRENT


def _observe_pool(response: httpx.Response) -> None:
    """Widen to the polite tier once Crossref confirms it served us from it.

    ``x-api-pool`` is Crossref's own answer to what ``in_polite_pool`` can only guess
    at: a mailto that never arrived, or one it declined, reads ``public`` here while
    the config looks configured. A missing header is not a grant — it leaves the public
    tier standing, as ``net/stats`` leaves an unadvertised quota alone.

    **It names the request class too** (``polite-single``, ``public-multi``), so only
    the part before the first ``-`` identifies the tier; matching the whole value never
    fires. Both widenings are idempotent, so this re-runs rather than latching.
    """
    if not in_polite_pool():
        return
    pool = response.headers.get("x-api-pool", "").strip().lower()
    if pool.split("-", 1)[0] != "polite":
        return
    _throttle.widen(max_concurrent=_POLITE_MAX_CONCURRENT, min_gap_seconds=_POLITE_REQUEST_GAP)
    _search_gap.widen(min_gap_seconds=_POLITE_SEARCH_GAP)


async def _throttled_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at Crossref's rate. Url-only: ``_get_client`` is the only place to configure it."""
    response = await _throttle.get(_get_client(), url, **kwargs)
    _observe_pool(response)
    return response


_search_gap = SubGap(_throttle, min_gap_seconds=_PUBLIC_SEARCH_GAP)


def reset_search_pacing() -> None:
    """Reset the search gap (test seam, called by conftest)."""
    _search_gap.reset()


def reset_pool_tier() -> None:
    """Drop back to the public tier (test seam, called by conftest).

    ``Throttle.reset`` restores neither gap nor width, so without this one test's
    promotion is the starting tier of every later one.
    """
    _throttle.max_concurrent = _PUBLIC_MAX_CONCURRENT
    _throttle.min_gap_seconds = _PUBLIC_REQUEST_GAP
    _search_gap.min_gap_seconds = _PUBLIC_SEARCH_GAP
    _throttle.reset()


async def _throttled_search_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET at Crossref's tighter *search* rate, then through the singles slot."""
    await _search_gap.wait()
    return await _throttled_get(url, **kwargs)


# ---------------------------------------------------------------------------
# DOI normalization
# ---------------------------------------------------------------------------


def canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return doinorm.canonical(doi)


def author_name(author: Any) -> str:
    """``"First Last"`` from a Crossref author entry, or its organisation ``name``.

    Crossref splits a personal name into ``given``/``family`` where OpenAlex and
    arXiv give one string. Rejoining is this module's business, so ``bibtex``'s
    surname rule and ``get_paper_authors``' name field cannot disagree about a
    name. ``Any``, not ``dict``: the entries come from untyped JSON.
    """
    if not isinstance(author, dict):
        return ""
    given = author.get("given")
    family = author.get("family")
    if isinstance(family, str) and family:
        return f"{given} {family}".strip() if isinstance(given, str) and given else family
    name = author.get("name")
    return name if isinstance(name, str) else ""


def institution_name(institution: Any) -> str:
    """The awarding institution's name from a Crossref ``institution`` value, or ``""``.

    Crossref deposits it as a *list of objects* (``[{"name": ..., "place": [...]}]``),
    not the list of strings ``title`` and ``container-title`` carry — read alike,
    every dissertation loses its ``school``. ``Any``, not ``dict``: untyped JSON,
    and some deposits carry a bare string.
    """
    if isinstance(institution, list):
        # First *named* entry: a place-only entry ahead of it is a real deposit.
        return next((found for entry in institution if (found := institution_name(entry))), "")
    if isinstance(institution, str):
        return institution
    if not isinstance(institution, dict):
        return ""
    name = institution.get("name")
    return name if isinstance(name, str) else ""


# Crossref deposits an abstract as a JATS fragment, so it is markup, not text.
_JATS_TAG_RE = re.compile(r"<[^>]*>")
# A structured abstract's section titles are content ("Background", "Methods"),
# but a lone leading "Abstract" title is the label of the field itself.
_JATS_LABEL_RE = re.compile(r"^\s*abstract[\s:]+", re.IGNORECASE)


def abstract_text(work: dict[str, Any]) -> str | None:
    """A Crossref work's abstract as plain text, or ``None``.

    The counterpart of ``openalex.reconstruct_abstract``: what the provider
    stores is not what an agent can read. Crossref deposits JATS, so tags are
    stripped, entities unescaped and whitespace collapsed — a structured
    abstract keeps its section titles, but the ``<jats:title>Abstract</jats:title>``
    labelling the field is dropped. Shape-guarded: the ``message`` arrives
    verbatim from untyped JSON.
    """
    raw = work.get("abstract")
    if not isinstance(raw, str) or not raw:
        return None
    text = " ".join(html.unescape(_JATS_TAG_RE.sub(" ", raw)).split())
    text = _JATS_LABEL_RE.sub("", text, count=1)
    return text or None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _message_of(data: object) -> dict[str, object] | None:
    """Crossref's ``message`` envelope, or ``None`` for a wrong-shape body.

    The shape ladder's first two rungs, shared by both readers; ``search_works`` adds
    the ``items`` rungs on top.
    """
    if not isinstance(data, dict):
        return None
    message = data.get("message")
    return message if isinstance(message, dict) else None


async def search_works(
    bibliographic: str,
    year: int | None = None,
    rows: int = 5,
) -> dict[str, Any]:
    """Search Crossref works by bibliographic query (title, author, etc.).

    Returns ``{"items": [...], "total_results": N | None}`` — dict-shaped hits only, and
    Crossref omits its count on some responses — or ``{"error": ...}`` on transport/HTTP
    failure or a wrong-shape body. The list is not cached (ad-hoc queries), but each hit
    with a DOI warms the works cache.
    """
    params = _build_params()
    params["query.bibliographic"] = bibliographic
    params["rows"] = str(min(max(rows, 1), MAX_SEARCH_ROWS))
    if year is not None:
        # Year-only on purpose: a fully-specified date drops works whose deposited date
        # is itself year-only (CrossRef/rest-api-doc#7).
        params["filter"] = f"from-pub-date:{year},until-pub-date:{year}"

    try:
        response = await _throttled_search_get(f"{CROSSREF_BASE_URL}/works", params=params)

        response.raise_for_status()
        data = response.json()
    except _PARSE_ERRORS:
        return _parse_error_dict()
    except http.HTTPX_ERRORS as e:
        return http.error_dict(LABEL, e)

    # A wrong shape here either raises out of the provider or reads as an empty result
    # set, and "no papers match" ends the agent's search.
    message = _message_of(data)
    if message is None:
        return _parse_error_dict()

    items = message.get("items") or []
    if not isinstance(items, list):
        return _parse_error_dict()
    items = [item for item in items if isinstance(item, dict)]

    # A hit has the same shape as /works/{doi}, so a later get_work — the graph tools and
    # paper.py's fallback_crossref, never get_paper_metadata's main path — is free.
    for item in items:
        doi = item.get("DOI")
        # isinstance, not truthiness: a non-string DOI reaches doinorm.normalize
        # and raises AttributeError, which no except clause here catches.
        if not isinstance(doi, str) or not doi:
            continue
        cache.warm(
            NAMESPACE, "works", canonical_doi(doi), item, max_age_seconds=_POSITIVE_TTL_SECONDS
        )

    return {"items": items, "total_results": message.get("total-results")}


async def get_work(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Fetch a work by DOI from Crossref, using cache when available.

    Concurrent callers for one DOI share a fetch. Returns the Crossref work object
    (the response's ``message``); ``force_refresh=True`` drops both cache halves first —
    for a reference list that may have grown, or to retry an identifier that 404'd.
    """
    canonical = canonical_doi(doi)

    not_found_error = f"No work found on Crossref for DOI: {doi}"

    async def _fetch() -> dict[str, Any]:
        bare_doi = doinorm.normalize(doi)
        # Percent-encoded so a reserved character can't truncate the request to the
        # wrong record; the DOI's own slash stays literal.
        url = f"{CROSSREF_BASE_URL}/works/{quote(bare_doi, safe='/')}"

        if not http.addresses_a_record(url):
            # A `.`/`..` segment shortens the path to the /works *collection*, whose 200
            # carries a work-list under a dict `message`: it clears the ladder below and
            # would cache as this DOI's work. Nothing cached — no request was spent.
            return http.not_found(not_found_error)

        try:
            response = await _throttled_get(url, params=_build_params())

            if response.status_code == 404:
                # Definitive, hence both the negative entry and the flag tools/graph.py forwards.
                err = http.not_found(not_found_error)
                cache.put_negative(NAMESPACE, "works", canonical, err)
                return err

            response.raise_for_status()
            data = response.json()
        except _PARSE_ERRORS:
            # Transient, not "not found" — uncached, so a retry re-fetches.
            return _parse_error_dict()
        except http.HTTPX_ERRORS as e:
            return http.error_dict(LABEL, e)

        # Wrong shape, not an empty work: never positive-cached for the TTL.
        work = _message_of(data)
        if work is None:
            return _parse_error_dict()
        cache.put(NAMESPACE, "works", canonical, work)
        return work

    return await cache.cached_lookup(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity="works",
        canonical=canonical,
        positive_ttl=_POSITIVE_TTL_SECONDS,
        fetch=_fetch,
        force_refresh=force_refresh,
    )
