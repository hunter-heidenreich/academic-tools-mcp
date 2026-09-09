"""BM25 keyword search over the converted-markdown cache.

Ranks ``.cache/<namespace>/markdown/*.md`` with SQLite FTS5 and re-reads only
the winners, for a title, a snippet and the section an agent chains into
``get_paper_section``.
"""

import contextlib
import os
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import unquote

from . import manual, papers
from .providers import acl, arxiv, biorxiv
from .store import cache, stems
from .util import doinorm, textnorm

# Enough to tell "variational dropout" from "dropout regularisation"; more is bloat.
_SNIPPET_CHARS = 200

# So a noisy query can't pull the whole corpus back in one tool call.
MAX_TOP_K = 50

# Keeps intra-word hyphens and dots: "self-attention", "BM25", "1.5x" stay one token.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-.]*[a-z0-9]|[a-z0-9]")

# Don't grow it: "all" / "no" / "not" / "very" carry content in this domain.
_STOPWORD_TEXT = """
a an the and or but
of to in on at for with by from as into about over under between
is are was were be been being
this that these those it its
we our their them they he she his her i you your
if then than so such
"""
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())

# --- Titles and snippets — what a hit shows ---


def _content_tokens(text: str, *, normalize: bool = False) -> set[str]:
    """Lowercased content words: punctuation, stopwords and single characters dropped.

    **Not a tokenizer** — ASCII-only, so it disagrees with FTS5 outside ASCII.
    ``_snippet_terms`` is the one consumer.
    """
    if normalize:
        text = textnorm.fold(text)
    return {
        tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS and len(tok) > 1
    }


def _extract_title(markdown: str) -> str | None:
    """The document's first H1 or H2, or ``None``. Delegated: heading level is ``papers``' policy."""
    return papers.first_section_heading(markdown)


def _extract_snippet(
    markdown: str,
    query_terms: set[str],
    *,
    normalize: bool = False,
) -> tuple[str, int | None]:
    """``(snippet, char_offset)`` for the window holding the most distinct query terms.

    ``char_offset`` indexes the ORIGINAL markdown under either normalisation,
    and is ``None`` for the document head, which no section may be attributed to.
    """
    half = _SNIPPET_CHARS // 2
    hits: list[tuple[int, str]] = []
    if query_terms:
        # Not str.lower(): 'İ' lowercases to two chars, drifting an unmapped m.start().
        lowered, index_map = textnorm.lower_with_map(markdown, fold=normalize)
        # Longest first: \b settles "attention"/"attentions", not a hyphen or dot split.
        alternation = "|".join(re.escape(t) for t in sorted(query_terms, key=len, reverse=True))
        # One pass for every term: megabyte documents, once per winner.
        pattern = re.compile(rf"\b(?:{alternation})\b")
        hits = [(index_map[m.start()], m.group(0)) for m in pattern.finditer(lowered)]

    best_offset: int | None = None
    if hits:
        # No sort: finditer scans forward and index_map is monotonic, so offsets ascend.
        best_offset = hits[0][0]
        best_distinct = 1
        counts: Counter[str] = Counter()
        lo = hi = 0
        for off, _term in hits:
            while hi < len(hits) and hits[hi][0] <= off + half:
                counts[hits[hi][1]] += 1
                hi += 1
            while hits[lo][0] < off - half:
                term = hits[lo][1]
                if counts[term] == 1:
                    del counts[term]
                else:
                    counts[term] -= 1
                lo += 1
            if len(counts) > best_distinct:
                best_distinct = len(counts)
                best_offset = off

    start = 0 if best_offset is None else max(0, best_offset - half)
    snippet = markdown[start : start + _SNIPPET_CHARS]
    # Collapsed: one crossing a heading renders as "## Methods\n\n\n\nWe trained...".
    return re.sub(r"\s+", " ", snippet.strip()), best_offset


# --- Filename → identifier inversion per namespace ---

# A DOI suffix may contain "_", so only a slash a known prefix introduces is decidable.
_NAMESPACE_DOI_PREFIXES = {
    biorxiv.NAMESPACE: biorxiv.DOI_PREFIX,
    acl.NAMESPACE: acl.ACL_DOI_PREFIX,
}

# manual holds publisher DOIs, not the freeform labels its name suggests.
_MANUAL_DOI_STEM_RE = re.compile(rf"^({doinorm.REGISTRANT_PATTERN})_")

