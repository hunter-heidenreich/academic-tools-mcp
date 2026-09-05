"""BM25 keyword search over the converted-markdown cache.

The PDF pipeline (download_pdf → convert_paper) lands every paper's
markdown under ``.cache/<namespace>/markdown/<canonical>.md``. After
weeks of use this becomes the agent's actual reading history — but
without a search primitive, recovering "the paper that mentioned X"
means remembering the exact identifier.

Ranking is SQLite FTS5's built-in BM25. For the top hits this module
re-reads the file to extract the document title (first H1/H2), a
~200-char snippet centred on the highest-scoring matching term, and the
section the snippet falls under, so the agent can chain into
``get_paper_section`` by index.

The index (``.cache/__search_index__/index.db``) is **contentless**
(``content=''``): it stores postings, never the text. The markdown is
already on disk and the winners are re-read for snippets anyway, so
storing it twice would be waste — this is what makes the database
smaller than the JSON index it replaced, not larger.

Refresh is a per-document upsert keyed on ``(mtime_ns, size)``: the corpus
walk remains, but only changed files are re-read and re-indexed. (The
measurements behind the move off the previous JSON index are in
``CHANGELOG.md`` — transcribing them here only gave two copies to disagree.)

Two FTS tables, not one: ``normalize`` is a query-time flag in this API
while diacritic folding is a build-time tokenizer option in FTS5, so the
index carries a folded and an un-folded table and the flag selects
between them. That preserves the parameter's exact meaning rather than
silently redefining it, and both tables together are still well under
the old single JSON file.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from . import _textnorm, cache, papers

# Default size of the snippet window centred on the best-scoring term
# match. ~200 chars is enough to disambiguate ("variational dropout" vs
# "dropout regularisation") without bloating the response.
_SNIPPET_CHARS = 200

# Hard cap on returned hits so a noisy query can't pull the whole corpus
# back in one tool call.
_MAX_TOP_K = 50

# Tokenisation: split on anything that isn't a letter, digit, or
# intra-word hyphen / dot (so "BM25" survives, "self-attention" stays
# one token, "1.5x" stays one token, but "(end)" / "[1]" don't pollute
# the index). All lowercased.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-.]*[a-z0-9]|[a-z0-9]")

# Lightweight English stopword set. Tiny on purpose — academic prose is
# already terse, and stripping too aggressively hurts recall on phrasal
# queries like "in distribution shift". The list is the standard NLTK
# top-25 minus terms that show up as content in this domain ("not",
# "no", "very", "all").
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "we",
        "our",
        "their",
        "them",
        "they",
        "he",
        "she",
        "his",
        "her",
        "i",
        "you",
        "your",
        "if",
        "then",
        "than",
        "so",
        "such",
        "into",
        "about",
        "over",
        "under",
        "between",
    }
)

# Heading regex matching the same shape used by papers.parse_sections.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

# Persistent index. Lives in a reserved double-underscore namespace dir
# that _iter_markdown_files naturally skips (it has no ``markdown/``
# subdir). Bump _INDEX_VERSION to force a full rebuild when the entry
# schema or tokeniser changes. The lock serialises the load→refresh→save
# critical section across the worker threads that search() runs in (it is
# dispatched via asyncio.to_thread at the tool layer); BM25 scoring and
# snippet extraction run lock-free on the freshly-parsed per-call dict.
# 2: the ``unindexable`` probe stopped being ASCII-biased. Rows written by
# version 1 can carry a false ``no_indexable_tokens`` flag on a perfectly
# indexable non-Latin paper, and the ``(mtime, size)`` signal would never
# recompute them, so the mismatch rebuilds rather than lingering.
_SCHEMA_VERSION = 2
_INDEX_DIRNAME = "__search_index__"
_DB_FILENAME = "index.db"
_INDEX_LOCK = threading.Lock()
# NUL can't occur in a namespace dir name or a filename stem, so it is a
# collision-free separator for the flat entry key.


# A character ``unicode61`` will treat as part of a token: any Unicode letter
# or digit. ``\w`` minus underscore, so this is script-agnostic where a
# ``[a-z0-9]`` class would not be. Compiled and searched rather than scanned in
# Python (``any(ch.isalnum() ...)`` is a per-character interpreter loop, which
# is real cost on a 1 MB thesis); ``search`` short-circuits on the first hit.
_ALNUM_RE = re.compile(r"[^\W_]")


def _tokenize(text: str, *, normalize: bool = False) -> list[str]:
    """Lowercase, drop stopwords, return a list of content tokens.

    Preserves intra-word hyphens and dots so domain terms like
    ``self-attention`` and ``BM25`` survive intact.

    With ``normalize=True``, NFKD-fold and strip combining marks first so
    "café" and "cafe" tokenise identically. Must be applied to BOTH the
    query and the documents or the BM25 vocabulary won't align.
    """
    if normalize:
        text = _textnorm.fold(text)
    return [
        tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS and len(tok) > 1
    ]


def _extract_title(markdown: str) -> str | None:
    """Return the first H1 or H2 in the document, or ``None``.

    Converters disagree on whether to use H1 or H2 for the paper title,
    so we accept either. The "Preamble" / "Abstract" prefix that some
    papers carry is preserved — the caller can re-rank if needed.
    """
    for match in _HEADING_RE.finditer(markdown):
        level = len(match.group(1))
        if level <= 2:
            return match.group(2).strip()
    return None


def _section_for_offset(markdown: str, offset: int) -> tuple[int | None, str | None]:
    """Return ``(section_index, title)`` for a character offset.

    Delegates to ``papers.section_at_offset`` so this agrees with what
    ``get_paper_section`` will actually accept. A local reimplementation drifts
    in two agent-visible ways: without the empty-section filter it names a
    section the reader's index has dropped, and returning a *title* instead of
    an index hits "Ambiguous section title" whenever a paper repeats a heading
    — 10.9% of a real corpus.
    """
    found = papers.section_at_offset(markdown, offset)
    if found is None:
        return None, None
    return found


def _extract_snippet(
    markdown: str,
    query_terms: set[str],
    window: int = _SNIPPET_CHARS,
    *,
    normalize: bool = False,
) -> tuple[str, int | None]:
    """Return ``(snippet, char_offset)`` for the best matching position.

    "Best matching" = the position with the most distinct query terms
    in the surrounding window (so we prefer "variational dropout"
    cooccurrence over a lone "dropout"). Falls back to the document
    head if no term appears at all.

    With ``normalize=True`` the ``query_terms`` are already diacritic-
    folded (by ``_tokenize(query, normalize=True)``), so we locate them
    against a folded copy of the markdown but map every hit back to an
    ORIGINAL offset, keeping ``char_offset`` and the snippet slice
    aligned with the un-folded text.
    """
    if not query_terms:
        return markdown[:window].strip(), 0

    # Find every occurrence of every query term, collecting (offset, term).
    # Word-boundary match so "drop" doesn't hit inside "dropout".
    #
    # Both modes locate against a transformed copy of the markdown but report
    # ORIGINAL offsets via index_map. The map is required even when
    # normalize=False: str.lower() is not length-preserving (U+0130 'İ' →
    # 'i' + combining dot), so a raw m.start() would drift past the real match
    # for any doc containing such a char before the hit.
    hits: list[tuple[int, str]] = []
    lowered, index_map = _textnorm.lower_with_map(markdown, fold=normalize)
    for term in query_terms:
        # Escape regex metacharacters in the term itself.
        pattern = re.compile(rf"\b{re.escape(term)}\b")
        for m in pattern.finditer(lowered):
            hits.append((index_map[m.start()], term))

    if not hits:
        return markdown[:window].strip(), None

    # Score each hit by counting distinct query terms within ±window/2
    # chars. Sort hits by offset so each window scan only walks nearby hits.
    hits.sort()
    half = window // 2
    best_offset = hits[0][0]
    best_distinct = 1
    # For each hit, count distinct terms in [hit - half, hit + half] by
    # walking neighbours outward until they leave the window. This is
    # O(hits × hits-in-window), worst-case quadratic when one term repeats
    # densely within a single window — bounded in practice only because at
    # most top_k winners are ever passed here.
    for i, (off, _term) in enumerate(hits):
        lo = off - half
        hi = off + half
        terms_in_window: set[str] = set()
        # Walk neighbours in both directions until they leave the window
        for j in range(i, len(hits)):
            if hits[j][0] > hi:
                break
            terms_in_window.add(hits[j][1])
        for j in range(i - 1, -1, -1):
            if hits[j][0] < lo:
                break
            terms_in_window.add(hits[j][1])
        if len(terms_in_window) > best_distinct:
            best_distinct = len(terms_in_window)
            best_offset = off

    start = max(0, best_offset - half)
    end = min(len(markdown), start + window)
    snippet = markdown[start:end].strip()
    # Collapse internal whitespace so a snippet that crosses a heading
    # boundary doesn't render as "## Methods\n\n\n\nWe trained...".
    snippet = re.sub(r"\s+", " ", snippet)
    return snippet, best_offset


# ---------------------------------------------------------------------------
# Filename → identifier inversion per namespace
# ---------------------------------------------------------------------------

# Each namespace stores markdown at canonical.replace("/", "_") + ".md".
# Inverting that is namespace-specific because DOI suffixes can legitimately
# contain underscores; we can only safely restore the slashes that the
# known prefix introduced.
_NAMESPACE_PREFIX_REPAIRS: dict[str, list[tuple[str, str]]] = {
    # bioRxiv DOIs are always "10.1101/<suffix>" — exactly one slash.
    "biorxiv": [("10.1101_", "10.1101/")],
    # ACL Anthology DOIs are always "10.18653/v1/<suffix>" — two slashes.
    "acl_anthology": [("10.18653_v1_", "10.18653/v1/")],
    # Manual canonical IDs are arbitrary user input. We don't try to
    # restore slashes here — the agent-visible identifier is whatever
    # the user originally passed to import_paper, which was already
    # lowercased by manual._canonical_key, so slash-bearing inputs
    # will round-trip imperfectly. Calling get_paper_metadata on a
    # manual identifier doesn't dispatch anywhere anyway, so the
    # imperfection costs nothing in practice.
    "manual": [],
}

# Old-style arXiv IDs carry exactly one slash: "archive[.subject]/NNNNNNN"
# (e.g. "hep-th/9901001", "cs/0501001", "math.GT/0309136"). canonical_arxiv_id
# lowercases them and keeps the slash, then the storage step turns it into "_",
# so the stem is "archive[.subject]_NNNNNNN". This regex inverts ALL archives
# (not just the hyphenated physics ones) in one shot. New-style IDs start with a
# digit ("2301.00001") and never match, so they pass through untouched.
_ARXIV_OLDSTYLE_STEM_RE = re.compile(r"^([a-z][a-z.\-]*)_(\d{7})$")


def _filename_to_canonical(namespace: str, stem: str) -> str:
    """Invert ``canonical.replace("/", "_")`` for the given namespace.

    ``stem`` is the filename without the ``.md`` extension. Returns the
    canonical form the original code would have used as a cache key.
    """
    if namespace == "arxiv":
        m = _ARXIV_OLDSTYLE_STEM_RE.match(stem)
        return f"{m.group(1)}/{m.group(2)}" if m else stem
    repairs = _NAMESPACE_PREFIX_REPAIRS.get(namespace, [])
    for needle, replacement in repairs:
        if stem.startswith(needle):
            return replacement + stem[len(needle) :]
    return stem


# ---------------------------------------------------------------------------
# BM25 search
# ---------------------------------------------------------------------------


def _iter_markdown_files(
    namespace: str | None = None,
) -> list[tuple[str, Path]]:
    """Yield ``(namespace, path)`` for every cached markdown file.

    A single-namespace filter is honoured so an agent that knows it
    only cares about, say, manual imports doesn't pay to score the
    whole corpus.
    """
    root = cache._CACHE_ROOT
    if not root.is_dir():
        return []
    namespaces = [namespace] if namespace else None
    out: list[tuple[str, Path]] = []
    for ns_dir in sorted(root.iterdir()):
        if not ns_dir.is_dir():
            continue
        if namespaces is not None and ns_dir.name not in namespaces:
            continue
        md_dir = ns_dir / "markdown"
        if not md_dir.is_dir():
            continue
        for md_path in sorted(md_dir.glob("*.md")):
            out.append((ns_dir.name, md_path))
    return out


def _scan_markdown(namespace: str | None = None) -> list[tuple[str, Path, int, int]]:
    """Like ``_iter_markdown_files`` but carries each file's stat inline.

    ``os.scandir`` gets the directory entry and its stat in one pass, where
    ``glob`` + a separate ``Path.stat()`` per file costs two. On a
    3,700-paper corpus that walk was 45% of a query's wall time; the refresh
    needs the stat anyway, so there is no reason to fetch it twice.

    Order is not guaranteed — the refresh keys on ``(ns, stem)``, not
    position. ``_iter_markdown_files`` keeps its sorted contract for callers
    that do care.
    """
    root = cache._CACHE_ROOT
    if not root.is_dir():
        return []
    out: list[tuple[str, Path, int, int]] = []
    try:
        namespace_entries = list(os.scandir(root))
    except OSError:
        return []
    for ns_entry in namespace_entries:
        if not ns_entry.is_dir() or (namespace is not None and ns_entry.name != namespace):
            continue
        md_dir = Path(ns_entry.path) / "markdown"
        try:
            entries = list(os.scandir(md_dir))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".md"):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            out.append((ns_entry.name, Path(entry.path), st.st_mtime_ns, st.st_size))
    return out


# ---------------------------------------------------------------------------
# Persistent incremental index
# ---------------------------------------------------------------------------


def _index_path() -> Path:
    """Path to the SQLite index database."""
    return cache._CACHE_ROOT / _INDEX_DIRNAME / _DB_FILENAME


def _legacy_index_path() -> Path:
    """Path to the JSON index this replaced, so it can be swept away."""
    return cache._CACHE_ROOT / _INDEX_DIRNAME / "index.json"


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS files (
           rowid     INTEGER PRIMARY KEY,
           ns        TEXT    NOT NULL,
           stem      TEXT    NOT NULL,
           mtime_ns  INTEGER NOT NULL,
           size      INTEGER NOT NULL,
           unindexable TEXT,
           UNIQUE (ns, stem)
       )""",
    "CREATE INDEX IF NOT EXISTS files_ns ON files(ns)",
    # Contentless (content=''): the index stores postings, never the text.
    # The markdown is already on disk and the top-k winners are re-read for
    # snippets anyway, so storing it twice would be pure waste — this is what
    # makes the database *smaller* than the JSON it replaces rather than
    # larger. contentless_delete=1 (SQLite 3.43+) lets a removed paper be
    # DELETEd without handing back its original text.
    #
    # Two tables rather than one because ``normalize`` is a query-time flag in
    # this API but diacritic folding is a *build-time* tokenizer option in
    # FTS5. Keeping both preserves the parameter's exact meaning instead of
    # silently redefining it.
    """CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
           body, content='', contentless_delete=1,
           tokenize="unicode61 remove_diacritics 0"
       )""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS fts_norm USING fts5(
           body, content='', contentless_delete=1,
           tokenize="unicode61 remove_diacritics 2"
       )""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def _connect() -> sqlite3.Connection:
    """Open the index database, creating and migrating it as needed.

    A connection per call: SQLite connections are not shareable across
    threads, and the tool layer dispatches searches through
    ``asyncio.to_thread``. Opening costs microseconds, so there is nothing to
    amortise by holding one open.
    """
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _open(path)
    except sqlite3.DatabaseError:
        # Not a database, or corrupt. The index is derived state — rebuilt
        # from the markdown on the next refresh — so discard and start over
        # rather than failing every search. Mirrors the JSON index, which
        # self-healed to empty on an unparseable file.
        for suffix in ("", "-wal", "-shm"):
            try:
                path.with_name(path.name + suffix).unlink()
            except OSError:
                pass
        return _open(path)


def _open(path: Path) -> sqlite3.Connection:
    """Open ``path`` and ensure the schema, without corruption recovery."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(con)
    except sqlite3.DatabaseError:
        con.close()
        raise
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Create the schema, rebuilding from scratch on a version mismatch."""
    version = None
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"]) if row else None
    except (sqlite3.DatabaseError, ValueError, TypeError):
        version = None

    if version is not None and version != _SCHEMA_VERSION:
        for table in ("fts", "fts_norm"):
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute("DROP TABLE IF EXISTS files")
        con.execute("DROP TABLE IF EXISTS meta")
        version = None

    with con:
        for stmt in _SCHEMA:
            con.execute(stmt)
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )


def _sweep_legacy_index() -> None:
    """Delete the JSON index this replaced. Best-effort, idempotent."""
    legacy = _legacy_index_path()
    try:
        if legacy.is_file():
            legacy.unlink()
    except OSError:
        pass


def _index_document(con: sqlite3.Connection, rowid: int, text: str) -> str | None:
    """Insert one document's postings. Returns an ``unindexable`` reason or None."""
    con.execute("DELETE FROM fts WHERE rowid = ?", (rowid,))
    con.execute("DELETE FROM fts_norm WHERE rowid = ?", (rowid,))
    con.execute("INSERT INTO fts(rowid, body) VALUES (?, ?)", (rowid, text))
    con.execute("INSERT INTO fts_norm(rowid, body) VALUES (?, ?)", (rowid, text))
    # FTS5 indexes what its tokenizer finds; a document it derives no terms
    # from can never match. Recording *why* keeps such papers reportable
    # rather than merely absent.
    #
    # The probe must agree with ``unicode61``, which tokenises on Unicode
    # character class — every letter and digit in every script. An ASCII-biased
    # test reports a Japanese or Cyrillic paper as unusable when FTS5 has
    # indexed it perfectly well, and since a non-Latin query does reach the
    # index, ``unindexable_note`` then tells the agent to fall back to
    # ``find_in_paper`` on a paper ``search_cached_papers`` would have found.
    if _ALNUM_RE.search(text) is None:
        return "no_indexable_tokens"
    return None


