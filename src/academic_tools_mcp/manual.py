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
from pathlib import Path
from typing import Any

from . import _doi, _pdf_download, cache, papers

NAMESPACE = "manual"

# ---------------------------------------------------------------------------
# Identifier normalization (manual-only fallback)
# ---------------------------------------------------------------------------


def _normalize_identifier(identifier: str) -> str:
    """Normalize an identifier to a bare form.

    If it looks like a DOI (bare, ``doi:`` prefix in any case, or a doi.org /
    dx.doi.org URL), strip to the bare DOI via :mod:`_doi`. Anything else —
    arXiv ids, freeform labels — is returned stripped but untouched, since
    this dispatcher accepts identifiers that are not DOIs at all.
    """
    return _doi.normalize(identifier)


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
    from .providers import arxiv

    candidate = arxiv._normalize_arxiv_id(normalized)
    return bool(_ARXIV_NEW_RE.match(candidate) or _ARXIV_OLD_RE.match(candidate))


def resolve_metadata_source(identifier: str) -> str | None:
    """Detect which provider should serve *metadata* for *identifier*.

    Returns one of ``"arxiv"``, ``"biorxiv"``, ``"openalex"``, or ``None``
    when the identifier does not resolve to a known metadata provider
    (e.g. a freeform label).

    Unlike :func:`resolve_target` (which routes PDF storage), ACL DOIs and
    any other DOI shape route to OpenAlex — ACL Anthology has no metadata
    API of its own, and OpenAlex handles arbitrary publisher DOIs.
    """
    from .providers import biorxiv

    normalized = _normalize_identifier(identifier)

    if _is_arxiv_identifier(normalized):
        return "arxiv"

    if biorxiv.is_biorxiv_doi(normalized):
        return "biorxiv"

    if _DOI_RE.match(normalized):
        return "openalex"

    return None


def resolve_target(identifier: str) -> dict[str, Any]:
    """Detect the target provider from *identifier* and return routing info.

    Returns a dict with:
      - namespace: cache namespace (e.g. "arxiv", "biorxiv", "manual")
      - canonical: canonical cache key for that provider
      - pdf_path: Path where the provider expects its PDF
    """
    from .providers import acl_anthology, arxiv, biorxiv

    normalized = _normalize_identifier(identifier)

    # --- arXiv (not a DOI, so check first) ---
    arxiv_norm = arxiv._normalize_arxiv_id(normalized)
    if _ARXIV_NEW_RE.match(arxiv_norm) or _ARXIV_OLD_RE.match(arxiv_norm):
        canonical = arxiv.canonical_arxiv_id(normalized)
        return {
            "namespace": arxiv.NAMESPACE,
            "canonical": canonical,
            "pdf_path": arxiv.pdf_path(normalized),
        }

    # --- ACL Anthology DOI (must check before generic DOI) ---
    if acl_anthology.is_acl_doi(normalized):
        canonical = acl_anthology.canonical_key(normalized)
        return {
            "namespace": acl_anthology.NAMESPACE,
            "canonical": canonical,
            "pdf_path": acl_anthology.pdf_path(normalized),
        }

    # --- bioRxiv / medRxiv DOI ---
    if biorxiv.is_biorxiv_doi(normalized):
        canonical = biorxiv.canonical_key(normalized)
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
    """Build a safe PDF filename from a canonical identifier.

    The filename is fed to the converter subprocess (``convert_pdf`` shells out
    via ``bash -c``), so strip anything outside a conservative safe charset —
    not just ``/`` and ``:`` — to keep shell metacharacters (``$``, backtick,
    quotes, spaces, ...) in an exotic identifier from ever reaching the shell.
    Dotted/hyphenated DOIs and arXiv ids are unaffected (``.``/``-`` are kept),
    so normal identifiers map to the same name as before.
    """
    return papers._safe_stem(canonical) + ".pdf"


def _manual_pdf_path(canonical: str) -> Path:
    """PDF path in the manual namespace (fallback only)."""
    return cache.cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)


def pdf_path(identifier: str) -> Path:
    """Return the expected cache path for a PDF, routed to the correct provider."""
    return resolve_target(identifier)["pdf_path"]


def _looks_like_cached_pdf(path: Path) -> bool:
    """Whether an existing cached PDF should be trusted as a hit.

    Thin alias for ``_pdf_download.is_usable_pdf``, the single home for this
    check — the native PDF providers and ``convert_paper`` guard on it too, so
    the rule cannot drift between the imported and downloaded paths. Kept as a
    name because callers and tests in this module read better with the local
    spelling.
    """
    return _pdf_download.is_usable_pdf(path)


