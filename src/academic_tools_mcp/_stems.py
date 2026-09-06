"""Cache artifact naming: the one sanitizer every derived path routes through.

A canonical identifier becomes a filesystem-safe stem here and nowhere else, so
a paper's PDF, its converted markdown and its section-index key cannot disagree
about which file belongs to it. Kept below the conversion pipeline on purpose:
the providers need to name a PDF, not to run a converter.

Also home to the startup sweep that renames files written under the pre-
``safe_stem`` rules, and to the two checksum helpers the sections index uses to
tell "this markdown changed" from "this markdown is the one I parsed".
"""

import hashlib
import re
from pathlib import Path
from urllib.parse import quote

from . import cache

# Characters that survive a stem unencoded. ``.``/``-`` are kept so dotted DOIs
# and arXiv-style ids round-trip; everything else is percent-encoded.
_SAFE_STEM_KEEP = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")


def safe_stem(canonical: str) -> str:
    """Map a canonical id to a filesystem/shell-safe path component.

    The single sanitizer for every derived path — PDF, markdown, and the
    sections cache key — so the three can never disagree about which file
    belongs to which paper.

    Encoding rather than collapsing, because collapsing is lossy: ``"a b"`` and
    ``"a_b"`` would share one file and two papers would overwrite each other.
    ``/`` is the one exception, keeping its ``_`` mapping — it appears in every
    DOI and old-style arXiv id, and the residual ambiguity is unreachable for
    real identifiers.

    Invariant: one pass, so an encoded character is never re-encoded.
    """
    return "".join(
        ch if ch in _SAFE_STEM_KEEP else "_" if ch == "/" else quote(ch, safe="")
        for ch in canonical
    )


# Invariant: exactly ``safe_stem``'s output alphabet, so its own output is never
# seen as legacy and re-encoded (``a%20b`` -> ``a%2520b``). ``safe_stem`` is not
# idempotent; the migration is, and this gate is why. ``~`` belongs here though
# ``_SAFE_STEM_KEEP`` omits it: ``quote`` passes the RFC 3986 unreserved set.
_MIGRATED_STEM_RE = re.compile(r"\A[A-Za-z0-9._%~-]*\Z")


def _needs_stem_migration(stem: str) -> bool:
    """Whether ``stem`` was written under a pre-``safe_stem`` filename rule."""
    return not _MIGRATED_STEM_RE.match(stem)


# The two artifact kinds this sweep renames; anything else in these directories
# (a temp file mid-write, an editor backup) is left alone.
_MIGRATABLE_SUFFIXES = frozenset({".pdf", ".md"})


def migrate_legacy_stems() -> int:
    """Rename cached PDFs/markdown written under the old filename rules.

    Ordinary arXiv ids and DOIs are fixed points of ``safe_stem``, so most of
        an existing cache is already correct — but a DOI carrying parentheses
        (Elsevier PII style) or a freeform label with a space lands on a different
        name now and would otherwise be orphaned: the paper reports "not converted
        yet" and re-runs a conversion that can take tens of minutes.

        Idempotent and best-effort — a file that can't be renamed is left alone
        for the next run. Returns the number of files moved. Called once at
        server startup, alongside ``cache.gc_orphan_tmp_files``.

        The sections index is deliberately not migrated: its cache keys are
        hashed, so there is nothing to rename, and a missing index is re-derived
        from the markdown on the next read.
    """
    moved = 0
    root = cache.CACHE_ROOT
    if not root.is_dir():
        return 0
    for namespace_dir in root.iterdir():
        if not namespace_dir.is_dir():
            continue
        for entity in ("pdfs", "markdown"):
            entity_dir = namespace_dir / entity
            if not entity_dir.is_dir():
                continue
            for path in entity_dir.iterdir():
                if not path.is_file():
                    continue
                # Artifacts only. ``atomic._new_temp`` names an in-flight write
                # ``<dst.name>.<rand>.tmp``, whose stem still carries the
                # destination's legacy characters — renaming it makes the
                # writer's ``os.replace`` raise ``FileNotFoundError``.
                if path.suffix not in _MIGRATABLE_SUFFIXES:
                    continue
                if not _needs_stem_migration(path.stem):
                    continue
                target = path.with_name(safe_stem(path.stem) + path.suffix)
                if target.exists():
                    # Already migrated (or a genuine collision) — leave both
                    # in place rather than destroying data.
                    continue
                try:
                    path.rename(target)
                    moved += 1
                except OSError:
                    continue
    return moved


def markdown_checksum(md_path: Path) -> str:
    """SHA-256 hex digest of a markdown file, or ``""`` if it doesn't exist.

    Used for cache invalidation — if the markdown changes, sections must be
    re-parsed. A writer that already holds the text checksums it with
    :func:`checksum_text` instead.
    """
    if not md_path.exists():
        return ""
    return hashlib.sha256(md_path.read_bytes()).hexdigest()


def checksum_text(markdown: str) -> str:
    """SHA-256 hex digest of markdown held in memory.

    Invariant: agrees with :func:`markdown_checksum` of the file
    ``atomic.write_text`` writes from the same string — that writer pins
    ``newline=""`` so the bytes on disk are exactly the UTF-8 encoding.

    A writer must checksum the string it parsed, never re-read the file it just
    wrote: the two are separated by a window in which another writer can land,
    and an index stamped with the *other* document's checksum matches disk
    forever and so is never re-parsed.
    """
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def markdown_path(namespace: str, canonical: str) -> Path:
    """Return the cache path for converted markdown."""
    return cache.cache_dir(namespace, "markdown") / (safe_stem(canonical) + ".md")


def sections_key(canonical: str) -> str:
    """Cache key for section index JSON."""
    return safe_stem(canonical)


def sections_key_for_stem(stem: str) -> str:
    """The sections key for a paper already named by ``stem`` on disk.

    The identity, because a stored stem *is* ``safe_stem`` output — but it has
    a name so a caller never spells the coupling itself. ``safe_stem`` is not
    idempotent, so re-sanitizing a stem would encode its own ``%`` escapes and
    invalidate nothing.
    """
    return stem


def pdf_path(namespace: str, canonical: str) -> Path:
    """Return the cache path for a downloaded PDF.

    The sibling of :func:`markdown_path`. Every provider's own ``pdf_path``
    canonicalizes its identifier and then delegates here, so the naming rule
    lives in one place rather than once per provider.
    """
    return cache.cache_dir(namespace, "pdfs") / (safe_stem(canonical) + ".pdf")