def _refresh_index(*, force_refresh: bool = False) -> None:
    """Bring the index in step with the markdown on disk.

    Walks the corpus comparing each file's ``(mtime_ns, size)`` to what is
    recorded, re-indexing only what changed and dropping rows whose file is
    gone. Held under a process-wide lock so two concurrent searches can't
    interleave writes.
    """
    with _INDEX_LOCK:
        _sweep_legacy_index()
        con = _connect()
        try:
            known = {
                (row["ns"], row["stem"]): (row["rowid"], row["mtime_ns"], row["size"])
                for row in con.execute("SELECT rowid, ns, stem, mtime_ns, size FROM files")
            }
            seen: set[tuple[str, str]] = set()

            with con:
                for ns, path, mtime_ns, size in _scan_markdown():
                    key = (ns, path.stem)
                    seen.add(key)
                    existing = known.get(key)
                    if (
                        not force_refresh
                        and existing is not None
                        and existing[1] == mtime_ns
                        and existing[2] == size
                    ):
                        continue
                    reason: str | None
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        reason, text = "unreadable", ""
                    else:
                        reason = None

                    if existing is None:
                        cur = con.execute(
                            "INSERT INTO files(ns, stem, mtime_ns, size) VALUES (?,?,?,?)",
                            (ns, path.stem, mtime_ns, size),
                        )
                        rowid = int(cur.lastrowid or 0)
                    else:
                        rowid = existing[0]
                        con.execute(
                            "UPDATE files SET mtime_ns = ?, size = ? WHERE rowid = ?",
                            (mtime_ns, size, rowid),
                        )
                    if reason is None:
                        reason = _index_document(con, rowid, text)
                    else:
                        con.execute("DELETE FROM fts WHERE rowid = ?", (rowid,))
                        con.execute("DELETE FROM fts_norm WHERE rowid = ?", (rowid,))
                    con.execute("UPDATE files SET unindexable = ? WHERE rowid = ?", (reason, rowid))

                for key, (rowid, _m, _s) in known.items():
                    if key in seen:
                        continue
                    con.execute("DELETE FROM fts WHERE rowid = ?", (rowid,))
                    con.execute("DELETE FROM fts_norm WHERE rowid = ?", (rowid,))
                    con.execute("DELETE FROM files WHERE rowid = ?", (rowid,))
        finally:
            con.close()


