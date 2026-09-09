"""Cache artifact naming: the one sanitizer every derived path routes through.

A canonical identifier becomes a filesystem-safe stem here and nowhere else, so a
paper's PDF, its markdown and its section-index key cannot disagree about which file
belongs to it. Below the conversion pipeline, so a provider can name a PDF without
importing a converter.
"""

import hashlib
import re
from pathlib import Path
from urllib.parse import quote

from . import cache


def safe_stem(canonical: str) -> str:
    """Map a canonical id to a filesystem/shell-safe path component.

    Percent-encodes rather than collapses: collapsing is lossy, so ``"a b"`` and
    ``"a_b"`` would share one file and overwrite each other. ``/`` -> ``_`` is the one
    deliberate exception, so ordinary DOI filenames don't churn.

    Rewriting ``quote``'s ``%2F`` is exact, not a heuristic: a literal ``%`` is
    written ``%25``, so no other input produces that escape, and ``quote`` emits
    uppercase hex.
    """
    return quote(canonical, safe="").replace("%2F", "_")


# Invariant: exactly ``safe_stem``'s output alphabet — ``quote``'s unreserved set, plus ``%``.
_MIGRATED_STEM_RE = re.compile(r"\A[A-Za-z0-9._%~-]*\Z")


def _needs_stem_migration(stem: str) -> bool:
    """Whether ``stem`` was written under a pre-``safe_stem`` filename rule."""
    return not _MIGRATED_STEM_RE.match(stem)


_MIGRATABLE_SUFFIXES = frozenset({".pdf", ".md"})


def list_dir(path: Path) -> list[Path]:
    """Directory entries, sorted and materialised; ``[]`` for anything unwalkable.

    The listing every startup sweep walks. Materialised because a sweep renames files
    into the directory it is walking; never raises because the sweeps run inside the
    startup lifespan, where an unreadable cache directory would otherwise stop the server.
    """
    try:
        return sorted(path.iterdir())
    except OSError:
        return []


def migrate_legacy_stems() -> int:
    """Rename cached PDFs/markdown written under the old filename rules.

    Most of a cache is already correct — an ordinary arXiv id or DOI stem already
    lands inside ``_MIGRATED_STEM_RE``'s alphabet — so the cheap stem checks gate the
    stat, not the reverse. Returns the number of files moved; idempotent and
    best-effort, so a file it can't rename is left for the next run.

    The sections index is deliberately not migrated: its cache keys are hashed, so
    there is nothing to rename, and a missing index is re-derived from the markdown
    on the next read.
    """
    moved = 0
    for namespace_dir in list_dir(cache.CACHE_ROOT):
        for entity in ("pdfs", "markdown"):
            for path in list_dir(namespace_dir / entity):
                # A ``.tmp`` shares its destination's legacy stem; renaming breaks ``os.replace``.
                if path.suffix not in _MIGRATABLE_SUFFIXES:
                    continue
                if not _needs_stem_migration(path.stem):
                    continue
                if not path.is_file():
                    continue
                target = path.with_name(safe_stem(path.stem) + path.suffix)
                if target.exists():
                    # Already migrated, or a real collision — leave both rather than overwrite.
                    continue
                try:
                    path.rename(target)
                    moved += 1
                except OSError:
                    continue
    return moved


def sections_key(canonical: str) -> str:
    """Cache key for section index JSON."""
    return safe_stem(canonical)


def sections_key_for_stem(stem: str) -> str:
    """The sections key for a paper already named by ``stem`` on disk.

    The identity: a stored stem is already ``safe_stem`` output, which is not
    idempotent, so re-sanitizing would encode its own escapes and key an entry
    nobody wrote.
    """
    return stem


def markdown_path_for_stem(namespace: str, stem: str) -> Path:
    """Markdown path for a stem already on disk; as-is, per :func:`sections_key_for_stem`."""
    return cache.cache_dir(namespace, "markdown") / (stem + ".md")


def markdown_path(namespace: str, canonical: str) -> Path:
    """Return the cache path for converted markdown."""
    return markdown_path_for_stem(namespace, safe_stem(canonical))


def pdf_path(namespace: str, canonical: str) -> Path:
    """Return the cache path for a downloaded PDF.

    Every provider's own ``pdf_path`` canonicalizes its identifier and delegates
    here, so the naming rule has one home rather than one per provider.
    """
    return cache.cache_dir(namespace, "pdfs") / (safe_stem(canonical) + ".pdf")


def checksum_text(markdown: str) -> str:
    """The digest a writer stamps into the sections index.

    Must equal the digest of what ``atomic.write_text`` puts on disk for the
    same string — that writer pins ``newline=""``.
    """
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()
