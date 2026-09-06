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
from typing import Any, NamedTuple
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


# ---------------------------------------------------------------------------
# Titles and snippets — what a hit shows
# ---------------------------------------------------------------------------


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


def _extract_snippet(
    markdown: str,
    query_terms: set[str],
    *,
    normalize: bool = False,
) -> tuple[str, int | None]:
    """Return ``(snippet, char_offset)`` for the best matching position.

    "Best matching" = the position with the most distinct query terms in the
    surrounding window, so "variational dropout" cooccurrence beats a lone
    "dropout". With nothing to centre on, returns the document head and a
    ``None`` offset — the caller must not attribute a section to that.

    ``char_offset`` indexes the ORIGINAL markdown under either normalisation.
    """
    half = _SNIPPET_CHARS // 2
    hits: list[tuple[int, str]] = []
    if query_terms:
        # Not a raw str.lower(): 'İ' lowercases to two chars, so an unmapped
        # m.start() drifts past the match.
        lowered, index_map = _textnorm.lower_with_map(markdown, fold=normalize)
        # Longest first — \b settles "attention" against "attentions" on its own,
        # but not a split on the hyphen or dot _content_tokens keeps intact.
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
    # Collapsed, or one crossing a heading renders as "## Methods\n\n\n\nWe trained...".
    return re.sub(r"\s+", " ", snippet.strip()), best_offset


# ---------------------------------------------------------------------------
# Filename → identifier inversion per namespace
# ---------------------------------------------------------------------------

# A DOI suffix may legitimately contain "_", so only a slash a known prefix
# introduced is decidable. These namespaces have exactly one such prefix.
_NAMESPACE_DOI_PREFIXES = {"biorxiv": "10.1101/", "acl_anthology": "10.18653/v1/"}

# manual holds publisher DOIs, not the freeform labels its name suggests.
_MANUAL_DOI_STEM_RE = re.compile(rf"^({_doi.REGISTRANT_PATTERN})_")

# "archive[.subject]_NNNNNNN[vN]"; new-style ids start with a digit and pass through.
_ARXIV_OLDSTYLE_STEM_RE = re.compile(r"^([a-z][a-z.\-]*)_(\d{7}(?:v\d+)?)$")


def _filename_to_canonical(namespace: str, stem: str) -> str:
    """Invert ``papers.safe_stem``: the cache key a stored filename came from.

    One ``unquote``, and it must stay one — ``safe_stem`` writes a literal
    ``%`` as ``%25``, so a single pass is its exact inverse and a second would
    decode an escape that was never one.
    """
    return unquote(_restore_slashes(namespace, stem))


def _restore_slashes(namespace: str, stem: str) -> str:
    """Undo ``safe_stem``'s ``"/" -> "_"`` mapping, as far as it is decidable."""
    if namespace == "arxiv":
        return _ARXIV_OLDSTYLE_STEM_RE.sub(r"\1/\2", stem, count=1)
    if namespace == "manual":
        return _MANUAL_DOI_STEM_RE.sub(r"\1/", stem, count=1)
    prefix = _NAMESPACE_DOI_PREFIXES.get(namespace, "")
    stemmed = prefix.replace("/", "_")
    if prefix and stem.startswith(stemmed):
        return prefix + stem[len(stemmed) :]
    return stem


# ---------------------------------------------------------------------------
# The corpus on disk
# ---------------------------------------------------------------------------


class _ScannedFile(NamedTuple):
    """One cached markdown file, as the refresh needs it.

    ``path`` stays a ``str``: the refresh reads only the files whose stat
    changed, so building a ``Path`` for every one of them is most of the walk.
    """

    namespace: str
    stem: str
    path: str
    mtime_ns: int
    size: int


def _scan_markdown() -> list[_ScannedFile]:
    """Every cached markdown file on disk.

    ``os.scandir`` carries the stat the refresh needs, so it isn't fetched
    twice. Order is not guaranteed — the refresh keys on ``(namespace, stem)``.

    **Must stay unfiltered.** ``_refresh_index`` prunes every indexed row this
    walk did not return, so a namespace filter would delete every other
    namespace's postings.
    """
    out: list[_ScannedFile] = []
    try:
        # A missing or non-directory root raises here, which is the same
        # "nothing cached yet" answer as an empty one.
        with os.scandir(cache._CACHE_ROOT) as namespaces:
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