def unindexable(
    namespace: str | None = None, *, force_refresh: bool = False, refresh: bool = True
) -> list[dict[str, Any]]:
    """Papers present on disk that the index could not use.

    Each record is ``{namespace, stem, reason}`` where ``reason`` is
    ``"no_indexable_tokens"`` or ``"unreadable"``.

    These papers are invisible to ``search`` — correctly, they have no
    searchable terms — but silently so, which is not: an agent asking "which
    paper mentioned X?" has no way to learn that part of the corpus was never
    considered. Surfaced through ``search_cached_papers``.

    ``refresh=False`` skips the corpus walk, for a caller that has just run
    ``search`` and so already has an index in step with the disk. The walk is
    an ``os.scandir`` over every cached markdown file; running it twice per
    tool call buys nothing.
    """
    if refresh:
        _refresh_index(force_refresh=force_refresh)
    con = _connect()
    try:
        sql = "SELECT ns, stem, unindexable FROM files WHERE unindexable IS NOT NULL"
        params: tuple[Any, ...] = ()
        if namespace is not None:
            sql += " AND ns = ?"
            params = (namespace,)
        rows = con.execute(sql + " ORDER BY ns, stem", params).fetchall()
    finally:
        con.close()
    return [{"namespace": r["ns"], "stem": r["stem"], "reason": r["unindexable"]} for r in rows]


