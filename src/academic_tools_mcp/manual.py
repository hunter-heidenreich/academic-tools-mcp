"""Manual paper import — for local files, arbitrary URLs, and pre-converted markdown.

Supports three intake paths:
  1. Local PDF: copy an existing PDF into the cache
  2. URL download: fetch a PDF from any URL into the cache
  3. Markdown import: copy a pre-converted markdown file directly into the cache,
     skipping the PDF download and conversion steps entirely

All paths use a user-supplied identifier (typically a DOI or arXiv ID) as the
cache key.  When the identifier matches a known provider (arXiv, bioRxiv/medRxiv,
ACL Anthology), the PDF/markdown is stored in **that provider's** cache namespace
so the native pipeline tools find it — no duplicates.  Unrecognised identifiers
fall back to the ``manual`` namespace.
"""

import re
import shutil
from pathlib import Path
from typing import Any

import httpx

from . import cache, papers

NAMESPACE = "manual"

# ---------------------------------------------------------------------------
# Identifier normalization (manual-only fallback)
# ---------------------------------------------------------------------------

_DOI_URL_RE = re.compile(r"https?://(?:dx\.)?doi\.org/(10\.\d{4,}/\S+)$")


def _normalize_identifier(identifier: str) -> str:
    """Normalize an identifier to a bare form.

    If it looks like a DOI (bare, doi: prefix, or URL), strip to the bare DOI.
    Otherwise, return as-is after stripping whitespace.
    """
    identifier = identifier.strip()

    if identifier.lower().startswith("doi:"):
        identifier = identifier[4:]

    m = _DOI_URL_RE.match(identifier)
    if m:
        return m.group(1)

    return identifier


def _canonical_key(identifier: str) -> str:
    """Return a canonical cache key from an identifier."""
    return _normalize_identifier(identifier).lower()


# ---------------------------------------------------------------------------
# Provider routing — store in the right namespace automatically
# ---------------------------------------------------------------------------

# arXiv ID patterns (new-style 2301.00001 and old-style hep-th/9901001)
_ARXIV_NEW_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_ARXIV_OLD_RE = re.compile(r"^[a-z-]+/\d{7}(v\d+)?$")


def _resolve_target(identifier: str) -> dict[str, Any]:
    """Detect the target provider from *identifier* and return routing info.

    Returns a dict with:
      - namespace: cache namespace (e.g. "arxiv", "biorxiv", "manual")
      - canonical: canonical cache key for that provider
      - pdf_path: Path where the provider expects its PDF
    """
    from . import arxiv, biorxiv, acl_anthology

    normalized = _normalize_identifier(identifier)

    # --- arXiv (not a DOI, so check first) ---
    arxiv_norm = arxiv._normalize_arxiv_id(normalized)
    if _ARXIV_NEW_RE.match(arxiv_norm) or _ARXIV_OLD_RE.match(arxiv_norm):
        canonical = arxiv._canonical_arxiv_id(normalized)
        return {
            "namespace": arxiv.NAMESPACE,
            "canonical": canonical,
            "pdf_path": arxiv.pdf_path(normalized),
        }

    # --- ACL Anthology DOI (must check before generic DOI) ---
    if acl_anthology.is_acl_doi(normalized):
        canonical = acl_anthology._canonical_key(normalized)
        return {
            "namespace": acl_anthology.NAMESPACE,
            "canonical": canonical,
            "pdf_path": acl_anthology.pdf_path(normalized),
        }

    # --- bioRxiv / medRxiv DOI ---
    if biorxiv.is_biorxiv_doi(normalized):
        canonical = biorxiv._canonical_key(normalized)
        return {
            "namespace": biorxiv.NAMESPACE,
            "canonical": canonical,
            "pdf_path": biorxiv.pdf_path(normalized),
        }

    # --- Fallback: manual namespace ---
    canonical = _canonical_key(identifier)
    return {
        "namespace": NAMESPACE,
        "canonical": canonical,
        "pdf_path": _manual_pdf_path(canonical),
    }


# ---------------------------------------------------------------------------
# PDF storage
# ---------------------------------------------------------------------------


def _pdf_filename(canonical: str) -> str:
    """Build a safe PDF filename from a canonical identifier."""
    return canonical.replace("/", "_").replace(":", "_") + ".pdf"