# ---------------------------------------------------------------------------
# Persistent incremental index
# ---------------------------------------------------------------------------

_INDEX_DIRNAME = "__search_index__"

# Bump when the row schema or the tokenizer changes; a mismatch rebuilds.
_SCHEMA_VERSION = 3

# Never equals a real stat, so a file that failed to read is retried.
_UNREADABLE_MTIME = -1

# One Unicode letter or digit: ``\w`` minus underscore, which is what
# ``unicode61`` treats as a token character, in any script.
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
    return cache._CACHE_ROOT / _INDEX_DIRNAME / "index.db"


def _legacy_index_path() -> Path:
    """Path to the pre-FTS5 JSON index, which ``_sweep_legacy_index`` removes."""
    return cache._CACHE_ROOT / _INDEX_DIRNAME / "index.json"


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
        # Whatever went wrong, don't leave the connection open for `_connect`
        # to unlink the file out from under.
        con.close()
        raise
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Create the schema, rebuilding from scratch on a version mismatch.

    A no-op — and a read-only one — when the recorded version already matches.
    """
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"]) if row else None
    except (sqlite3.DatabaseError, ValueError, TypeError):
        version = None

    if version == _SCHEMA_VERSION:
        # Written last, so its presence means the tables exist. Keeps the open
        # read-only; otherwise a search opens three write transactions.
        return

    # An unreadable version says nothing about the tables under it: rebuild,
    # don't certify.
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
    """Replace one document's postings. Returns an ``unindexable`` reason or None.

    A document with no terms is left out of both tables rather than inserted
    and then declared unusable, so it is absent exactly like an unreadable one.
    """
    con.execute("DELETE FROM fts WHERE rowid = ?", (rowid,))
    con.execute("DELETE FROM fts_norm WHERE rowid = ?", (rowid,))
    # Must agree with ``unicode61`` on what a term is: an ASCII-biased probe
    # calls a Japanese or Cyrillic paper unusable when the index holds it fine.
    if _ALNUM_RE.search(text) is None:
        return "no_indexable_tokens"
    con.execute("INSERT INTO fts(rowid, body) VALUES (?, ?)", (rowid, text))
    con.execute("INSERT INTO fts_norm(rowid, body) VALUES (?, ?)", (rowid, text))
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
                (row["ns"], row["stem"]): _IndexedFile(row["rowid"], row["mtime_ns"], row["size"])
                for row in con.execute("SELECT rowid, ns, stem, mtime_ns, size FROM files")
            }
            seen: set[tuple[str, str]] = set()

            with con:
                for found in _scan_markdown():
                    key = (found.namespace, found.stem)
                    seen.add(key)
                    existing = known.get(key)
                    if (
                        not force_refresh
                        and existing is not None
                        and existing.mtime_ns == found.mtime_ns
                        and existing.size == found.size
                    ):
                        continue
                    reason: str | None
                    try:
                        text = Path(found.path).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        reason, text = "unreadable", ""
                    else:
                        reason = None

                    # Storing the stat that succeeded freezes the failure: a
                    # chmod leaves mtime alone, so no retry would ever fire.
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
                    # Always: it drops the old postings, and adds none for the
                    # empty text an unreadable file leaves behind.
                    probed = _index_document(con, rowid, text)
                    con.execute(
                        "UPDATE files SET unindexable = ? WHERE rowid = ?",
                        (reason or probed, rowid),
                    )

                # Every indexed row the walk missed — why it stays unfiltered.
                for key, row in known.items():
                    if key in seen:
                        continue
                    con.execute("DELETE FROM fts WHERE rowid = ?", (row.rowid,))
                    con.execute("DELETE FROM fts_norm WHERE rowid = ?", (row.rowid,))
                    con.execute("DELETE FROM files WHERE rowid = ?", (row.rowid,))
        finally:
            con.close()


def unindexable(namespace: str | None = None, *, refresh: bool = True) -> list[dict[str, Any]]:
    """Papers present on disk that the index could not use.

    Each record is ``{namespace, stem, reason}``, where ``reason`` is
    ``"no_indexable_tokens"`` or ``"unreadable"``. Such papers are invisible
    to ``search`` correctly but *silently*, which is what this fixes.

    ``refresh=False`` is a contract, not an optimisation: it is how
    ``search_cached_papers`` reads the state the ``search`` it just ran left
    behind, instead of walking the corpus a second time.
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
    return [{"namespace": r["ns"], "stem": r["stem"], "reason": r["unindexable"]} for r in rows]