# Query words for the MATCH expression. Deliberately *not* ``_tokenize``:
# that regex was written to tokenise documents back when this module did its
# own indexing, and it splits on any non-ASCII character. FTS5 now tokenises
# the documents itself, so feeding it ``_tokenize``'s output made the two
# sides disagree — "Gutiérrez" became ``guti OR rrez`` and matched nothing,
# even though the document indexed cleanly as one token. Split on whitespace
# and let FTS5 apply the same tokenizer to the query that it applied to the
# corpus.
_QUERY_SPLIT_RE = re.compile(r"\s+")


def _query_words(query: str) -> list[str]:
    """The query's words, as handed to FTS5 — whitespace-split, filtered.

    Stopwords and single characters are dropped here rather than by
    ``_tokenize``. Both filters still have to happen: the index stores the
    *raw* markdown under ``unicode61``, which strips neither, so a query
    carrying "the" would otherwise OR in a term that matches essentially the
    whole corpus. What must NOT happen is ``_tokenize``'s ASCII-only word
    regex, which discards a non-Latin word entirely and made the query
    disagree with the index that had happily stored it.
    """
    words: list[str] = []
    for raw in _QUERY_SPLIT_RE.split(query.strip()):
        lowered = raw.lower()
        if len(lowered) <= 1 or lowered in _STOPWORDS:
            continue
        words.append(raw)
    return words


