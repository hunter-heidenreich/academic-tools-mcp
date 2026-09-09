"""The sections cache: read it, refresh it when the markdown drifted, drop it.

One JSON entry per paper, keyed by :func:`stems.sections_key` and checksummed
against the markdown it describes, so a manual edit shows up on the next read.

**Invariant: an entry carries all four of ``sections``, ``sections_detected``,
``markdown_checksum`` and ``conversion_mode``**, only the last nullable. A
missing ``sections`` or ``sections_detected`` re-parses; a wrong one reaches the
agent as truth. New writers go through :func:`store_markdown_and_index`.

The per-paper ``sections_lock`` serialises re-parses and every unlink of the
markdown/index pair. Every unlinker holds it, so a reader that has it can trust
a successful read for the rest of its call.
"""

import asyncio
import contextlib
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..store import atomic, cache
from ..store.stems import checksum_text, markdown_path, sections_key, sections_key_for_stem
from .sections import parse_sections_and_detect

# The four-key invariant above, as a value: a partial entry carried onto a new
# key would read there as complete.
_ENTRY_KEYS = frozenset({"sections", "sections_detected", "markdown_checksum", "conversion_mode"})


def drop_derived(namespace: str, canonical: str) -> None:
    """Drop a paper's converted markdown and its section index.

    Both halves, always: dropping one leaves a reader matching a checksum
    against bytes that no longer exist. Best-effort, so a file that survives
    still loses its index. Caller must hold :func:`sections_lock`.
    """
    with contextlib.suppress(OSError):
        markdown_path(namespace, canonical).unlink()
    cache.invalidate(namespace, "sections", sections_key(canonical))


def recorded_conversion_mode(namespace: str, canonical: str) -> str | None:
    """Provenance of this paper's cached markdown, or ``None`` if unrecorded.

    ``"full"`` / ``"fast"`` for converter output, ``"imported"`` for a file an
    operator handed to ``import_paper``, ``None`` for an entry predating the
    field or no entry at all — including an index deleted by hand: nothing
    recorded, nothing to protect. The named read for callers deciding whether
    they may replace the markdown — ``tools/pipeline``'s download cascade is
    the one, and it must not reach into the sections cache itself. Caller holds
    :func:`sections_lock`, as :func:`drop_derived` requires.
    """
    entry = cache.get(namespace, "sections", sections_key(canonical), count=False)
    if entry is None:
        return None
    mode = entry.get("conversion_mode")
    return mode if isinstance(mode, str) else None


def rekey_sections(
    src_namespace: str, src_stem: str, dst_namespace: str, dst_canonical: str
) -> bool:
    """Carry a section index entry onto a re-filed paper's new key, verbatim.

    The key changed, the bytes did not. Re-deriving would reset
    ``conversion_mode`` to ``None``, losing the ``"imported"`` marker that keeps
    an operator's own markdown out of ``tools/pipeline``'s download cascade.

    Not a third assembler: it copies a *complete* entry, only onto an empty
    destination. Keyed by the source *stem*, which is what the caller found on
    disk. Caller holds the destination's :func:`sections_lock`.
    """
    entry = cache.get(src_namespace, "sections", sections_key_for_stem(src_stem), count=False)
    if entry is None or not entry.keys() >= _ENTRY_KEYS:
        return False

    dst_key = sections_key(dst_canonical)
    if cache.get(dst_namespace, "sections", dst_key, count=False) is not None:
        return False

    cache.put(dst_namespace, "sections", dst_key, dict(entry))
    return True


# Per-paper locks, LRU-capped so a long session can't grow this map unbounded.
_SECTION_LOCKS_MAX: int = 1024
_section_locks: "OrderedDict[tuple[str, str], asyncio.Lock]" = OrderedDict()


def sections_lock(namespace: str, canonical: str) -> asyncio.Lock:
    """Return the async lock guarding one paper's markdown/section-index pair.

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
            # Held locks rotate to the back, never evicted: mutual exclusion
            # can't be dropped under a writer. Over cap beats spinning forever.
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
    # count=False: this served no upstream lookup.
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
            # Re-parse rather than default: a guess would reach the agent as truth.
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

    The entry writer every other module goes through, so an entry can never be
    built with a key missing (``_reparse_sections_locked`` is the only other
    assembler). ``mode`` is provenance: ``"full"`` / ``"fast"`` for converter
    output, ``"imported"`` for a file that never ran through one. Takes the
    markdown verbatim — post-processing is the caller's, since what is right for
    converter output is wrong for a file an operator wrote.
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
