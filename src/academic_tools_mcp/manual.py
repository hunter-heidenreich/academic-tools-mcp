"""Import of local PDFs and pre-converted markdown, plus the two dispatchers.

An import is stored in the identifier's *own* provider namespace, so the native
pipeline tools find it with no duplicate; only an identifier no provider claims
falls back to ``manual``. ``resolve_target`` decides that, and
``resolve_metadata_source`` derives from it.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypedDict
from urllib.parse import unquote

from . import papers
from .download import streaming
from .net import stats
from .providers import acl, arxiv, biorxiv
from .store import atomic, cache, stems
from .util import doinorm

NAMESPACE = "manual"

MetadataSource = Literal["arxiv", "biorxiv", "openalex"]
RefileOutcome = Literal["moved", "linked"]

# Covers the `arXiv:` prefix, every abs/pdf URL host, and the `10.48550/arXiv.` DOI.
_ARXIV_MARKER = "arxiv"


class Target(TypedDict):
    """Where an identifier's PDF and markdown belong."""

    namespace: str
    canonical: str
    pdf_path: Path


# Provider routing — store in the right namespace automatically


class _Route(NamedTuple):
    """One provider's claim on an identifier, and where it files it."""

    claims: Callable[[str], bool]
    namespace: str
    canonical_key: Callable[[str], str]
    pdf_path: Callable[[str], Path]


# Ordered: an arXiv id is not a DOI, an ACL DOI is, so the generic fallback comes last.
_ROUTES = (
    _Route(arxiv.is_arxiv_id, arxiv.NAMESPACE, arxiv.canonical_arxiv_id, arxiv.pdf_path),
    _Route(
        acl.is_acl_doi,
        acl.NAMESPACE,
        acl.canonical_key,
        acl.pdf_path,
    ),
    _Route(biorxiv.is_biorxiv_doi, biorxiv.NAMESPACE, biorxiv.canonical_key, biorxiv.pdf_path),
)


def resolve_target(identifier: str) -> Target:
    """Detect the target provider from *identifier* and return routing info.

    Anything no provider claims falls back to ``manual``, keyed by
    ``doinorm.canonical`` of it — so a label is case-folded too.
    """
    normalized = doinorm.normalize(identifier)

    for route in _ROUTES:
        if route.claims(normalized):
            canonical = route.canonical_key(normalized)
            return Target(
                namespace=route.namespace,
                canonical=canonical,
                pdf_path=route.pdf_path(canonical),
            )

    canonical = doinorm.canonical(normalized)
    return Target(
        namespace=NAMESPACE,
        canonical=canonical,
        pdf_path=_manual_pdf_path(canonical),
    )


_METADATA_SOURCE_BY_NAMESPACE: dict[str, MetadataSource] = {
    arxiv.NAMESPACE: "arxiv",
    biorxiv.NAMESPACE: "biorxiv",
    acl.NAMESPACE: "openalex",
}


def resolve_metadata_source(identifier: str) -> MetadataSource | None:
    """Detect which provider should serve *metadata* for *identifier*.

    ``None`` when nothing claims it. Derived from :func:`resolve_target`, not a
    second pass over the shapes, so storage and metadata cannot disagree; only
    the ``manual`` fallback re-tests its key. ACL is the one namespace that
    changes hands — the Anthology has no metadata API.
    """
    target = resolve_target(identifier)

    if source := _METADATA_SOURCE_BY_NAMESPACE.get(target["namespace"]):
        return source

    return "openalex" if doinorm.looks_like_doi(target["canonical"]) else None


def migrate_misrouted_arxiv() -> int:
    """Re-file cached files that ``resolve_target`` now routes to ``arxiv``.

    Renames as it goes: the legacy ``manual`` key kept an ``arXiv:`` prefix the
    arXiv key drops. Idempotent and best-effort, once at startup. Returns files
    re-filed, linked as well as moved; only a moved markdown orphans its
    ``manual`` section index.
    """
    refiled = 0
    for entity in ("pdfs", "markdown"):
        target_dir = cache.cache_dir(arxiv.NAMESPACE, entity)
        for path in stems.list_dir(cache.cache_dir(NAMESPACE, entity)):
            outcome = _refile_misrouted_arxiv(path, target_dir)
            if outcome is None:
                continue
            refiled += 1
            if entity == "markdown" and outcome == "moved":
                cache.invalidate(NAMESPACE, "sections", stems.sections_key_for_stem(path.stem))
    return refiled


def _refile_misrouted_arxiv(path: Path, target_dir: Path) -> RefileOutcome | None:
    """Re-file one arXiv-shaped ``manual`` file into *target_dir*, under its arXiv stem.

    ``None`` for anything left where it is, which never raises — a skip is for
    the next run, and a filesystem without hard links takes that path.
    """
    if not path.is_file():
        return None

    claim = _misrouted_arxiv_id(path.stem)
    if claim is None:
        return None
    recovered, outcome = claim

    target = target_dir / (stems.safe_stem(recovered) + path.suffix)
    if target.exists():
        return None

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if outcome == "moved":
            path.rename(target)
        else:
            os.link(path, target)
    except OSError:
        return None
    return outcome


