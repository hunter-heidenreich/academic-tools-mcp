"""Manual paper import — for local files and pre-converted markdown.

Supports two intake paths:
  1. Local PDF: copy an existing PDF into the cache
  2. Markdown import: copy a pre-converted markdown file directly into the cache,
     skipping the PDF download and conversion steps entirely

Both paths use a user-supplied identifier (typically a DOI or arXiv ID) as the
cache key.  When the identifier matches a known provider (arXiv, bioRxiv/medRxiv,
ACL Anthology), the PDF/markdown is stored in **that provider's** cache namespace
so the native pipeline tools find it — no duplicates.  Unrecognised identifiers
fall back to the ``manual`` namespace.
"""

import re
import shutil
from pathlib import Path
from typing import Any

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

# DOI shape: "10.<registrant>/<suffix>"
_DOI_RE = re.compile(r"^10\.\d{4,}/\S+$")


def _is_arxiv_identifier(normalized: str) -> bool:
    """Return True if *normalized* matches an arXiv ID shape."""
    from . import arxiv

    candidate = arxiv._normalize_arxiv_id(normalized)
    return bool(_ARXIV_NEW_RE.match(candidate) or _ARXIV_OLD_RE.match(candidate))


def _resolve_metadata_source(identifier: str) -> str | None:
    """Detect which provider should serve *metadata* for *identifier*.

    Returns one of ``"arxiv"``, ``"biorxiv"``, ``"openalex"``, or ``None``
    when the identifier does not resolve to a known metadata provider
    (e.g. a freeform label).

    Unlike :func:`_resolve_target` (which routes PDF storage), ACL DOIs and
    any other DOI shape route to OpenAlex — ACL Anthology has no metadata
    API of its own, and OpenAlex handles arbitrary publisher DOIs.
    """
    from . import biorxiv

    normalized = _normalize_identifier(identifier)

    if _is_arxiv_identifier(normalized):
        return "arxiv"

    if biorxiv.is_biorxiv_doi(normalized):
        return "biorxiv"

    if _DOI_RE.match(normalized):
        return "openalex"

    return None


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

    try:
        with source.open("rb") as f:
            header = f.read(5)
    except OSError as e:
        return {"error": f"Could not read file {file_path}: {e}"}

    if header != b"%PDF-":
        return {
            "error": (
                f"Not a PDF: {file_path} (missing %PDF- header). "
                "If this is pre-converted text, save it as .md/.markdown and "
                "re-import."
            )
        }

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

    try:
        markdown = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return {
            "error": (
                f"Could not decode {file_path} as UTF-8 ({e.reason} at byte {e.start}). "
                "Re-save the file as UTF-8 and retry."
            )
        }
    except OSError as e:
        return {"error": f"Could not read file {file_path}: {e}"}

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
