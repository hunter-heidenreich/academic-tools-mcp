"""The sections cache: read it, refresh it when the markdown drifted, drop it.

One JSON entry per paper, keyed by :func:`_stems.sections_key`, checksummed
against the markdown it describes so a manual edit is picked up on the next
read.

**Invariant: an entry carries all four of ``sections``, ``sections_detected``,
``markdown_checksum`` and ``conversion_mode``.** A missing ``sections_detected``
costs a re-parse; a wrong one is reported to the agent as truth. New writers go
through :func:`store_markdown_and_index`.

The per-paper ``sections_lock`` serialises everything that replaces the
markdown/index pair. Every unlinker holds it, so a reader that has it can trust
a successful read for the rest of its call.
"""

import asyncio
import contextlib
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .. import atomic, cache
from .._stems import checksum_text, markdown_path, sections_key
from .sections import parse_sections_and_detect


def drop_derived(namespace: str, canonical: str) -> None:
    """Drop a paper's converted markdown and its section index.

    The single home for the force_refresh cascade: whenever the PDF underneath
    is replaced, both halves are stale, and dropping only one leaves a reader
    matching a checksum against bytes that no longer exist.

    Caller must hold :func:`sections_lock` for the same paper — every unlinker
    of the markdown takes it. Best-effort: a file that can't be unlinked leaves
    the sections entry dropped anyway, so the next read re-parses.
    """
    with contextlib.suppress(OSError):
        markdown_path(namespace, canonical).unlink()
    cache.invalidate(namespace, "sections", sections_key(canonical))


# Per-paper locks, LRU-capped so a long session touching thousands of papers
# doesn't grow this map without bound.
_SECTION_LOCKS_MAX: int = 1024
_section_locks: "OrderedDict[tuple[str, str], asyncio.Lock]" = OrderedDict()


def sections_lock(namespace: str, canonical: str) -> asyncio.Lock:
    """Return the async lock guarding the sections cache for one paper.

    Nothing here awaits, so a caller cannot be interleaved mid-insert; the
    ``setdefault`` keeps that true for free if one ever could be.

    **Invariant: this map is the only owner of a lock across an await.** Every
    caller writes ``async with sections_lock(...)`` as one expression, and
    ``Lock.acquire`` on an uncontended lock returns without yielding — which is
    the whole reason eviction cannot race a caller. Hold the returned lock in a
    variable and await something before entering it and that argument is gone:
    the key can be evicted and recreated, handing two callers two Locks.
    """
    key = (namespace, canonical)
    lock = _section_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        existing = _section_locks.setdefault(key, lock)
        if existing is lock:
            # Evict oldest-first, rotating held locks and the just-added key to
            # the back. Bail once everything has been skipped once: going
            # slightly over cap is fine, spinning forever is not.
            held_skips = 0
            while len(_section_locks) > _SECTION_LOCKS_MAX:
                if held_skips >= len(_section_locks):
                    break
                evict_key, evict_lock = next(iter(_section_locks.items()))
                if evict_key == key or evict_lock.locked():
                    _section_locks.move_to_end(evict_key)
                    held_skips += 1
                    continue
                _section_locks.pop(evict_key, None)
                held_skips = 0
        else:
            lock = existing
    _section_locks.move_to_end(key)
    return lock


def _read_markdown(md_path: Path) -> str | None:
    """Read cached markdown as UTF-8, or ``None`` if it isn't there.

    One read, no ``exists()`` ahead of it: the check-then-read has a window a
    concurrent ``drop_derived`` fits through, and the answer is the same either
    way.
    """
    try:
        return md_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