# New-style ids start with a digit and pass through. Built from `arxiv`'s
# exported patterns, so this and the router cannot drift.
_ARXIV_OLDSTYLE_STEM_RE = re.compile(
    rf"^({arxiv.OLD_ARCHIVE_PATTERN})_({arxiv.OLD_NUMBER_PATTERN})$"
)


def _filename_to_canonical(namespace: str, stem: str) -> str:
    """Invert ``stems.safe_stem``: the cache key a stored filename came from.

    Exactly one ``unquote`` — ``safe_stem`` writes a literal ``%`` as ``%25``,
    so a second pass decodes an escape that never was one.
    """
    return unquote(_restore_slashes(namespace, stem))


def _restore_slashes(namespace: str, stem: str) -> str:
    """Undo ``safe_stem``'s ``"/" -> "_"`` mapping, as far as it is decidable."""
    if namespace == arxiv.NAMESPACE:
        return _ARXIV_OLDSTYLE_STEM_RE.sub(r"\1/\2", stem, count=1)
    if namespace == manual.NAMESPACE:
        return _MANUAL_DOI_STEM_RE.sub(r"\1/", stem, count=1)
    prefix = _NAMESPACE_DOI_PREFIXES.get(namespace, "")
    stemmed = prefix.replace("/", "_")
    if prefix and stem.startswith(stemmed):
        return prefix + stem[len(stemmed) :]
    return stem


# --- The corpus on disk ---


class _ScannedFile(NamedTuple):
    """One cached markdown file, as the refresh needs it.

    ``path`` stays a ``str``: only changed files are read, so a ``Path`` per
    entry is overhead on the warm walk.
    """

    namespace: str
    stem: str
    path: str
    mtime_ns: int
    size: int


def _scan_markdown() -> list[_ScannedFile]:
    """Every cached markdown file on disk, carrying ``os.scandir``'s stat, in no order.

    **Must stay unfiltered:** ``_prune_missing`` deletes every indexed row this
    walk did not return.
    """
    out: list[_ScannedFile] = []
    try:
        # A missing or non-directory root is the same "nothing cached yet" as an empty one.
        with os.scandir(cache.CACHE_ROOT) as namespaces:
            namespace_entries = list(namespaces)
    except OSError:
        return []
    for ns_entry in namespace_entries:
        if not ns_entry.is_dir():
            continue
        try:
            with os.scandir(Path(ns_entry.path) / "markdown") as files:
                entries = list(files)
        except OSError:
            continue
        for entry in entries:
            if not entry.name.endswith(".md"):
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            out.append(
                _ScannedFile(
                    ns_entry.name,
                    entry.name.removesuffix(".md"),
                    entry.path,
                    st.st_mtime_ns,
                    st.st_size,
                )
            )
    return out


# --- The FTS5 index ---

_INDEX_DIRNAME = "__search_index__"

# Bump when the row schema or the tokenizer changes; a mismatch rebuilds.
_SCHEMA_VERSION = 3

# Never equals a real stat, so a file that failed to read is retried.
_UNREADABLE_MTIME = -1

# Exported: `tools/search.py` owes the agent one explanation per reason.
NO_INDEXABLE_TOKENS = "no_indexable_tokens"
UNREADABLE = "unreadable"
UNINDEXABLE_REASONS = frozenset({NO_INDEXABLE_TOKENS, UNREADABLE})

# One Unicode letter or digit: a close proxy for what `unicode61` tokenises, in
# any script, so a CJK or Cyrillic paper isn't called unusable.
_ALNUM_RE = re.compile(r"[^\W_]")

_INDEX_LOCK = threading.Lock()
# Only touched under _INDEX_LOCK, so it needs no lock of its own.
_LEGACY_SWEPT: set[Path] = set()


class _IndexedFile(NamedTuple):
    """The row already recorded for a cached file."""

    rowid: int
    mtime_ns: int
    size: int


def _index_path() -> Path:
    """Path to the SQLite index database."""
    return cache.CACHE_ROOT / _INDEX_DIRNAME / "index.db"


def _legacy_index_path() -> Path:
    """Path to the pre-FTS5 JSON index, which ``_sweep_legacy_index`` removes."""
    return cache.CACHE_ROOT / _INDEX_DIRNAME / "index.json"


