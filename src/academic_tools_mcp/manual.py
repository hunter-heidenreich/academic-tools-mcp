"""Import of local PDFs and pre-converted markdown, plus the two dispatchers.

An import is keyed by a user-supplied identifier and stored in **that
identifier's** provider namespace (arXiv, bioRxiv/medRxiv, ACL Anthology), so
the native pipeline tools find the file with no duplicate; only an identifier
no provider claims falls back to ``manual``. ``resolve_target`` decides that
for storage and ``resolve_metadata_source`` for metadata, both off one shape
test — every paper tool routes through one of them.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NamedTuple, TypedDict
from urllib.parse import unquote

from . import _doi, _pdf_download, _stats, _stems, atomic, cache, papers
from .providers import acl_anthology, arxiv, biorxiv

NAMESPACE = "manual"

MetadataSource = Literal["arxiv", "biorxiv", "openalex"]


class Target(TypedDict):
    """Where an identifier's PDF and markdown belong."""

    namespace: str
    canonical: str
    pdf_path: Path


# ---------------------------------------------------------------------------
# Provider routing — store in the right namespace automatically
# ---------------------------------------------------------------------------


class _Route(NamedTuple):
    """One provider's claim on an identifier, and where it files it."""

    claims: Callable[[str], bool]
    namespace: str
    canonical_key: Callable[[str], str]
    pdf_path: Callable[[str], Path]


# Ordered, and it must stay ordered: an arXiv id is not a DOI, and an ACL DOI
# is a DOI, so the generic-DOI fallback can only come last.
_ROUTES = (
    _Route(arxiv.is_arxiv_id, arxiv.NAMESPACE, arxiv.canonical_arxiv_id, arxiv.pdf_path),
    _Route(
        acl_anthology.is_acl_doi,
        acl_anthology.NAMESPACE,
        acl_anthology.canonical_key,
        acl_anthology.pdf_path,
    ),
    _Route(biorxiv.is_biorxiv_doi, biorxiv.NAMESPACE, biorxiv.canonical_key, biorxiv.pdf_path),
)


def resolve_target(identifier: str) -> Target:
    """Detect the target provider from *identifier* and return routing info.

    An identifier no provider claims falls back to the ``manual`` namespace,
    keyed by its bare DOI or, for a freeform label, by the label itself.
    """
    normalized = _doi.normalize(identifier)

    for route in _ROUTES:
        if route.claims(normalized):
            canonical = route.canonical_key(normalized)
            return Target(
                namespace=route.namespace,
                canonical=canonical,
                pdf_path=route.pdf_path(canonical),
            )

    canonical = _doi.canonical(normalized)
    return Target(
        namespace=NAMESPACE,
        canonical=canonical,
        pdf_path=_manual_pdf_path(canonical),
    )


_METADATA_SOURCE_BY_NAMESPACE: dict[str, MetadataSource] = {
    arxiv.NAMESPACE: "arxiv",
    biorxiv.NAMESPACE: "biorxiv",
    acl_anthology.NAMESPACE: "openalex",
}


def resolve_metadata_source(identifier: str) -> MetadataSource | None:
    """Detect which provider should serve *metadata* for *identifier*.

    ``None`` when nothing claims it (a freeform label). Derived from
    :func:`resolve_target` rather than re-testing the shapes, so storage and
    metadata cannot disagree about which identifiers are arXiv's.

    Where the two differ is the mapping: ACL DOIs, and any other DOI shape,
    route to OpenAlex — ACL Anthology has no metadata API of its own, and
    OpenAlex handles arbitrary publisher DOIs.
    """
    target = resolve_target(identifier)

    if source := _METADATA_SOURCE_BY_NAMESPACE.get(target["namespace"]):
        return source

    return "openalex" if _doi.looks_like_doi(target["canonical"]) else None


def migrate_misrouted_arxiv() -> int:
    """Re-file cached files that ``resolve_target`` now routes to ``arxiv``.

    Renames as it moves: a ``manual`` key kept the ``arXiv:`` prefix that the
    arXiv key drops, so reusing the source name would file
    ``arxiv%3A2301.00001`` where only ``2301.00001`` is ever looked up.

    Run once at startup, idempotent and best-effort like
    ``papers.migrate_legacy_stems``. Returns the number of files moved.
    """
    moved = 0
    for entity in ("pdfs", "markdown"):
        source_dir = cache.cache_dir(NAMESPACE, entity)
        if not source_dir.is_dir():
            continue
        target_dir = cache.cache_dir(arxiv.NAMESPACE, entity)
        # Materialised: the loop renames files out of the directory it walks.
        for path in sorted(source_dir.iterdir()):
            if not _refile_misrouted_arxiv(path, target_dir):
                continue
            moved += 1
            if entity == "markdown":
                # The stem is already ``safe_stem`` output, so the key is
                # derived from it rather than re-sanitized: ``safe_stem`` is
                # not idempotent and would re-encode its own escapes.
                cache.invalidate(NAMESPACE, "sections", _stems.sections_key_for_stem(path.stem))
    return moved


def _refile_misrouted_arxiv(path: Path, target_dir: Path) -> bool:
    """Move one arXiv-shaped ``manual`` file into *target_dir*, under its arXiv stem.

    False for anything left where it is, which never raises — a skip is for the
    next run.
    """
    if not path.is_file():
        return False

    recovered = _misrouted_arxiv_id(path.stem)
    if recovered is None:
        return False

    target = target_dir / (papers.safe_stem(recovered) + path.suffix)
    if target.exists():
        return False

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path.rename(target)
    except OSError:
        return False
    return True


