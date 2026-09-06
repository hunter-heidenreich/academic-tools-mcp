"""BM25 keyword search over the converted-markdown cache.

The PDF pipeline (download_pdf → convert_paper) lands every paper's markdown
under ``.cache/<namespace>/markdown/<canonical>.md``. This ranks that corpus
against a query with SQLite FTS5 and re-reads only the winners, to extract a
title, a snippet centred on the densest cluster of query terms, and the
section index an agent chains into ``get_paper_section``.

Design rationale — the contentless index, the two tokenizer tables, the
``(mtime_ns, size)`` refresh and the invariants each of them holds — is in
``.claude/rules/search.md``, which loads whenever this file is opened.
"""

import contextlib
import os
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import _doi, _textnorm, cache, papers

# Enough to tell "variational dropout" from "dropout regularisation"; more is bloat.
_SNIPPET_CHARS = 200

# So a noisy query can't pull the whole corpus back in one tool call.
_MAX_TOP_K = 50

# Keeps intra-word hyphens and dots, so "self-attention", "BM25" and "1.5x"
# stay one token each. Snippet terms only — FTS5 tokenises the corpus.
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

_HEADING_RE = re.compile(papers.HEADING_PATTERN, re.MULTILINE)


def _content_tokens(text: str, *, normalize: bool = False) -> set[str]:
    """Lowercased content words, punctuation stripped, stopwords dropped.

    **Not a tokenizer** — FTS5 tokenises both the corpus and the query, and
    this regex disagrees with it outside ASCII. Its one consumer is
    ``_snippet_terms``, which needs the punctuation-stripped view of a query
    word. ``normalize=True`` NFKD-folds first, so "café" and "cafe" agree.
    """
    if normalize:
        text = _textnorm.fold(text)
    return {
        tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS and len(tok) > 1
    }


def _extract_title(markdown: str) -> str | None:
    """Return the first H1 or H2 in the document, or ``None``.

    Either level, because converters disagree on which one a paper title gets.
    """
    for match in _HEADING_RE.finditer(markdown):
        level = len(match.group(1))
        if level <= 2:
            return match.group(2).strip()
    return None


def _section_for_offset(markdown: str, offset: int) -> tuple[int | None, str | None]:
    """Return ``(section_index, title)`` for a character offset, or ``(None, None)``.

    Must keep delegating to ``papers.section_at_offset``: a local copy of that
    scan names sections ``get_paper_section`` would refuse.
    """
    return papers.section_at_offset(markdown, offset) or (None, None)


def _extract_snippet(
    markdown: str,
    query_terms: set[str],
    window: int = _SNIPPET_CHARS,
    *,
    normalize: bool = False,
) -> tuple[str, int | None]:
    """Return ``(snippet, char_offset)`` for the best matching position.

    "Best matching" = the position with the most distinct query terms in the
    surrounding window, so "variational dropout" cooccurrence beats a lone
    "dropout". Falls back to the document head, and a ``None`` offset, when no
    term is found — the caller must not attribute a section to that.

    ``char_offset`` indexes the ORIGINAL markdown under either normalisation.
    """
    if not query_terms:
        return _collapse(markdown[:window]), 0

    # Word boundaries, so "drop" doesn't hit inside "dropout". One alternation
    # over all the terms rather than a pass each — these documents reach
    # megabytes and this runs once per winner — longest alternative first so an
    # overlapping pair ("attention" / "attentions") reports the longer. The
    # matched text *is* the term: both sides are transformed identically.
    #
    # index_map is required even on the normalize=False path: str.lower() is
    # not length-preserving (U+0130 'İ' → 'i' + combining dot), so a raw
    # m.start() drifts past the match.
    lowered, index_map = _textnorm.lower_with_map(markdown, fold=normalize)
    alternation = "|".join(re.escape(t) for t in sorted(query_terms, key=len, reverse=True))
    pattern = re.compile(rf"\b(?:{alternation})\b")
    hits: list[tuple[int, str]] = [
        (index_map[m.start()], m.group(0)) for m in pattern.finditer(lowered)
    ]

    if not hits:
        return _collapse(markdown[:window]), None

    # Distinct terms within ±window/2 chars, via one sliding window over the
    # offset-sorted hits: `lo` and `hi` only advance, so a term repeating
    # densely inside one window costs linear time, not quadratic.
    hits.sort()
    half = window // 2
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

    start = max(0, best_offset - half)
    end = min(len(markdown), start + window)
    return _collapse(markdown[start:end]), best_offset