def _fts_query(query: str) -> str:
    """Build an FTS5 MATCH expression OR-ing the query's words.

    Each word is double-quoted so FTS5 treats it as a literal phrase rather
    than syntax — an unquoted ``NOT``, ``OR``, ``*``, ``-`` or ``:`` in a
    user query would otherwise be parsed as an operator, or raise. Inside a
    quoted phrase only ``"`` is special, and it is escaped by doubling.

    Returns ``""`` when nothing survives filtering, which the caller treats
    as an empty result — an empty MATCH expression is a syntax error to FTS5.
    """
    quoted = []
    for word in _query_words(query):
        escaped = word.replace('"', '""')
        # A word that tokenises to nothing (pure punctuation) would make FTS5
        # reject the whole expression.
        if not escaped.strip('"'):
            continue
        quoted.append(f'"{escaped}"')
    return " OR ".join(dict.fromkeys(quoted))


def _snippet_terms(query: str, *, normalize: bool) -> set[str]:
    """Terms used to centre the snippet on the best-matching passage.

    The union of two views of the query, because neither alone is enough.
    ``_tokenize`` strips punctuation a raw word would carry into the
    word-boundary regex ("transformer." never matches), but its ASCII-only
    pattern mangles anything else: "Gutiérrez" becomes ``guti``/``rrez``,
    neither of which appears in the text, and a wholly non-Latin query
    becomes nothing at all. Either case alone centres the snippet on the
    document head and reports no section, so a hit the index found perfectly
    well comes back unnavigable.

    The raw words are transformed the way ``_extract_snippet`` expects them:
    folded when ``normalize``, then lowercased. For an ordinary ASCII query
    the two views coincide and this is exactly ``_tokenize`` as before.
    """
    terms = set(_tokenize(query, normalize=normalize))
    terms.update(
        (_textnorm.fold(word) if normalize else word).lower() for word in _query_words(query)
    )
    return terms