def _misrouted_arxiv_id(stem: str) -> str | None:
    """The arXiv key a ``manual`` stem belongs under, or None if it is manual's.

    Both candidates, because the stem alone doesn't say whether an ``_`` was a
    slash: ``arxiv%3A2301.00001`` carries none, ``arxiv%3Ahep-th_9901001``
    does. Repair then decode is the order ``cache_search`` inverts stems in.

    Deliberately *not* ``cache_search._filename_to_canonical``, despite being
    the same shape of operation. That one repairs the slash with each
    namespace's own anchored grammar, which is right for a stem that namespace
    wrote — and wrong here: these stems were written under the legacy
    ``manual`` key rule, which keeps an ``arXiv:`` prefix that
    ``_ARXIV_OLDSTYLE_STEM_RE`` (``^archive_number$``) can never match. Sharing
    the grammar makes the sweep miss the prefixed spellings it exists for.
    """
    for candidate in (stem, stem.replace("_", "/", 1)):
        recovered = unquote(candidate)
        if arxiv.is_arxiv_id(recovered):
            return arxiv.canonical_arxiv_id(recovered)
    return None


# ---------------------------------------------------------------------------
# PDF storage
# ---------------------------------------------------------------------------


def _manual_pdf_path(canonical: str) -> Path:
    """PDF path in the manual namespace (fallback only).

    Folds its argument first, like every provider's ``pdf_path``, so a raw
    spelling can't build a path the cache never writes.
    """
    return _stems.pdf_path(NAMESPACE, _doi.canonical(canonical))


# ---------------------------------------------------------------------------
# Import argument checks — shared by both intake paths
# ---------------------------------------------------------------------------


def _identifier_error(identifier: str) -> dict[str, Any] | None:
    """Reject an identifier that normalizes to nothing, else None.

    The empty key stems to ``""``, so every blank import shares one entry.
    """
    if not _doi.normalize(identifier):
        return {
            "error": (
                f"Blank identifier: {identifier!r}. Pass the paper's DOI, arXiv ID, "
                "or a freeform label — it is the cache key the rest of the pipeline "
                "looks the file up by."
            )
        }
    return None


def _source_error(source: Path, file_path: str) -> dict[str, Any] | None:
    """Reject an import source that is missing or not a regular file, else None.

    Not a readability check — a file that can't be opened surfaces as the read
    error each caller returns.
    """
    if not source.exists():
        return {"error": f"File not found: {file_path}"}
    if not source.is_file():
        return {"error": f"Not a file: {file_path}"}
    return None


# ---------------------------------------------------------------------------
# PDF import
# ---------------------------------------------------------------------------


def import_local_pdf(
    file_path: str, identifier: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Copy a local PDF into the cache, under the identifier's own namespace.

    ``force_refresh`` replaces an already-cached PDF instead of returning it as
    ``cached``. Landing a PDF over a previous one cascades either way, dropping
    the derived markdown and sections so the next ``convert_paper`` re-runs.

    Returns ``{identifier, namespace, path, size_bytes, cached}`` or
    ``{error}``. Caller must hold ``papers.sections_lock`` for the routed
    ``(namespace, canonical)``.
    """
    if err := _identifier_error(identifier):
        return err

    source = Path(file_path).expanduser().resolve()
    if err := _source_error(source, file_path):
        return err

    # Not _pdf_download.is_usable_pdf: an unopenable source earns its own error.
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
        # cached_hit owns the stat, and the race it absorbs (pdf-download.md).
        hit = _pdf_download.cached_hit(dest)
        if hit is not None:
            return {"identifier": canonical, "namespace": namespace, **hit}

    # Atomic: a crash mid-copy can't leave a half-written canonical PDF.
    try:
        atomic.copy(source, dest)
        # Inside the try: a concurrent unlink must surface as this error, not
        # as an OSError out of the tool.
        size_bytes = dest.stat().st_size
    except OSError as e:
        # cache.put's counter, so one row shows an operator any failed write.
        _stats.incr(namespace, "cache_write_failures")
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


# ---------------------------------------------------------------------------
# Markdown import
# ---------------------------------------------------------------------------


def import_markdown(
    file_path: str, identifier: str, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Copy a local markdown file into the cache, skipping download and conversion.

    ``force_refresh`` replaces already-cached markdown instead of returning it
    as ``cached``, re-parsing the section index from the new file.

    Returns ``{identifier, namespace, markdown_path, sections, cached}`` or
    ``{error}``. Caller must hold ``papers.sections_lock`` for the routed
    ``(namespace, canonical)`` — this replaces the markdown / section-index
    pair ``convert_pdf`` mutates under it.
    """
    if err := _identifier_error(identifier):
        return err

    source = Path(file_path).expanduser().resolve()
    if err := _source_error(source, file_path):
        return err

    target = resolve_target(identifier)
    namespace = target["namespace"]
    canonical = target["canonical"]
    md_path = papers.markdown_path(namespace, canonical)

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

    # Verbatim: ``_finalize_markdown``'s rstrip and image rewrite are right for
    # converter output and wrong for an operator's own file, whose links resolve.
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
    """Serve markdown already in the cache, re-parsing its sections.

    A re-parse rather than a read of the section index: it cannot disagree with
    what a reader would compute, and it keeps this independent of cache state.
    """
    try:
        # Explicit UTF-8, as it was written: a locale-default read mis-decodes
        # non-ASCII under a non-UTF-8 host locale.
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
