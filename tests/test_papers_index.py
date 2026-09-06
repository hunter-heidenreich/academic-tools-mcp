"""The sections cache: the per-paper lock, and the entry writers.

Covers ``papers.index``. The invariant under most of it is that an entry never
describes a document other than the one whose checksum it carries.
"""

import asyncio
from collections import OrderedDict

import pytest

from academic_tools_mcp import cache, papers

from ._checksums import markdown_checksum


class TestSectionLocksLRU:
    """The per-paper section lock dict is bounded so a long-running
    session that touches thousands of papers doesn't accumulate Locks
    forever. Eviction is FIFO and skips currently-held locks.
    """

    @pytest.fixture(autouse=True)
    def _reset_locks(self, monkeypatch):

        monkeypatch.setattr(papers.index, "_section_locks", OrderedDict())

    def test_unbounded_below_cap(self, monkeypatch):
        monkeypatch.setattr(papers.index, "_SECTION_LOCKS_MAX", 100)
        for i in range(50):
            papers.sections_lock("test", f"paper-{i}")
        assert len(papers.index._section_locks) == 50

    def test_evicts_oldest_when_cap_exceeded(self, monkeypatch):
        monkeypatch.setattr(papers.index, "_SECTION_LOCKS_MAX", 5)
        for i in range(10):
            papers.sections_lock("test", f"paper-{i}")
        assert len(papers.index._section_locks) == 5
        # Newest five survive; oldest five evicted.
        survivors = set(papers.index._section_locks)
        assert survivors == {("test", f"paper-{i}") for i in range(5, 10)}

    def test_touch_promotes_to_end(self, monkeypatch):
        monkeypatch.setattr(papers.index, "_SECTION_LOCKS_MAX", 3)
        papers.sections_lock("test", "a")
        papers.sections_lock("test", "b")
        papers.sections_lock("test", "c")
        # Touch "a" so it moves to the end of the LRU order.
        papers.sections_lock("test", "a")
        # Adding "d" should now evict "b" (the new oldest), not "a".
        papers.sections_lock("test", "d")
        keys = list(papers.index._section_locks.keys())
        assert ("test", "b") not in keys
        assert ("test", "a") in keys

    @pytest.mark.asyncio
    async def test_held_lock_is_not_evicted(self, monkeypatch):
        # If the oldest lock is held when we try to evict, we skip it
        # and evict the next free one instead — dropping a held lock
        # would let a racing caller bypass mutual exclusion.
        monkeypatch.setattr(papers.index, "_SECTION_LOCKS_MAX", 2)
        held = papers.sections_lock("test", "held")
        await held.acquire()
        try:
            papers.sections_lock("test", "free-1")
            papers.sections_lock("test", "free-2")
            keys = set(papers.index._section_locks.keys())
            # "held" must still be present; one of the free ones got evicted.
            assert ("test", "held") in keys
        finally:
            held.release()

    @pytest.mark.asyncio
    async def test_all_locks_held_bails_over_cap(self, monkeypatch):
        # When every lock is held, eviction must bail (go over cap) rather
        # than spin — and must do so without dropping a held lock.
        monkeypatch.setattr(papers.index, "_SECTION_LOCKS_MAX", 2)
        a = papers.sections_lock("test", "a")
        b = papers.sections_lock("test", "b")
        await a.acquire()
        await b.acquire()
        try:
            papers.sections_lock("test", "c")  # over cap, but a/b are held
            keys = set(papers.index._section_locks.keys())
            assert ("test", "a") in keys
            assert ("test", "b") in keys
            assert ("test", "c") in keys  # added; nothing evictable
        finally:
            a.release()
            b.release()

    def test_returns_same_lock_for_same_key(self):
        lock1 = papers.sections_lock("test", "same")
        lock2 = papers.sections_lock("test", "same")
        assert lock1 is lock2


class TestSectionsLockRacingConstructor:
    """Two coroutines constructing the lock for one paper must end up with the
    same object — a second ``Lock`` would silently drop mutual exclusion.
    """

    def test_the_losing_constructor_returns_the_winners_lock(self, monkeypatch):

        winner = asyncio.Lock()

        class RacedMap(OrderedDict):
            def setdefault(self, key, default):
                # Stand in for a constructor that lost: the entry is already
                # there by the time this call lands.
                super().setdefault(key, winner)
                return super().__getitem__(key)

        monkeypatch.setattr(papers.index, "_section_locks", RacedMap())
        assert papers.sections_lock("ns", "c") is winner