_SCHEMA = (
    # Declared, not implicit — VACUUM renumbers an implicit rowid, and fts keys on it.
    """CREATE TABLE IF NOT EXISTS files (
           rowid     INTEGER PRIMARY KEY,
           ns        TEXT    NOT NULL,
           stem      TEXT    NOT NULL,
           mtime_ns  INTEGER NOT NULL,
           size      INTEGER NOT NULL,
           unindexable TEXT,
           UNIQUE (ns, stem)
       )""",
    # contentless_delete=1 needs SQLite 3.43+; it is what lets a removed paper go.
    # Two tables: remove_diacritics is build-time, `normalize` is per-query.
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

    One per call: connections are not shareable across ``search``'s worker
    threads, and opening costs microseconds.
    """
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _open(path)
    except sqlite3.OperationalError:
        # A DatabaseError subclass — shadowed here so a busy index isn't deleted below.
        raise
    except sqlite3.DatabaseError:
        # Derived state: discard and rebuild rather than fail every search.
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                path.with_name(path.name + suffix).unlink()
        return _open(path)


def _open(path: Path) -> sqlite3.Connection:
    """Open ``path`` and ensure the schema, without corruption recovery."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(con)
    except Exception:
        # Don't leave the connection open for `_connect` to unlink out from under.
        con.close()
        raise
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Create the schema, rebuilding from scratch on a version mismatch."""
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"]) if row else None
    except (sqlite3.DatabaseError, ValueError, TypeError):
        version = None

    if version == _SCHEMA_VERSION:
        # Written last, so its presence means the tables exist — and the open stays read-only.
        return

    # An unreadable version says nothing about the tables: rebuild, don't certify.
    for table in ("fts", "fts_norm", "files", "meta"):
        con.execute(f"DROP TABLE IF EXISTS {table}")

    with con:
        for stmt in _SCHEMA:
            con.execute(stmt)
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )


def _sweep_legacy_index() -> None:
    """Delete the pre-FTS5 JSON index. Best-effort, idempotent, once per cache root.

    Only an upgrade can leave that file, so probing per refresh never pays off.
    """
    root = cache.CACHE_ROOT
    if root in _LEGACY_SWEPT:
        return
    _LEGACY_SWEPT.add(root)
    legacy = _legacy_index_path()
    try:
        if legacy.is_file():
            legacy.unlink()
    except OSError:
        pass


# --- Staleness and self-healing ---


def _index_document(con: sqlite3.Connection, rowid: int, text: str) -> str | None:
    """Replace one document's postings. Returns an ``unindexable`` reason or None.

    A document with no terms is left out of both tables, absent exactly like an
    unreadable one.
    """
    con.execute("DELETE FROM fts WHERE rowid = ?", (rowid,))
    con.execute("DELETE FROM fts_norm WHERE rowid = ?", (rowid,))
    # Must agree with `unicode61` on what a term is, in every script.
    if _ALNUM_RE.search(text) is None:
        return NO_INDEXABLE_TOKENS
    con.execute("INSERT INTO fts(rowid, body) VALUES (?, ?)", (rowid, text))
    con.execute("INSERT INTO fts_norm(rowid, body) VALUES (?, ?)", (rowid, text))
    return None


# DELETE cannot decrement a contentless table's statistics, so a replaced
# document inflates what `bm25()` divides by. Reset once churn == corpus.
_CHURN_KEY = "stat_churn"


def _churn(con: sqlite3.Connection) -> int:
    """Documents replaced or removed since the statistics were last reset."""
    row = con.execute("SELECT value FROM meta WHERE key = ?", (_CHURN_KEY,)).fetchone()
    try:
        return int(row["value"]) if row else 0
    except (ValueError, TypeError):
        # As with the schema version: unreadable is not a claim that they're clean.
        return 0


def _set_churn(con: sqlite3.Connection, value: int) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (_CHURN_KEY, str(value)))


def _reset_postings(con: sqlite3.Connection) -> None:
    """Drop every posting so FTS5 recomputes its statistics; callers must re-insert.

    ``delete-all`` is the only reset a contentless table has — ``rebuild`` needs
    stored content.
    """
    con.execute("INSERT INTO fts(fts) VALUES('delete-all')")
    con.execute("INSERT INTO fts_norm(fts_norm) VALUES('delete-all')")


def _is_fresh(found: _ScannedFile, existing: _IndexedFile | None) -> bool:
    """Does the recorded row already describe the file on disk?"""
    return (
        existing is not None and existing.mtime_ns == found.mtime_ns and existing.size == found.size
    )


def _reindex_file(
    con: sqlite3.Connection, found: _ScannedFile, existing: _IndexedFile | None
) -> int:
    """Bring one file's ``files`` row and its postings up to date.

    Caller holds the ``with con:`` transaction — this issues writes only.
    Returns churn: 1 whenever a row already existed (an over-count when that
    revision had no postings, which only resets sooner), 0 for a fresh insert.
    """
    reason: str | None
    try:
        text = Path(found.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        reason, text = UNREADABLE, ""
    else:
        reason = None

    # Storing the stat that succeeded would freeze the failure: a chmod leaves mtime alone.
    recorded_mtime = _UNREADABLE_MTIME if reason else found.mtime_ns

    if existing is None:
        cur = con.execute(
            "INSERT INTO files(ns, stem, mtime_ns, size) VALUES (?,?,?,?)",
            (found.namespace, found.stem, recorded_mtime, found.size),
        )
        rowid = int(cur.lastrowid or 0)
    else:
        rowid = existing.rowid
        con.execute(
            "UPDATE files SET mtime_ns = ?, size = ? WHERE rowid = ?",
            (recorded_mtime, found.size, rowid),
        )
    # Always: it drops the old postings, and adds none for unreadable text.
    probed = _index_document(con, rowid, text)
    con.execute("UPDATE files SET unindexable = ? WHERE rowid = ?", (reason or probed, rowid))
    return 0 if existing is None else 1


def _prune_missing(
    con: sqlite3.Connection,
    known: dict[tuple[str, str], _IndexedFile],
    seen: set[tuple[str, str]],
) -> int:
    """Drop every indexed row the walk did not return; returns how many."""
    removed = 0
    for key, row in known.items():
        if key in seen:
            continue
        con.execute("DELETE FROM fts WHERE rowid = ?", (row.rowid,))
        con.execute("DELETE FROM fts_norm WHERE rowid = ?", (row.rowid,))
        con.execute("DELETE FROM files WHERE rowid = ?", (row.rowid,))
        removed += 1
    return removed


def _refresh_index(*, force_refresh: bool = False) -> None:
    """Re-index what ``_is_fresh`` rejects, drop rows whose file is gone.

    Under a process-wide lock: ``search`` runs in worker threads, and two
    refreshes must not interleave writes.
    """
    with _INDEX_LOCK:
        _sweep_legacy_index()
        con = _connect()
        try:
            known = {
                (row["ns"], row["stem"]): _IndexedFile(row["rowid"], row["mtime_ns"], row["size"])
                for row in con.execute("SELECT rowid, ns, stem, mtime_ns, size FROM files")
            }
            seen: set[tuple[str, str]] = set()
            # Both paths re-index everything, so the reset is free; the counter is
            # from previous refreshes, so a crossing resets on the next search.
            reset = force_refresh or (known and _churn(con) >= len(known))

            with con:
                if reset:
                    _reset_postings(con)
                churn = 0
                for found in _scan_markdown():
                    key = (found.namespace, found.stem)
                    seen.add(key)
                    existing = known.get(key)
                    if reset or not _is_fresh(found, existing):
                        churn += _reindex_file(con, found, existing)
                churn += _prune_missing(con, known, seen)
                # Rebuilt from nothing, so the displacements above are accounted for.
                _set_churn(con, 0 if reset else _churn(con) + churn)
        finally:
            con.close()


# --- Query and ranking ---

# Whitespace and NUL only: a punctuated word stays whole and reaches FTS5 as a
# phrase. NUL because `unicode61` separates on it — sqlite3 binds one fine.
_QUERY_SPLIT_RE = re.compile(r"[\s\x00]+")


def _query_words(query: str) -> list[str]:
    """The query's words as handed to FTS5, stopwords and single characters dropped.

    ``unicode61`` strips neither, so an unfiltered "the" ORs in a term matching
    the whole corpus. Filtered here, not in ``_content_tokens``, whose ASCII-only
    regex drops non-Latin words the index holds.
    """
    return [
        word
        for word in _QUERY_SPLIT_RE.split(query.strip())
        # len(word), not len(word.lower()): 'İ' lowercases to two characters.
        if len(word) > 1 and word.lower() not in _STOPWORDS
    ]


def _fts_query(query: str) -> str:
    """An FTS5 MATCH expression OR-ing the query's words.

    Each is quoted so an unquoted ``NOT``/``OR``/``*``/``-``/``:`` cannot parse
    as an operator; a quoted word is still tokenised, so "self-attention" is a
    phrase, not a term. ``""`` when nothing survives — an empty MATCH is an FTS5
    syntax error, so the caller must treat it as an empty result.
    """
    # Keyed case-insensitively, as FTS5 is: one term ORed with its own spelling
    # scores twice. Emitted as typed — folding is the tokenizer's job.
    by_token: dict[str, str] = {}
    for word in _query_words(query):
        by_token.setdefault(word.lower(), word)
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in by_token.values())


def _snippet_terms(query: str, *, normalize: bool) -> set[str]:
    """Terms to centre the snippet on: tokenised words unioned with raw ones.

    Neither view alone survives ``_extract_snippet``'s word-boundary scan —
    tokenising strips punctuation ("transformer."), the raw word keeps what the
    ASCII-only pattern mangles ("Gutiérrez" → "guti"/"rrez").
    """
    return _content_tokens(query, normalize=normalize) | {
        (textnorm.fold(word) if normalize else word).lower() for word in _query_words(query)
    }


def _search_sql(
    table: str, match_expr: str, namespace: str | None, top_k: int
) -> tuple[str, list[Any]]:
    """The ranked MATCH query and its bound parameters.

    ``table`` is one of two literals ``search`` chose, never caller input, and
    every value is bound — why the f-string below is safe.
    """
    sql = (
        f"SELECT f.ns AS ns, f.stem AS stem, bm25({table}) AS score "  # noqa: S608
        f"FROM {table} JOIN files f ON f.rowid = {table}.rowid "
        f"WHERE {table} MATCH ?"
    )
    params: list[Any] = [match_expr]
    if namespace is not None:
        sql += " AND f.ns = ?"
        params.append(namespace)
    # Tie-break by (namespace, stem): FTS5 orders by rank alone, and insertion order drifts.
    sql += f" ORDER BY bm25({table}), f.ns, f.stem LIMIT ?"
    params.append(top_k)
    return sql, params


def _hit(row: sqlite3.Row, terms: set[str], *, normalize: bool) -> dict[str, Any] | None:
    """Shape one result row into an agent-facing hit, or None if unreadable."""
    # bm25() is negative, most-relevant first. No floor: every row returned matched.
    score = -float(row["score"])
    path = stems.markdown_path_for_stem(row["ns"], row["stem"])
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    snippet, snippet_offset = _extract_snippet(text, terms, normalize=normalize)
    # Never a local heading scan: a copy drops papers' empty-section filter.
    found = papers.section_at_offset(text, snippet_offset) if snippet_offset is not None else None
    section_index, section = found or (None, None)
    return {
        "namespace": row["ns"],
        "canonical_id": _filename_to_canonical(row["ns"], row["stem"]),
        # Significant figures, not decimals: a degenerate IDF scales below 1e-7.
        "score": float(f"{score:.6g}"),
        "title": _extract_title(text),
        "snippet": snippet,
        "section": section,
        # The chainable handle: `section` is a title, and a repeated one is ambiguous.
        "section_index": section_index,
        "char_offset": snippet_offset,
        "char_count": len(text),
    }


def search(
    query: str,
    *,
    top_k: int = 10,
    namespace: str | None = None,
    normalize: bool = False,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    """Rank cached markdown files against ``query`` using BM25, best first.

    Up to ``top_k`` hits, silently clamped to :data:`MAX_TOP_K`, shaped by
    :func:`_hit`. Scores are corpus-global: ``namespace`` selects which
    documents come back, not how they rank. Refreshes the index first — the
    contract ``unindexable(refresh=False)`` is written against — but both early
    returns fire before that. Raises ``sqlite3.Error`` rather than reporting a
    locked or corrupt index as a confident "no paper mentions this".
    """
    if top_k <= 0:
        return []
    top_k = min(top_k, MAX_TOP_K)
    # Gate on the MATCH expression, never the word regex: FTS5 sees more than it does.
    match_expr = _fts_query(query)
    if not match_expr:
        return []

    _refresh_index(force_refresh=force_refresh)

    table = "fts_norm" if normalize else "fts"
    sql, params = _search_sql(table, match_expr, namespace, top_k)
    con = _connect()
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    terms = _snippet_terms(query, normalize=normalize)
    hits = (_hit(row, terms, normalize=normalize) for row in rows)
    return [hit for hit in hits if hit is not None]


def unindexable(namespace: str | None = None, *, refresh: bool = True) -> list[dict[str, Any]]:
    """Papers on disk the index could not use — silently invisible otherwise.

    ``{namespace, stem, canonical_id, reason}`` rows, ``reason`` one of
    :data:`UNINDEXABLE_REASONS`, ordered by ``(namespace, stem)`` since callers
    sample off the front. ``refresh=False`` is a contract, not an optimisation:
    it reads what the ``search`` just run left behind.
    """
    if refresh:
        _refresh_index()
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
    return [
        {
            "namespace": r["ns"],
            "stem": r["stem"],
            "canonical_id": _filename_to_canonical(r["ns"], r["stem"]),
            "reason": r["unindexable"],
        }
        for r in rows
    ]