async def _reparse_sections_locked(
    namespace: str,
    canonical: str,
    md_path: Path,
    *,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """Return the sections payload for a converted paper, re-parsing if stale.

    **The caller MUST already hold the per-paper ``sections_lock``.**

    Returns ``{sections, sections_detected, markdown_checksum, conversion_mode}``,
    or ``None`` when the markdown is missing — both "never converted" and the
    race where a cascade unlinks it under a caller that saw it a moment ago.

    ``force_refresh`` drops the index so the next read re-parses. It does not
    touch ``conversion_mode``: the markdown is unchanged, so what converted it
    is unchanged, and the recorded value is read before the entry goes.
    """
    # Read before any invalidate, so force_refresh can still recover the
    # recorded conversion_mode below. count=False: a sections read went to no
    # provider, so it is not a cache hit in the sense the counter means.
    cached = cache.get(namespace, "sections", sections_key(canonical), count=False)
    if force_refresh:
        cache.invalidate(namespace, "sections", sections_key(canonical))

    text = await asyncio.to_thread(_read_markdown, md_path)
    if text is None:
        return None
    current_checksum = checksum_text(text)

    if cached is not None and not force_refresh:
        stored_checksum = cached.get("markdown_checksum")
        if (
            stored_checksum is not None
            and stored_checksum == current_checksum
            and cached.get("sections") is not None
            # An entry predating ``sections_detected`` is re-parsed rather than
            # read with a guessed default. Re-parsing is a regex pass over text
            # already in hand — no subprocess, no network — so computing the
            # true answer is cheaper than the cost of reporting a wrong one,
            # which is an agent told a heading-free thesis "has one section".
            and cached.get("sections_detected") is not None
        ):
            return cached

    # Re-parse, preserving any recorded conversion_mode: a re-parse produces no
    # new evidence about what converted the file.
    recorded_mode = cached.get("conversion_mode") if cached is not None else None

    sections, detected = await asyncio.to_thread(parse_sections_and_detect, text)
    payload = {
        "sections": sections,
        "sections_detected": detected,
        "markdown_checksum": current_checksum,
        "conversion_mode": recorded_mode,
    }
    cache.put(namespace, "sections", sections_key(canonical), payload)
    return payload


async def get_or_parse_sections(
    namespace: str, canonical: str, *, force_refresh: bool = False
) -> dict[str, Any] | None:
    """Public sections accessor: read the cache, re-parsing when it drifted.

    Re-parses the markdown if the section index is missing or its checksum
    no longer matches. Acquires the per-paper ``sections_lock`` and delegates to
    ``_reparse_sections_locked``. Returns the sections payload
    (``{sections, sections_detected, markdown_checksum, conversion_mode}``) or
    ``None`` when the paper isn't converted (no markdown on disk). ``force_refresh=True`` drops
    the cached section index first so the next read re-parses.
    """
    md_path = markdown_path(namespace, canonical)
    async with sections_lock(namespace, canonical):
        return await _reparse_sections_locked(
            namespace, canonical, md_path, force_refresh=force_refresh
        )


def store_markdown_and_index(
    namespace: str,
    canonical: str,
    md_path: Path,
    markdown: str,
    mode: str,
) -> dict[str, Any]:
    """Write markdown to the cache and store its section index.

    The single home for assembling a sections-cache entry. Every writer routes
    through here — the two conversion modes via :func:`_finalize_markdown`, and
    ``manual.import_markdown`` directly — so the payload can never be assembled
    with a key missing.

    Invariant: an entry always carries all four of ``sections``,
    ``sections_detected``, ``markdown_checksum`` and ``conversion_mode``. A
    missing ``sections_detected`` costs a re-parse; a wrong one reaches the
    agent as truth — a heading-free paper reported as having one real section,
    the exact reading ``sections_note`` exists to prevent.

    ``mode`` is the provenance tag: ``"full"`` / ``"fast"`` for converter
    output, ``"imported"`` for a pre-converted file that never ran through one.

    Takes the markdown verbatim — post-processing belongs to the caller, since
    what is right for converter output (see :func:`_finalize_markdown`) is
    wrong for a file the operator wrote by hand.
    """
    # Atomic UTF-8 write: a crash mid-write can't leave a torn markdown file,
    # and non-ASCII content survives a non-UTF-8 host locale.
    atomic.write_text(md_path, markdown)

    sections, detected = parse_sections_and_detect(markdown)
    cache.put(
        namespace,
        "sections",
        sections_key(canonical),
        {
            "sections": sections,
            "sections_detected": detected,
            "markdown_checksum": checksum_text(markdown),
            "conversion_mode": mode,
        },
    )
    return {
        "markdown_path": str(md_path),
        "sections": sections,
        "sections_detected": detected,
        "cached": False,
        "conversion_mode": mode,
    }
