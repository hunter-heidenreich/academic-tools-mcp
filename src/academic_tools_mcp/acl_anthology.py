import re
from pathlib import Path
from typing import Any

import httpx

from . import cache

NAMESPACE = "acl_anthology"

# ACL Anthology DOI prefix — all ACL venue papers use this
_ACL_DOI_PREFIX = "10.18653/v1/"


# ---------------------------------------------------------------------------
# DOI → Anthology ID resolution
# ---------------------------------------------------------------------------


def is_acl_doi(doi: str) -> bool:
    """Check if a DOI belongs to the ACL Anthology."""
    return _normalize_doi(doi).startswith(_ACL_DOI_PREFIX)


def _normalize_doi(doi: str) -> str:
    """Normalize a DOI to bare form (e.g., 10.18653/v1/2023.acl-long.1)."""
    doi = doi.strip()
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    elif doi.startswith("http://doi.org/"):
        doi = doi[len("http://doi.org/"):]
    elif doi.startswith("doi:"):
        doi = doi[len("doi:"):]
    return doi


def doi_to_anthology_id(doi: str) -> str | None:
    """Extract an ACL Anthology ID from a DOI.

    e.g., "10.18653/v1/2023.acl-long.1" -> "2023.acl-long.1"
    Returns None if the DOI is not an ACL Anthology DOI.
    """
    bare = _normalize_doi(doi)
    if not bare.startswith(_ACL_DOI_PREFIX):
        return None
    return bare[len(_ACL_DOI_PREFIX):]


def _canonical_key(doi: str) -> str:
    """Return a canonical cache key from an ACL DOI."""
    return _normalize_doi(doi).lower()


def pdf_url(anthology_id: str) -> str:
    """Build the direct PDF URL for an Anthology paper."""
    return f"https://aclanthology.org/{anthology_id}.pdf"


# ---------------------------------------------------------------------------
# PDF download
# ---------------------------------------------------------------------------


def _pdf_filename(anthology_id: str) -> str:
    """Build a human-readable PDF filename from an Anthology ID."""
    return anthology_id.replace("/", "_") + ".pdf"


def pdf_path(doi: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet)."""
    aid = doi_to_anthology_id(doi)
    if aid is None:
        return Path("/dev/null")
    return cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(aid)


async def download_pdf(doi: str) -> dict[str, Any]:
    """Download the PDF for an ACL Anthology paper and cache it locally.

    Returns a dict with the file path and size, or an error.
    """
    aid = doi_to_anthology_id(doi)
    if aid is None:
        return {"error": f"Not an ACL Anthology DOI: {doi}"}

    dest = cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(aid)

    if dest.exists():
        return {
            "anthology_id": aid,
            "pdf_url": pdf_url(aid),
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "cached": True,
        }

    url = pdf_url(aid)

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(url)

    if response.status_code == 404:
        return {"error": f"PDF not found on ACL Anthology for: {aid}"}

    response.raise_for_status()

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)

    return {
        "anthology_id": aid,
        "pdf_url": url,
        "path": str(dest),
        "size_bytes": len(response.content),
        "cached": False,
    }