def _collapse(snippet: str) -> str:
    """Trim and flatten a snippet's whitespace.

    Every return path goes through this, so the response key doesn't mean two
    different things depending on whether a term was located.
    """
    return re.sub(r"\s+", " ", snippet.strip())


# ---------------------------------------------------------------------------
# Filename → identifier inversion per namespace
# ---------------------------------------------------------------------------

# Inverting safe_stem's "/" -> "_" is namespace-specific, because a DOI suffix
# may legitimately contain "_": only a slash a known prefix introduced is
# decidable.
_NAMESPACE_PREFIX_REPAIRS: dict[str, list[tuple[str, str]]] = {
    # bioRxiv DOIs are always "10.1101/<suffix>" — exactly one slash.
    "biorxiv": [("10.1101_", "10.1101/")],
    # ACL Anthology DOIs are always "10.18653/v1/<suffix>" — two slashes.
    "acl_anthology": [("10.18653_v1_", "10.18653/v1/")],
}

# Most of the manual namespace is publisher DOIs, not the freeform labels the
# name suggests. The registrant is digits only, so the first "_" after it is
# unambiguously the slash; a suffix carrying further slashes round-trips
# imperfectly, and a label doesn't match and passes through.
_MANUAL_DOI_STEM_RE = re.compile(rf"^({_doi.REGISTRANT_PATTERN})_")

# "archive[.subject]_NNNNNNN[vN]" — every old-style archive, dotted or
# hyphenated, with the version canonical_arxiv_id keeps. New-style ids start
# with a digit and pass through.
_ARXIV_OLDSTYLE_STEM_RE = re.compile(r"^([a-z][a-z.\-]*)_(\d{7}(?:v\d+)?)$")


def _filename_to_canonical(namespace: str, stem: str) -> str:
    """Invert ``papers.safe_stem`` for the given namespace.

    ``stem`` is the filename without the ``.md`` extension. Returns the
    canonical form the original code would have used as a cache key, so the
    ``canonical_id`` on a hit chains into the paper tools.

    Percent-decoding is unconditional and runs last: ``safe_stem`` encodes a
    literal ``%`` as ``%25``, so a single ``unquote`` is its exact inverse and
    can't manufacture an escape that wasn't one.
    """
    return unquote(_restore_slashes(namespace, stem))


def _restore_slashes(namespace: str, stem: str) -> str:
    """Undo ``safe_stem``'s ``"/" -> "_"`` mapping, as far as it is decidable."""
    if namespace == "arxiv":
        m = _ARXIV_OLDSTYLE_STEM_RE.match(stem)
        return f"{m.group(1)}/{m.group(2)}" if m else stem
    if namespace == "manual":
        return _MANUAL_DOI_STEM_RE.sub(r"\1/", stem, count=1)
    repairs = _NAMESPACE_PREFIX_REPAIRS.get(namespace, [])
    for needle, replacement in repairs:
        if stem.startswith(needle):
            return replacement + stem[len(needle) :]
    return stem


# ---------------------------------------------------------------------------
# BM25 search
# ---------------------------------------------------------------------------


