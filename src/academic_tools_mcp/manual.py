"""Manual paper import — for local files, arbitrary URLs, and pre-converted markdown.

Supports three intake paths:
  1. Local PDF: copy an existing PDF into the cache
  2. URL download: fetch a PDF from any URL into the cache
  3. Markdown import: copy a pre-converted markdown file directly into the cache,
     skipping the PDF download and MinerU conversion steps entirely

All paths use a user-supplied identifier (typically a DOI) as the cache key,
enabling chaining into Crossref/OpenAlex for metadata after import.
"""

import re
import shutil
from pathlib import Path
from typing import Any

import httpx

from . import cache, papers

NAMESPACE = "manual"

# ---------------------------------------------------------------------------
# Identifier normalization
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
# PDF storage
# ---------------------------------------------------------------------------


def _pdf_filename(canonical: str) -> str:
    """Build a safe PDF filename from a canonical identifier."""
    return canonical.replace("/", "_").replace(":", "_") + ".pdf"


def pdf_path(identifier: str) -> Path:
    """Return the expected cache path for a PDF (may or may not exist yet)."""
    canonical = _canonical_key(identifier)
    return cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)


def import_local_pdf(file_path: str, identifier: str) -> dict[str, Any]:
    """Copy a local PDF into the cache.

    Args:
        file_path: Absolute or relative path to the PDF file.
        identifier: DOI or freeform label to key this paper.

    Returns:
        Dict with the cache path and size, or an error.
    """
    source = Path(file_path).expanduser().resolve()

    if not source.exists():
        return {"error": f"File not found: {file_path}"}

    if not source.is_file():
        return {"error": f"Not a file: {file_path}"}

    canonical = _canonical_key(identifier)
    dest = cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)

    if dest.exists():
        return {
            "identifier": _normalize_identifier(identifier),
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "cached": True,
        }

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)

    return {
        "identifier": _normalize_identifier(identifier),
        "path": str(dest),
        "size_bytes": dest.stat().st_size,
        "cached": False,
    }


async def download_pdf_from_url(url: str, identifier: str) -> dict[str, Any]:
    """Download a PDF from an arbitrary URL and cache it.

    Args:
        url: Direct URL to a PDF file.
        identifier: DOI or freeform label to key this paper.

    Returns:
        Dict with the cache path and size, or an error.
    """
    canonical = _canonical_key(identifier)
    dest = cache._cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)

    if dest.exists():
        return {
            "identifier": _normalize_identifier(identifier),
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

    This skips the PDF download and MinerU conversion steps entirely.
    The markdown is stored in the same location that convert_manual_paper
    would produce, so get_manual_paper_sections / get_manual_paper_section
    work immediately after this call.

    Args:
        file_path: Absolute or relative path to a markdown file.
        identifier: DOI or freeform label to key this paper.

    Returns:
        Dict with the markdown path, section index, or an error.
    """
    source = Path(file_path).expanduser().resolve()

    if not source.exists():
        return {"error": f"File not found: {file_path}"}

    if not source.is_file():
        return {"error": f"Not a file: {file_path}"}

    canonical = _canonical_key(identifier)
    md_path = papers._markdown_path(NAMESPACE, canonical)

    if md_path.exists():
        markdown = md_path.read_text()
        sections = papers.parse_sections(markdown)
        return {
            "identifier": _normalize_identifier(identifier),
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
    cache.put(NAMESPACE, "sections", papers._sections_key(canonical), sections_data)

    return {
        "identifier": _normalize_identifier(identifier),
        "markdown_path": str(md_path),
        "sections": sections,
        "cached": False,
    }
