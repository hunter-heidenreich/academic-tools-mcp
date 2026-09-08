"""ACL Anthology client. Maps ACL DOIs to anthology IDs and fetches their PDFs."""

import re
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx

from .. import _pdf_download, _singleflight, _stems, cache
from ..net import clients, http
from ..net.throttle import Throttle
from ..util import doinorm, useragent

# Not "acl": this is the cache *directory* name, so renaming it needs a sweep.
NAMESPACE = "acl_anthology"

# Agent-facing provider name; every site that names us reads it (providers.md).
LABEL = "ACL Anthology"

# Exported: ``cache_search`` inverts a stored stem with this same prefix.
ACL_DOI_PREFIX = "10.18653/v1/"

# PDF downloads are larger than a metadata call; use a generous timeout.
_PDF_TIMEOUT_SECONDS = 60.0

# No documented rate limit, hence no gap. A static-file CDN, so 4 PDF
# downloads may stream at once; the burst cap still applies past _MAX_PENDING.
_MAX_CONCURRENT = 4
_MIN_REQUEST_GAP = 0.0
_MAX_PENDING = 5

# 24h, not the preprint servers' 1h: a camera-ready file is static, so a 404
# means a wrong ID or a paper not posted yet — neither resolves in minutes.
_NEG_ENTITY = "downloads"
_NEG_TTL_SECONDS = 24 * 60 * 60

_single_flight = _singleflight.SingleFlight()

_throttle = Throttle(
    namespace=NAMESPACE,
    label=LABEL,
    max_concurrent=_MAX_CONCURRENT,
    min_gap_seconds=_MIN_REQUEST_GAP,
    max_pending=_MAX_PENDING,
)


def _get_client() -> httpx.AsyncClient:
    """The pooled AsyncClient. Configured here or nowhere — see ``clients.get_client``."""
    return clients.get_client(NAMESPACE, headers=useragent.headers(), timeout=_PDF_TIMEOUT_SECONDS)


def _request_slot(url: str) -> AbstractAsyncContextManager[None]:
    """ACL Anthology's rate-limit slot (see ``Throttle.slot``).

    Module-level so ``slot_factory`` and the test seam resolve an attribute.
    """
    return _throttle.slot(url)


# ---------------------------------------------------------------------------
# DOI → Anthology ID resolution
# ---------------------------------------------------------------------------


def _strip_acl_prefix(bare: str) -> str | None:
    """Return the anthology-id suffix if ``bare`` carries the ACL prefix, else ``None``.

    The prefix is matched case-insensitively (DOIs are), and the suffix comes
    back untouched for ``_normalize_anthology_id``. An empty suffix is *not* an
    ACL DOI: ``safe_stem("")`` would cache every ``10.18653/v1/`` as one ``.pdf``.
    """
    if bare[: len(ACL_DOI_PREFIX)].lower() != ACL_DOI_PREFIX:
        return None
    suffix = bare[len(ACL_DOI_PREFIX) :]
    return suffix if suffix.strip() else None


def is_acl_doi(doi: str) -> bool:
    """Check if a DOI belongs to the ACL Anthology."""
    return _strip_acl_prefix(doinorm.normalize(doi)) is not None


def canonical_key(doi: str) -> str:
    """Return a canonical cache key from an ACL DOI."""
    return doinorm.canonical(doi)


# Pre-2020 IDs: <LETTER><2-digit-year>-<digits>, e.g. P16-1160, W04-1013.
_OLD_FORMAT_ID_RE = re.compile(r"^[A-Za-z]\d{2}-\d+$")


def _normalize_anthology_id(anthology_id: str) -> str:
    """Uppercase old-format Anthology IDs so the CDN URL resolves.

    The CDN is case-sensitive (``p16-1160.pdf`` 404s) and Crossref hands these
    back lowercased. New-format IDs (``2023.acl-long.1``) carry lowercase venue
    letters and must stay untouched.
    """
    if _OLD_FORMAT_ID_RE.match(anthology_id):
        return anthology_id.upper()
    return anthology_id