def _scan_markdown() -> list[tuple[str, Path, int, int]]:
    """Every cached markdown file as ``(namespace, path, mtime_ns, size)``.

    ``os.scandir`` carries the stat the refresh needs, so it isn't fetched
    twice. Order is not guaranteed — the refresh keys on ``(ns, stem)``.

    **Must stay unfiltered.** ``_refresh_index`` prunes every indexed row this
    walk did not return, so a namespace filter would delete every other
    namespace's postings.
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
        if not ns_entry.is_dir():
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

_INDEX_DIRNAME = "__search_index__"
_DB_FILENAME = "index.db"

# Bump to force a full rebuild when the row schema or the tokenizer changes;
# a mismatch is rebuilt rather than migrated, since the index is derived state.
_SCHEMA_VERSION = 2

# Stands in for the mtime of a file that could not be read, so the staleness
# signal can never match and the next refresh retries it.
_UNREADABLE_MTIME = -1

# One Unicode letter or digit — ``\w`` minus underscore, which is what
# ``unicode61`` counts as a token character in any script.
_ALNUM_RE = re.compile(r"[^\W_]")

# Process-wide mutable state, both guarded by the lock: refreshes serialise on
# it, and the legacy sweep records which cache roots it has already visited.
_INDEX_LOCK = threading.Lock()
_LEGACY_SWEPT: set[Path] = set()


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
    # Contentless: postings only. The markdown is on disk and the winners are
    # re-read for snippets anyway. contentless_delete=1 (SQLite 3.43+) is what
    # lets a removed paper be DELETEd without handing back its text.
    #
    # Two tables because folding is a build-time tokenizer option in FTS5
    # while ``normalize`` is a query-time flag here.
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

    One connection per call — they are not shareable across the worker threads
    ``search`` runs in, and opening costs microseconds.
    """
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return _open(path)
    except sqlite3.DatabaseError:
        # Not a database, or corrupt. The index is derived state — rebuilt
        # from the markdown on the next refresh — so discard and start over
        # rather than failing every search.
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
    except sqlite3.DatabaseError:
        con.close()
        raise
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Create the schema, rebuilding from scratch on a version mismatch.

    A no-op — and a read-only one — when the recorded version already matches.
    """
    version = None
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"]) if row else None
    except (sqlite3.DatabaseError, ValueError, TypeError):
        version = None

    if version == _SCHEMA_VERSION:
        # The meta row is written last, so its presence means the tables exist.
        # Returning keeps the common open read-only; otherwise every connection
        # opens a write transaction, and a search opens three.
        return

    if version is not None:
        for table in ("fts", "fts_norm"):
            con.execute(f"DROP TABLE IF EXISTS {table}")
        con.execute("DROP TABLE IF EXISTS files")
        con.execute("DROP TABLE IF EXISTS meta")

    with con:
        for stmt in _SCHEMA:
            con.execute(stmt)
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )


def _sweep_legacy_index() -> None:
    """Delete the JSON index this replaced. Best-effort, idempotent.

    Once per cache root, not per refresh: only an upgrade can leave that file,
    so probing for it on every search is a stat that will never pay off. Called
    under ``_INDEX_LOCK``, so the set needs no lock of its own.
    """
    root = cache._CACHE_ROOT
    if root in _LEGACY_SWEPT:
        return
    _LEGACY_SWEPT.add(root)
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
    # A document FTS5 derives no terms from can never match; recording *why*
    # keeps it reportable rather than merely absent. The probe must agree with
    # ``unicode61`` on what a term is — an ASCII-biased test calls a Japanese
    # or Cyrillic paper unusable when the index holds it fine.
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

                    # Storing the stat that *did* succeed would freeze the
                    # failure — a lock that cleared, or a chmod, leaves mtime
                    # alone, so the retry would never fire.
                    recorded_mtime = _UNREADABLE_MTIME if reason else mtime_ns

                    if existing is None:
                        cur = con.execute(
                            "INSERT INTO files(ns, stem, mtime_ns, size) VALUES (?,?,?,?)",
                            (ns, path.stem, recorded_mtime, size),
                        )
                        rowid = int(cur.lastrowid or 0)
                    else:
                        rowid = existing[0]
                        con.execute(
                            "UPDATE files SET mtime_ns = ?, size = ? WHERE rowid = ?",
                            (recorded_mtime, size, rowid),
                        )
                    if reason is None:
                        reason = _index_document(con, rowid, text)
                    else:
                        con.execute("DELETE FROM fts WHERE rowid = ?", (rowid,))
                        con.execute("DELETE FROM fts_norm WHERE rowid = ?", (rowid,))
                    con.execute("UPDATE files SET unindexable = ? WHERE rowid = ?", (reason, rowid))

                # Prune: every indexed row the walk did not return. This is
                # why _scan_markdown must stay unfiltered.
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

    Each record is ``{namespace, stem, reason}``, where ``reason`` is
    ``"no_indexable_tokens"`` or ``"unreadable"``. Such papers are invisible
    to ``search`` correctly but *silently*, which is what this fixes.

    ``refresh=False`` skips the corpus walk — for a caller that has just run
    ``search`` and so already has an index in step with the disk. It is the
    contract ``search_cached_papers`` relies on, not an optimisation: making
    it unconditional walks the corpus twice per tool call.
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


# ``unicode61``'s separators, so the query splits the way the corpus did.
# Splitting on NUL is also what keeps one out of the bind — sqlite3 cannot bind
# a string containing one at all.
_QUERY_SPLIT_RE = re.compile(r"[\s\x00]+")


def _query_words(query: str) -> list[str]:
    """The query's words, as handed to FTS5 — split, then filtered.

    Stopwords and single characters are dropped here and not by
    ``_content_tokens``, whose ASCII-only regex would discard a non-Latin word
    the index holds. The
    filters themselves are still needed: ``unicode61`` strips neither, so an
    unfiltered "the" ORs in a term matching the whole corpus.
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

    Returns ``""`` when nothing survives filtering, which the caller must treat
    as an empty result — an empty MATCH expression is a syntax error to FTS5.
    """
    quoted = [f'"{word.replace(chr(34), chr(34) * 2)}"' for word in _query_words(query)]
    return " OR ".join(dict.fromkeys(quoted))


def _snippet_terms(query: str, *, normalize: bool) -> set[str]:
    """Terms used to centre the snippet on the best-matching passage.

    The union of two views of the query, because neither alone is enough for
    ``_extract_snippet``'s word-boundary scan: ``_content_tokens`` strips
    punctuation a raw word would carry in ("transformer." never matches), while
    the raw words keep what its ASCII-only pattern mangles ("Gutiérrez"). With
    only one, a hit the index found comes back centred on the document head and
    with no section — unnavigable.
    """
    terms = _content_tokens(query, normalize=normalize)
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

    Every returned hit matched at least one query term and scores above zero;
    higher is better. ``section_index`` and ``char_offset`` are ``None`` when
    the term could not be located in the text.

    **Scores are corpus-global.** ``namespace`` selects which documents come
    back, not how they rank: term rarity is computed over the whole index, so
    one paper scores identically in a filtered and an unfiltered search.

    ``normalize=True`` folds diacritics on both sides, so "cafe" and "café"
    rank identically. ``force_refresh=True`` re-indexes every document
    regardless of the ``(mtime, size)`` staleness signal — the safety valve for
    a file that changed without either changing.

    Raises ``sqlite3.Error`` rather than swallowing it, which would report a
    locked or corrupt index as a confident "no paper mentions this";
    ``search_cached_papers`` turns it into ``{error, suggestion}``.
    """
    if top_k <= 0:
        return []
    top_k = min(top_k, _MAX_TOP_K)
    # Gate on the MATCH expression, never on the word regex: a wholly non-Latin
    # query tokenises to nothing under that ASCII regex, and FTS5 would have
    # matched it.
    match_expr = _fts_query(query)
    if not match_expr:
        return []

    _refresh_index(force_refresh=force_refresh)

    table = "fts_norm" if normalize else "fts"

    # ``table`` is one of the two literals chosen above, never caller input,
    # and every value is bound with a ? placeholder.
    sql = (
        f"SELECT f.ns AS ns, f.stem AS stem, bm25({table}) AS score "  # noqa: S608
        f"FROM {table} JOIN files f ON f.rowid = {table}.rowid "
        f"WHERE {table} MATCH ?"
    )
    params: list[Any] = [match_expr]
    if namespace is not None:
        sql += " AND f.ns = ?"
        params.append(namespace)
    # Tie-break by (namespace, stem): FTS5 orders by rank alone, so equal
    # scores would fall back to insertion order, which drifts.
    sql += f" ORDER BY bm25({table}), f.ns, f.stem LIMIT ?"
    params.append(top_k)

    con = _connect()
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    unique_query_terms = _snippet_terms(query, normalize=normalize)
    out: list[dict[str, Any]] = []
    for row in rows:
        # bm25() is negative, most-relevant first; flip it so the response
        # reads "higher is better". No score floor: FTS5 returns only rows that
        # matched, so a low score means "matched on a term with little
        # discriminative value", not "did not match".
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
                # Significant figures, not decimal places: a degenerate IDF
                # clamps to 1e-6 and the length normalisation scales it below
                # 1e-7, which any fixed number of decimals reports as 0.0 —
                # breaking the invariant that every hit scores above zero.
                "score": float(f"{score:.6g}"),
                "title": title,
                "snippet": snippet,
                "section": section,
                # The chainable handle: `section` is a title, and a repeated
                # one is rejected as ambiguous.
                "section_index": section_index,
                "char_offset": snippet_offset,
                "char_count": len(text),
            }
        )
    return out
