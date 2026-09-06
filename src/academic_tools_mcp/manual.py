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

import contextlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import _doi, _pdf_download, _stats, cache, papers

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

# arXiv ID patterns (new-style 2301.00001 and old-style hep-th/9901001).
# Matched against the *lowercased* id, and the old-style archive class carries
# "." — old-style ids are dotted (``math.GT/0309136``, ``cond-mat.stat-mech``)
# and vary in case upstream, exactly as ``canonical_arxiv_id`` documents. An
# id these reject falls through to the ``manual`` namespace under a canonical
# key that is *already* the arXiv one, so one paper lands under two
# namespaces depending on how it was typed.
_ARXIV_NEW_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_ARXIV_OLD_RE = re.compile(r"^[a-z][a-z.\-]*/\d{7}(v\d+)?$")


def _is_arxiv_identifier(normalized: str) -> bool:
    """Return True if *normalized* matches an arXiv ID shape."""
    from .providers import arxiv

    candidate = arxiv._normalize_arxiv_id(normalized).lower()
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

    # Invariant: dispatch tests DOI shape with the same predicate that builds
    # the cache key, so the two can never disagree about what is a DOI.
    if _doi.looks_like_doi(normalized):
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
    # Same predicate as resolve_metadata_source, so storage and metadata can
    # never disagree about which identifiers are arXiv's.
    if _is_arxiv_identifier(normalized):
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


def migrate_misrouted_arxiv() -> int:
    """Move cached files that ``resolve_target`` now routes to ``arxiv``.

    Old-style arXiv ids that are dotted (``math.GT/0309136``,
    ``cond-mat.stat-mech/0501001``) or upper-cased were rejected by
    ``_ARXIV_OLD_RE`` and fell through to ``manual``, under a canonical key
    identical to the one ``arxiv`` uses. The stem is therefore unchanged and
    this is a plain directory move.

    Idempotent and best-effort, like ``papers.migrate_legacy_stems``: a file
    that can't be moved is left for the next run, an existing target is never
    overwritten, and the count of moved files is returned. Called once at
    startup. The sections index keys on a hash, so it re-derives on its own.
    """
    from .providers import arxiv

    moved = 0
    for entity in ("pdfs", "markdown"):
        source_dir = cache.cache_dir(NAMESPACE, entity)
        if not source_dir.is_dir():
            continue
        for path in sorted(source_dir.iterdir()):
            if not path.is_file():
                continue
            if not _is_misrouted_arxiv_stem(path.stem):
                continue
            # cache_dir only builds the path; the arXiv namespace may not
            # exist yet on a cache that has only ever seen manual imports.
            target_dir = cache.cache_dir(arxiv.NAMESPACE, entity)
            target = target_dir / path.name
            if target.exists():
                continue
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                path.rename(target)
                moved += 1
            except OSError:
                continue
    return moved


def _is_misrouted_arxiv_stem(stem: str) -> bool:
    """Whether a ``manual`` stem is an arXiv id stored in the wrong namespace.

    Inverts ``safe_stem`` — restore the slash, then percent-decode, the order
    ``cache_search._filename_to_canonical`` uses — and asks the router. Both
    with and without the slash repair, because it is not decidable from the
    stem alone: ``arxiv%3A2301.00001`` carries no slash while
    ``arxiv%3Ahep-th_9901001`` does.

    Asking the router rather than matching a shape here is the point: the
    criterion is exactly "``resolve_target`` would put this somewhere else
    now", so the migration cannot disagree with the routing it exists to
    catch up with.
    """
    return any(
        _is_arxiv_identifier(unquote(candidate)) for candidate in {stem, stem.replace("_", "/", 1)}
    )


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
    return papers.safe_stem(canonical) + ".pdf"


def _manual_pdf_path(canonical: str) -> Path:
    """PDF path in the manual namespace (fallback only)."""
    return cache.cache_dir(NAMESPACE, "pdfs") / _pdf_filename(canonical)


def pdf_path(identifier: str) -> Path:
    """Return the expected cache path for a PDF, routed to the correct provider."""
    return resolve_target(identifier)["pdf_path"]


def _invalidate_derived(namespace: str, canonical: str) -> None:
    """Drop cached markdown + section index for a paper.

    Mirrors the force_refresh cascade in ``tools/pipeline.py``: once the PDF
    is replaced, any previously-converted markdown and its section index are
    stale relative to the new bytes, so the next ``convert_paper`` must re-run
    rather than return previously-converted text.
    """
    md_path = papers.markdown_path(namespace, canonical)
    with contextlib.suppress(OSError):
        md_path.unlink(missing_ok=True)
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
    if not force_refresh:
        # Through cached_hit, not a local check-then-stat: it owns the stat,
        # so a concurrent unlink between the usability check and the size
        # read is a miss we re-import, not an OSError out of this function.
        hit = _pdf_download.cached_hit(dest)
        if hit is not None:
            return {
                "identifier": _normalize_identifier(identifier),
                "namespace": target["namespace"],
                **hit,
            }

    # Atomic copy: a crash / disk-full mid-copy can't leave a half-written
    # canonical PDF (which _pdf_download.is_usable_pdf would then reject).
    try:
        cache._atomic_copy(source, dest)
        # Inside the same try: the size read is part of landing the file, and
        # a concurrent unlink between the two must surface as this error, not
        # as an OSError out of the tool.
        size_bytes = dest.stat().st_size
    except OSError as e:
        # Same counter as cache.put's write failure: one row an operator can
        # read to see a full or read-only disk, whatever kind of write hit it.
        _stats.incr(target["namespace"], "cache_write_failures")
        return {"error": f"Could not copy {file_path} into the cache: {e}"}

    result: dict[str, Any] = {
        "identifier": _normalize_identifier(identifier),
        "namespace": target["namespace"],
        "path": str(dest),
        "size_bytes": size_bytes,
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