class TestStoredChecksumDescribesTheStoredText:
    """The sections index must never describe a document other than the one
    whose checksum it carries.

    ``store_markdown_and_index`` used to re-read the file to checksum it, and
    ``convert_pdf``'s full mode calls it without holding ``sections_lock``
    (only the global conversion lock), while ``import_paper`` writes the same
    path *with* that lock. A write landing in the gap left an entry holding
    document X's sections under document Y's checksum — and since
    ``_reparse_sections_locked`` accepts any entry whose checksum matches disk,
    it was never re-parsed.
    """

    def test_checksum_comes_from_the_parsed_text_not_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        md_path = papers.markdown_path("test", "racy")

        ours = "## Ours\n\nour body\n"
        theirs = "## Theirs\n\ntheir body\n"

        real_write = papers.index.atomic.write_text

        def write_then_lose_the_race(path, payload):
            real_write(path, payload)
            # A concurrent writer lands between the write and the checksum.
            # Only the markdown — atomic.write_text is also the cache's writer.
            if path == md_path:
                real_write(path, theirs)

        monkeypatch.setattr(papers.index.atomic, "write_text", write_then_lose_the_race)
        stored = papers.store_markdown_and_index("test", "racy", md_path, ours, "full")

        entry = cache.get("test", "sections", papers.sections_key("racy"))
        assert [s["title"] for s in stored["sections"]] == ["Ours"]
        # The entry describes our text, so it must carry our checksum — not the
        # one on disk, which would make it match forever.
        assert entry["markdown_checksum"] == papers.checksum_text(ours)
        assert entry["markdown_checksum"] != markdown_checksum(md_path)

    def test_checksum_text_agrees_with_the_file_the_writer_wrote(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        md_path = papers.markdown_path("test", "agree")
        # Non-ASCII and every newline shape: atomic.write_text pins newline=""
        # so the bytes on disk are exactly the UTF-8 encoding of the payload.
        # A payload carrying \r\n or a bare \r is what distinguishes that pin
        # from a writer that translates line endings on the way out.
        text = "## Gutiérrez\r\n\r\nline one\nline two\rline three\n"
        papers.store_markdown_and_index("test", "agree", md_path, text, "full")
        assert papers.checksum_text(text) == markdown_checksum(md_path)

    @pytest.mark.asyncio
    async def test_a_mismatched_entry_self_heals_on_the_next_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        md_path = papers.markdown_path("test", "heal")
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("## Real\n\nreal body\n", encoding="utf-8")
        cache.put(
            "test",
            "sections",
            papers.sections_key("heal"),
            {
                "sections": [{"index": 0, "title": "Stale", "h3s": [], "approx_tokens": 1}],
                "sections_detected": True,
                "markdown_checksum": papers.checksum_text("## Other\n\nother\n"),
                "conversion_mode": "full",
            },
        )
        payload = await papers.get_or_parse_sections("test", "heal")
        assert [s["title"] for s in payload["sections"]] == ["Real"]


class TestGetOrParseSectionsForceRefresh:
    @pytest.mark.asyncio
    async def test_force_refresh_drops_the_index_so_the_next_read_reparses(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(cache, "CACHE_ROOT", tmp_path / "cache")
        md_path = papers.markdown_path("test", "forced")
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text("## Real\n\nbody\n", encoding="utf-8")

        # A stale entry whose checksum *matches* disk: without the drop it
        # would be served, because the checksum gate sees nothing wrong.
        cache.put(
            "test",
            "sections",
            papers.sections_key("forced"),
            {
                "sections": [{"index": 0, "title": "Stale", "h3s": [], "approx_tokens": 1}],
                "sections_detected": True,
                "markdown_checksum": papers.checksum_text("## Real\n\nbody\n"),
                "conversion_mode": "full",
            },
        )

        served = await papers.get_or_parse_sections("test", "forced")
        assert [s["title"] for s in served["sections"]] == ["Stale"]

        refreshed = await papers.get_or_parse_sections("test", "forced", force_refresh=True)
        assert [s["title"] for s in refreshed["sections"]] == ["Real"]