def _manual_pdf_path(canonical: str) -> Path:
    """PDF path in the manual namespace (fallback only)."""
    return cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)


def pdf_path(identifier: str) -> Path:
    """Return the expected cache path for a PDF, routed to the correct provider."""
    return _resolve_target(identifier)["pdf_path"]


def import_local_pdf(file_path: str, identifier: str) -> dict[str, Any]:
    """Copy a local PDF into the cache.

    Routes to the correct provider namespace based on the identifier
    (arXiv ID, bioRxiv DOI, ACL DOI, or manual fallback).

    Args:
        file_path: Absolute or relative path to the PDF file.
        identifier: DOI, arXiv ID, or freeform label to key this paper.

    Returns:
        Dict with the cache path and size, or an error.
    """
    source = Path(file_path).expanduser().resolve()

    if not source.exists():
        return {"error": f"File not found: {file_path}"}

    if not source.is_file():
        return {"error": f"Not a file: {file_path}"}

    target = _resolve_target(identifier)
    dest = target["pdf_path"]

    if dest.exists():
        return {
            "identifier": _normalize_identifier(identifier),
            "namespace": target["namespace"],
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "cached": True,
        }

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)

    return {
        "identifier": _normalize_identifier(identifier),
        "namespace": target["namespace"],
        "path": str(dest),
        "size_bytes": dest.stat().st_size,
        "cached": False,
    }


async def download_pdf_from_url(url: str, identifier: str) -> dict[str, Any]:
    """Download a PDF from an arbitrary URL and cache it.

    Routes to the correct provider namespace based on the identifier.

    Args:
        url: Direct URL to a PDF file.
        identifier: DOI, arXiv ID, or freeform label to key this paper.

    Returns:
        Dict with the cache path and size, or an error.
    """
    target = _resolve_target(identifier)
    dest = target["pdf_path"]

    if dest.exists():
        return {
            "identifier": _normalize_identifier(identifier),
            "namespace": target["namespace"],
            "url": url,
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "cached": True,
        }

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(url)

    if response.status_code == 404:
        return {"error": f"PDF not found at URL: {url}"}

    response.raise_for_status()

    # Basic content-type sanity check (some servers don't set it correctly)
    content_type = response.headers.get("content-type", "")
    if content_type and "html" in content_type and "pdf" not in content_type:
        return {
            "error": f"URL returned HTML, not a PDF. You may need to authenticate or use a direct download link. Content-Type: {content_type}"
        }

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)

    return {
        "identifier": _normalize_identifier(identifier),
        "namespace": target["namespace"],
        "url": url,
        "path": str(dest),
        "size_bytes": len(response.content),
        "cached": False,
    }


# ---------------------------------------------------------------------------
# Markdown import
# ---------------------------------------------------------------------------


def import_markdown(file_path: str, identifier: str) -> dict[str, Any]:
    """Copy a local markdown file into the cache and parse sections.

    This skips the PDF download and conversion steps entirely.
    The markdown is stored in the target provider's cache location so
    the native section tools find it immediately.

    Args:
        file_path: Absolute or relative path to a markdown file.
        identifier: DOI, arXiv ID, or freeform label to key this paper.

    Returns:
        Dict with the markdown path, section index, or an error.
    """
    source = Path(file_path).expanduser().resolve()

    if not source.exists():
        return {"error": f"File not found: {file_path}"}

    if not source.is_file():
        return {"error": f"Not a file: {file_path}"}

    target = _resolve_target(identifier)
    namespace = target["namespace"]
    canonical = target["canonical"]
    md_path = papers._markdown_path(namespace, canonical)

    if md_path.exists():
        markdown = md_path.read_text()
        sections = papers.parse_sections(markdown)
        return {
            "identifier": _normalize_identifier(identifier),
            "namespace": namespace,
            "markdown_path": str(md_path),
            "sections": sections,
            "cached": True,
        }

    markdown = source.read_text()

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown)

    # Parse and cache the section index
    sections = papers.parse_sections(markdown)
    sections_data = {"sections": sections}
    cache.put(namespace, "sections", papers._sections_key(canonical), sections_data)

    return {
        "identifier": _normalize_identifier(identifier),
        "namespace": namespace,
        "markdown_path": str(md_path),
        "sections": sections,
        "cached": False,
    }