def doi_to_anthology_id(doi: str) -> str | None:
    """Extract an ACL Anthology ID from a DOI.

    e.g., "10.18653/v1/2023.acl-long.1" -> "2023.acl-long.1"; ``None`` if the
    DOI is not an ACL one. Invariant: the ID addresses the CDN and names nothing
    on disk — every cached artifact keys on ``canonical_key``.
    """
    suffix = _strip_acl_prefix(doinorm.normalize(doi))
    if suffix is None:
        return None
    return _normalize_anthology_id(suffix)


def pdf_url(anthology_id: str) -> str:
    """Build the direct PDF URL for an Anthology paper.

    ``safe=""``: an Anthology ID has no path structure, so a stray ``/`` from a
    bogus DOI suffix is an escape, not a separator.
    """
    return f"https://aclanthology.org/{quote(anthology_id, safe='')}.pdf"


# ---------------------------------------------------------------------------
# PDF download
# ---------------------------------------------------------------------------


def pdf_path(doi: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet).

    Raises ``ValueError`` for a non-ACL DOI rather than returning a sentinel: a
    path whose ``.exists()`` is truthy slips a non-PDF past ``convert_paper``.
    """
    if not is_acl_doi(doi):
        raise ValueError(f"Not an ACL Anthology DOI: {doi}")
    return _stems.pdf_path(NAMESPACE, canonical_key(doi))


async def download_pdf(doi: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """Download the PDF for an ACL Anthology paper and cache it locally.

    Returns the file path and size, or an error. ``force_refresh=True``
    re-downloads and atomically replaces the cached PDF — the Anthology
    occasionally re-issues a camera-ready at the same URL — keeping the
    existing file if the re-download fails.
    """
    aid = doi_to_anthology_id(doi)
    if aid is None:
        # Definitive: unflagged, every classifier downstream reads it as unknown.
        return http.not_found(f"Not an ACL Anthology DOI: {doi}")

    canonical = canonical_key(doi)
    dest = _stems.pdf_path(NAMESPACE, canonical)
    url = pdf_url(aid)

    async def _fetch() -> dict[str, Any]:
        return await _pdf_download.stream_to_file(
            _get_client(),
            url,
            dest,
            slot_factory=lambda: _request_slot(url),
            namespace=NAMESPACE,
            provider_label=LABEL,
            timeout=_PDF_TIMEOUT_SECONDS,
            not_found_message=f"PDF not found on ACL Anthology for: {aid}",
        )

    # ``extra_fields`` puts the ACL provenance on a cached hit and a fresh
    # success alike, without this function restating either branch.
    return await _pdf_download.cached_download(
        single_flight=_single_flight,
        namespace=NAMESPACE,
        entity=_NEG_ENTITY,
        canonical=canonical,
        dest=dest,
        fetch=_fetch,
        neg_ttl=_NEG_TTL_SECONDS,
        force_refresh=force_refresh,
        extra_fields={"anthology_id": aid, "pdf_url": url},
    )


# ---------------------------------------------------------------------------
# Startup migration
# ---------------------------------------------------------------------------

# Derived, never spelled out, so it cannot drift from what ``pdf_path`` writes.
_CANONICAL_STEM_PREFIX = _stems.safe_stem(ACL_DOI_PREFIX)


def migrate_legacy_pdf_stems() -> int:
    """Re-file cached PDFs named after the Anthology ID, not the canonical key.

    Only ``pdfs/`` moves; markdown and sections were always canonical-keyed.
    Run at startup, so idempotent and best-effort like
    ``papers.migrate_legacy_stems``: nothing here may raise out of the lifespan.
    Returns the number of files moved.
    """
    moved = 0
    pdf_dir = cache.cache_dir(NAMESPACE, "pdfs")
    # Materialised: the loop renames files into the directory it walks.
    for path in _stems.list_dir(pdf_dir):
        # An in-flight ``.tmp`` still carries the destination's stem; renaming
        # it breaks the writer's ``os.replace``.
        if path.suffix != ".pdf" or path.stem.startswith(_CANONICAL_STEM_PREFIX):
            continue
        if not path.is_file():
            continue
        target = pdf_dir / (
            _stems.safe_stem(canonical_key(ACL_DOI_PREFIX + unquote(path.stem))) + ".pdf"
        )
        if target.exists():
            # Already migrated (or a genuine collision) — leave both in place
            # rather than destroying data.
            continue
        try:
            path.rename(target)
            moved += 1
        except OSError:
            continue
    return moved