# ---------------------------------------------------------------------------
# Query and ranking
# ---------------------------------------------------------------------------

# ``unicode61``'s separators, so the query splits the way the corpus did — and
# sqlite3 cannot bind a string containing a NUL at all.
_QUERY_SPLIT_RE = re.compile(r"[\s\x00]+")


def _query_words(query: str) -> list[str]:
    """The query's words, as handed to FTS5 — split, then filtered.

    Filtered here rather than in ``_content_tokens``, whose ASCII-only regex
    would drop a non-Latin word the index holds. Filtered at all because
    ``unicode61`` strips neither stopwords nor single characters, so an
    unfiltered "the" ORs in a term matching the whole corpus.
    """
    return [
        word
        for word in _QUERY_SPLIT_RE.split(query.strip())
        # len(word), not len(word.lower()): 'İ' lowercases to two characters
        # and would slip through a filter meant to drop single characters.
        if len(word) > 1 and word.lower() not in _STOPWORDS
    ]


def _fts_query(query: str) -> str:
    """Build an FTS5 MATCH expression OR-ing the query's words.

    Each word is quoted so FTS5 reads it as a phrase, not syntax: an unquoted
    ``NOT``, ``OR``, ``*``, ``-`` or ``:`` parses as an operator, or raises.
    Inside a phrase only ``"`` is special, escaped by doubling.

    Returns ``""`` when nothing survives filtering, which the caller must treat
    as an empty result — an empty MATCH expression is a syntax error to FTS5.
    """
    # Keyed case-insensitively because FTS5 is — a term ORed with its own other
    # spelling scores twice. Emitted as typed: folding is the tokenizer's job.
    by_token: dict[str, str] = {}
    for word in _query_words(query):
        by_token.setdefault(word.lower(), word)
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in by_token.values())


def _snippet_terms(query: str, *, normalize: bool) -> set[str]:
    """Terms used to centre the snippet on the best-matching passage.

    Two views, because neither alone survives ``_extract_snippet``'s
    word-boundary scan: ``_content_tokens`` strips punctuation a raw word
    carries in ("transformer." never matches), and the raw words keep what its
    ASCII-only pattern mangles ("Gutiérrez" into "guti"/"rrez").
    """
    return _content_tokens(query, normalize=normalize) | {
        (_textnorm.fold(word) if normalize else word).lower() for word in _query_words(query)
    }


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
    # Gate on the MATCH expression, never the word regex: a non-Latin query
    # tokenises to nothing under it, and FTS5 would have matched.
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

    terms = _snippet_terms(query, normalize=normalize)
    out: list[dict[str, Any]] = []
    for row in rows:
        # bm25() is negative, most-relevant first. No floor: FTS5 returns only
        # rows that matched, so a low score is a weak term, not a non-match.
        score = -float(row["score"])
        path = cache.cache_dir(row["ns"], "markdown") / f"{row['stem']}.md"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = _extract_title(text)
        snippet, snippet_offset = _extract_snippet(text, terms, normalize=normalize)
        # Never a local heading scan: a copy of papers' drops the empty-section
        # filter and names a section get_paper_section would refuse.
        found = (
            papers.section_at_offset(text, snippet_offset) if snippet_offset is not None else None
        )
        section_index, section = found or (None, None)
        out.append(
            {
                "namespace": row["ns"],
                "canonical_id": _filename_to_canonical(row["ns"], row["stem"]),
                # Significant figures, not decimals: a degenerate IDF scales
                # below 1e-7, which any fixed decimals report as 0.0.
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
