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

    Both halves, always: dropping one leaves a reader matching a checksum
    against bytes that no longer exist. Best-effort, so a file that survives
    still loses its index. Caller must hold :func:`sections_lock`.
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

    **Invariant: this map is the only owner of a lock across an await.** Write
    ``async with sections_lock(...)`` as one expression — acquiring an
    uncontended lock never yields, which is what stops eviction racing a
    caller. Bind it, await, then enter, and the key can be evicted and
    recreated, handing two callers two different Locks.
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

    No ``exists()`` ahead of it: a concurrent ``drop_derived`` fits through the
    gap, and the answer is the same either way.
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
    """The entry for a converted paper, re-parsed if stale, or None if gone.

    **The caller MUST already hold the per-paper ``sections_lock``.**
    ``force_refresh`` drops the index but keeps ``conversion_mode``: the
    markdown is unchanged, so what converted it is too.
    """
    # Read before the invalidate, or force_refresh has no mode left to keep.
    # count=False: this served no upstream lookup (.claude/rules/cache.md).
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
            # Re-parse rather than default a legacy entry: a wrong
            # sections_detected reaches the agent as truth.
            and cached.get("sections_detected") is not None
        ):
            return cached

    # A re-parse is no new evidence about what converted the file.
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
    """Public sections accessor: :func:`_reparse_sections_locked` under the lock.

    ``None`` when the paper isn't converted.
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

    The single home for assembling an entry, so it can never be built with a
    key missing. ``mode`` is provenance: ``"full"`` / ``"fast"`` for converter
    output, ``"imported"`` for a file that never ran through one.

    Takes the markdown verbatim — post-processing is the caller's, since what
    is right for converter output is wrong for a file an operator wrote.
    """
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