def _misrouted_arxiv_id(stem: str) -> tuple[str, RefileOutcome] | None:
    """The arXiv key a ``manual`` stem belongs under, and how to re-file it.

    Three candidates: a stem doesn't say which ``_`` were slashes. ``safe_stem``
    leaves a literal ``_`` alone, so ``is_arxiv_id`` cannot tell a restored
    slash from one — ``hep-th_9901001`` reads as both, and so does
    ``thesis_1234567``. Hence ``"linked"`` for a repair a label could have
    written, ``"moved"`` only for a stem that is exclusively arXiv's.
    Deliberately not ``corpus._filename_to_canonical``, whose anchored grammar
    cannot match the ``arXiv:`` prefix these stems carry.
    """
    names_arxiv = _ARXIV_MARKER in unquote(stem).lower()
    for repaired, candidate in enumerate((stem, stem.replace("_", "/", 1), stem.replace("_", "/"))):
        recovered = unquote(candidate)
        if arxiv.is_arxiv_id(recovered):
            exclusive = not repaired or names_arxiv
            return arxiv.canonical_arxiv_id(recovered), "moved" if exclusive else "linked"
    return None


# PDF storage


def _manual_pdf_path(canonical: str) -> Path:
    """PDF path in the manual namespace; folds first, like every provider's ``pdf_path``."""
    return stems.pdf_path(NAMESPACE, doinorm.canonical(canonical))


# Import argument checks — shared by both intake paths


def _identifier_error(identifier: str) -> dict[str, Any] | None:
    """Reject an identifier that normalizes to nothing — every blank shares one cache entry."""
    if not doinorm.normalize(identifier):
        return {
            "error": (
                f"Blank identifier: {identifier!r}. Pass the paper's DOI, arXiv ID, "
                "or a freeform label — it is the cache key the rest of the pipeline "
                "looks the file up by."
            )
        }
    return None


def _source_error(source: Path, file_path: str) -> dict[str, Any] | None:
    """Reject a missing or non-regular import source; unreadable is each caller's own error."""
    if not source.exists():
        return {"error": f"File not found: {file_path}"}
    if not source.is_file():
        return {"error": f"Not a file: {file_path}"}
    return None


# PDF import


def import_local_pdf(
    file_path: str, identifier: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Copy a local PDF into the cache, under the identifier's own namespace.

    Returns ``{identifier, namespace, path, size_bytes, cached}``, plus
    ``cascaded_invalidated`` when new bytes land (``existed or force_refresh``,
    dropping the derived markdown and sections), or ``{error}``. Caller holds
    ``papers.sections_lock`` for the routed ``(namespace, canonical)``.
    """
    if err := _identifier_error(identifier):
        return err

    source = Path(file_path).expanduser().resolve()
    if err := _source_error(source, file_path):
        return err

    # Not streaming.is_usable_pdf: an unopenable source earns its own error.
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
    namespace = target["namespace"]
    canonical = target["canonical"]
    dest = target["pdf_path"]

    existed = dest.exists()
    if not force_refresh:
        # cached_hit owns the stat, and the race it absorbs (download.md).
        hit = streaming.cached_hit(dest)
        if hit is not None:
            return {"identifier": canonical, "namespace": namespace, **hit}

    # Atomic: a crash mid-copy can't leave a half-written canonical PDF.
    try:
        atomic.copy(source, dest)
        # Inside the try: a concurrent unlink surfaces as this error, not an OSError.
        size_bytes = dest.stat().st_size
    except OSError as e:
        # cache.put's counter, so one row shows an operator any failed write.
        stats.incr(namespace, "cache_write_failures")
        return {"error": f"Could not copy {file_path} into the cache: {e}"}

    result: dict[str, Any] = {
        "identifier": canonical,
        "namespace": namespace,
        "path": str(dest),
        "size_bytes": size_bytes,
        "cached": False,
    }

    if existed or force_refresh:
        papers.drop_derived(namespace, canonical)
        result["cascaded_invalidated"] = ["markdown", "sections"]

    return result


# Markdown import


def import_markdown(
    file_path: str, identifier: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Copy a local markdown file into the cache, skipping download and conversion.

    Returns ``{identifier, namespace, markdown_path, sections, cached}`` or
    ``{error}``. Caller holds ``papers.sections_lock``: this replaces the
    markdown / section-index pair ``convert_pdf`` mutates under it.
    """
    if err := _identifier_error(identifier):
        return err

    source = Path(file_path).expanduser().resolve()
    if err := _source_error(source, file_path):
        return err

    target = resolve_target(identifier)
    namespace = target["namespace"]
    canonical = target["canonical"]
    md_path = stems.markdown_path(namespace, canonical)

    if not force_refresh and md_path.exists():
        return _cached_markdown(md_path, namespace, canonical, identifier)

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

    # Verbatim: ``_finalize_markdown``'s rewrites are wrong for a file whose links resolve.
    stored = papers.store_markdown_and_index(namespace, canonical, md_path, markdown, "imported")

    return {
        "identifier": canonical,
        "namespace": namespace,
        "markdown_path": str(md_path),
        "sections": stored["sections"],
        "cached": False,
    }


def _cached_markdown(
    md_path: Path, namespace: str, canonical: str, identifier: str
) -> dict[str, Any]:
    """Serve cached markdown, re-parsing rather than reading the index, which could disagree."""
    try:
        # Explicit UTF-8, as written: a locale-default read mis-decodes under LC_ALL=C.
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

    return {
        "identifier": canonical,
        "namespace": namespace,
        "markdown_path": str(md_path),
        "sections": papers.parse_sections(markdown),
        "cached": True,
    }