def search(
    query: str,
    *,
    top_k: int = 10,
    namespace: str | None = None,
    normalize: bool = False,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Rank cached markdown files against ``query`` using BM25.

    Returns up to ``top_k`` hits, each shaped:

    ::

        {
            "namespace": "arxiv",
            "canonical_id": "2301.00001",
            "score": 12.4,
            "title": "Attention Is All You Need",
            "snippet": "...the proposed transformer relies entirely on...",
            "section": "Methods",       # H1/H2 the snippet falls under
            "section_index": 3,         # chainable into get_paper_section
            "char_offset": 18422,
            "char_count": 48217,
        }

    Ranking is SQLite FTS5's built-in BM25 over a contentless index. Only
    the ``top_k`` winners are re-read, to extract the title, snippet, and
    section. Every returned hit matched at least one query term and scores
    above zero; higher is better.

    **Scores are corpus-global.** ``namespace`` selects which documents come
    back, not how they rank: term rarity is computed over the whole index, so
    one paper scores identically in a filtered and an unfiltered search.

    ``normalize=True`` folds diacritics on both sides, so "cafe" and "café"
    rank identically. Folding is a build-time property of an FTS5 tokenizer
    rather than a query-time one, so the index carries a folded and an
    un-folded table and this flag selects between them — the parameter means
    exactly what it did before.

    ``force_refresh=True`` re-indexes every document regardless of the
    ``(mtime, size)`` staleness signal — a safety valve for the rare case
    where a file changed without either changing.
    """
    if top_k <= 0:
        return []
    top_k = min(top_k, _MAX_TOP_K)
    # Gate on the MATCH expression, not on ``_tokenize``: a query of purely
    # non-Latin words tokenises to nothing under that ASCII regex, and
    # returning early here made such a search come back empty even though
    # FTS5 had indexed the term and a raw MATCH found it.
    match_expr = _fts_query(query)
    if not match_expr:
        return []

    _refresh_index(force_refresh=force_refresh)

    table = "fts_norm" if normalize else "fts"
    sql = (
        f"SELECT f.ns AS ns, f.stem AS stem, bm25({table}) AS score "
        f"FROM {table} JOIN files f ON f.rowid = {table}.rowid "
        f"WHERE {table} MATCH ?"
    )
    params: list[Any] = [match_expr]
    if namespace is not None:
        sql += " AND f.ns = ?"
        params.append(namespace)
    # Tie-break by (namespace, stem): equal-scoring hits would otherwise
    # come back in rowid order, which drifts as papers are added.
    sql += f" ORDER BY bm25({table}), f.ns, f.stem LIMIT ?"
    params.append(top_k)

    con = _connect()
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        # A query FTS5 cannot parse is an empty result, not a crash.
        return []
    finally:
        con.close()

    unique_query_terms = _snippet_terms(query, normalize=normalize)
    out: list[dict[str, Any]] = []
    for row in rows:
        # FTS5's bm25() is negative, most-relevant first. Flip it so the
        # response keeps "higher is better", as it always has. No score
        # filter: FTS5 only returns rows that actually matched, so a low
        # score means "matched on a term with little discriminative value",
        # not "did not match".
        score = -float(row["score"])
        path = cache._CACHE_ROOT / row["ns"] / "markdown" / f"{row['stem']}.md"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = _extract_title(text)
        snippet, snippet_offset = _extract_snippet(text, unique_query_terms, normalize=normalize)
        if snippet_offset is not None:
            section_index, section = _section_for_offset(text, snippet_offset)
        else:
            section_index, section = None, None
        out.append(
            {
                "namespace": row["ns"],
                "canonical_id": _filename_to_canonical(row["ns"], row["stem"]),
                # Invariant: every returned hit scores above zero. 6 decimals
                # rather than 3 because FTS5 returns a very small positive
                # score for a degenerate IDF (a term in every document of a
                # small corpus), and 3 rounds those to 0.0. Real-corpus scores
                # are 0.9-4, so readability is unaffected.
                "score": round(score, 6),
                "title": title,
                "snippet": snippet,
                "section": section,
                # The chainable handle. `section` is a title, and titles are
                # not unique — get_paper_section rejects a repeated one with
                # "Ambiguous section title". Pass this index instead.
                "section_index": section_index,
                "char_offset": snippet_offset,
                "char_count": len(text),
            }
        )
    return out