def _invalidate_derived(namespace: str, canonical: str) -> None:
    """Drop cached markdown + section index for a paper.

    Mirrors the force_refresh cascade in ``tools/pipeline.py``: once the PDF
    is replaced, any previously-converted markdown and its section index are
    stale relative to the new bytes, so the next ``convert_paper`` must re-run
    rather than return previously-converted text.
    """
    md_path = papers.markdown_path(namespace, canonical)
    try:
        md_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    cache.invalidate(namespace, "sections", papers.sections_key(canonical))


def import_local_pdf(
    file_path: str, identifier: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Copy a local PDF into the cache.

    Routes to the correct provider namespace based on the identifier
    (arXiv ID, bioRxiv DOI, ACL DOI, or manual fallback).

    Args:
        file_path: Absolute or relative path to the PDF file.
        identifier: DOI, arXiv ID, or freeform label to key this paper.
        force_refresh: Replace an already-cached PDF for this identifier
            instead of returning it as ``cached``. When a PDF is actually
            (re)written over a prior one, the cached markdown + section index
            are dropped so the next ``convert_paper`` picks up the new bytes.

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

    target = resolve_target(identifier)
    dest = target["pdf_path"]

    existed = dest.exists()
    if not force_refresh and existed and _looks_like_cached_pdf(dest):
        return {
            "identifier": _normalize_identifier(identifier),
            "namespace": target["namespace"],
            "path": str(dest),
            "size_bytes": dest.stat().st_size,
            "cached": True,
        }

    # Atomic copy: a crash / disk-full mid-copy can't leave a half-written
    # canonical PDF (which _looks_like_cached_pdf would then have to reject).
    try:
        cache._atomic_copy(source, dest)
    except OSError as e:
        return {"error": f"Could not copy {file_path} into the cache: {e}"}

    result: dict[str, Any] = {
        "identifier": _normalize_identifier(identifier),
        "namespace": target["namespace"],
        "path": str(dest),
        "size_bytes": dest.stat().st_size,
        "cached": False,
    }

    # Replacing an existing PDF (or a forced refresh) makes any previously
    # converted markdown + sections stale — cascade-drop them.
    if existed or force_refresh:
        _invalidate_derived(target["namespace"], target["canonical"])
        result["cascaded_invalidated"] = ["markdown", "sections"]

    return result


# ---------------------------------------------------------------------------
# Markdown import
# ---------------------------------------------------------------------------


def import_markdown(
    file_path: str, identifier: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Copy a local markdown file into the cache and parse sections.

    This skips the PDF download and conversion steps entirely.
    The markdown is stored in the target provider's cache location so
    the native section tools find it immediately.

    Args:
        file_path: Absolute or relative path to a markdown file.
        identifier: DOI, arXiv ID, or freeform label to key this paper.
        force_refresh: Replace already-cached markdown for this identifier
            instead of returning it as ``cached``, re-parsing the section
            index from the new file.

    Returns:
        Dict with the markdown path, section index, or an error.
    """
    source = Path(file_path).expanduser().resolve()

    if not source.exists():
        return {"error": f"File not found: {file_path}"}

    if not source.is_file():
        return {"error": f"Not a file: {file_path}"}

    target = resolve_target(identifier)
    namespace = target["namespace"]
    canonical = target["canonical"]
    md_path = papers.markdown_path(namespace, canonical)

    if not force_refresh and md_path.exists():
        # Cached markdown is written UTF-8 (below), so read it back UTF-8 too
        # — a locale-default read would mis-decode / raise on non-ASCII under
        # a non-UTF-8 host locale. Handle decode/IO errors the same way the
        # fresh read below does instead of letting a raw exception escape.
        try:
            markdown = md_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            return {
                "error": (
                    f"Cached markdown for {identifier!r} is not valid UTF-8 "
                    f"({e.reason} at byte {e.start}) — the cache entry is "
                    "corrupt. Re-import with force_refresh=True."
                )
            }
        except OSError as e:
            return {"error": f"Could not read cached markdown for {identifier!r}: {e}"}
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

    # Write and index through the shared writer so this entry carries the same
    # four keys a converted paper's does. Assembling the payload here instead
    # omitted ``sections_detected``, and because the checksum still matched,
    # ``get_paper_sections`` reported an imported heading-free paper as having
    # detected sections. ``mode="imported"`` records that no converter ran.
    #
    # The markdown goes in verbatim — no image-path stripping, no rstrip. That
    # post-processing is right for converter output and wrong here: this file
    # is the operator's own text, often a hand-made conversion whose links
    # resolve.
    stored = papers.store_markdown_and_index(namespace, canonical, md_path, markdown, "imported")

    return {
        "identifier": _normalize_identifier(identifier),
        "namespace": namespace,
        "markdown_path": str(md_path),
        "sections": stored["sections"],
        "cached": False,
    }
