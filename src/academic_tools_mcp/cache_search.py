"""BM25 keyword search over the converted-markdown cache.

The PDF pipeline (download_pdf → convert_paper) lands every paper's
markdown under ``.cache/<namespace>/markdown/<canonical>.md``. After
weeks of use this becomes the agent's actual reading history — but
without a search primitive, recovering "the paper that mentioned X"
means remembering the exact identifier.

This module walks every cached markdown file and ranks them against a
query using standard BM25 (k1=1.5, b=0.75). For the top hits it
extracts the document title (first H1/H2), a ~200-char snippet centred
on the highest-scoring matching term, and the H2 section the snippet
falls under so the agent can chain into ``get_paper_section``.

A persistent on-disk index (``.cache/__search_index__/index.json``)
holds each document's term frequencies keyed by a cheap ``os.stat``
staleness signal (``mtime_ns`` + ``size``), so a search only re-reads
and re-tokenises files that actually changed since the last call —
everything else is served straight from the index. The full corpus is
still *stat*-walked each call (cheap), and the top-``k`` winners are
re-read to extract snippets, but the O(corpus) read+tokenise that used
to run on every call is gone. The BM25 score is still computed in pure
Python — no embeddings, no third-party index. If keyword recall ever
becomes the bottleneck, embedding-based rerank is the natural
follow-up; if the index JSON parse itself becomes the bottleneck at
very large corpora, sharding it per-namespace is the next lever.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from . import _textnorm, cache

# Standard BM25 hyperparameters. k1 controls term-frequency saturation
# (higher = more weight to repeated terms); b controls length
# normalisation (1.0 = full, 0.0 = none). The Robertson defaults work
# well across mixed corpora and there's no signal here to tune them.
_BM25_K1 = 1.5
_BM25_B = 0.75

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
_INDEX_VERSION = 1
_INDEX_DIRNAME = "__search_index__"
_INDEX_FILENAME = "index.json"
_INDEX_LOCK = threading.Lock()
# NUL can't occur in a namespace dir name or a filename stem, so it is a
# collision-free separator for the flat entry key.
_INDEX_KEY_SEP = "\x00"

# In-memory memo of the last parsed index, keyed by index.json's own
# (mtime_ns, size). An unchanged corpus leaves index.json untouched, so a
# follow-up search reuses this parsed dict instead of re-reading and
# re-parsing the JSON (the O(corpus) parse that otherwise ran on every call).
# Copy-on-write: _refresh_index never mutates the memo dict in place — on a
# memo hit it shallow-copies the entries before mutating and swaps the memo to
# a fresh object — so a concurrent search still scoring a previously-returned
# dict is never disturbed. Both fields are guarded by _INDEX_LOCK, the same
# critical section as the file I/O. Tests reset them via conftest.
_INDEX_MEMO: dict[str, Any] | None = None
_INDEX_MEMO_SIG: tuple[int, int] | None = None


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


def _section_for_offset(markdown: str, offset: int) -> str | None:
    """Return the H1/H2 heading whose body contains ``offset``, or None.

    Matches papers.parse_sections' notion of "section" — H1 and H2 both
    open a new section, H3 doesn't. Used so a snippet hit can tell the
    agent which section to chain into via get_paper_section.
    """
    current: str | None = None
    for match in _HEADING_RE.finditer(markdown):
        if match.start() > offset:
            return current
        level = len(match.group(1))
        if level <= 2:
            current = match.group(2).strip()
    return current


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
# (e.g. "hep-th/9901001", "cs/0501001", "math.GT/0309136"). _canonical_arxiv_id
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
    if not root.exists():
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


# ---------------------------------------------------------------------------
# Persistent incremental index
# ---------------------------------------------------------------------------


def _index_path() -> Path:
    """Path to the on-disk index, resolved live against the cache root.

    Read ``cache._CACHE_ROOT`` on every call (never cache at import) so a
    test that monkeypatches the cache root keeps the index sandboxed.
    """
    return cache._CACHE_ROOT / _INDEX_DIRNAME / _INDEX_FILENAME


def _empty_index() -> dict[str, Any]:
    return {"version": _INDEX_VERSION, "entries": {}}


def _entry_key(namespace: str, stem: str) -> str:
    return f"{namespace}{_INDEX_KEY_SEP}{stem}"


def _load_index() -> dict[str, Any]:
    """Load and validate the index, self-healing to empty on any problem.

    A corrupt/unreadable JSON file, a version mismatch, or a malformed
    shape all return a fresh empty index — the next dirty save overwrites
    the bad file atomically, so no explicit unlink is needed. A version
    bump therefore forces a full rebuild (every file looks "new").
    """
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return _empty_index()
    if (
        not isinstance(data, dict)
        or data.get("version") != _INDEX_VERSION
        or not isinstance(data.get("entries"), dict)
    ):
        return _empty_index()
    return data


def _save_index(index: dict[str, Any]) -> None:
    """Persist the index atomically (reuses the shared cache helper)."""
    cache._atomic_write_json(_index_path(), json.dumps(index, ensure_ascii=False))


def _build_entry(namespace: str, path: Path, *, mtime_ns: int, size: int) -> dict[str, Any] | None:
    """Tokenise one markdown file into an index entry, or ``None`` to skip.

    Both tokenisation modes are computed and stored so a single index
    serves ``normalize=True`` and ``normalize=False`` queries without a
    re-tokenise on mode flip (normalised token frequencies cannot be
    derived from the un-normalised ones — diacritics change the token
    boundaries). Returns ``None`` for an unreadable file (skip, mirroring
    the old mid-walk skip) or a file with no content tokens in either mode
    (so it never enters the corpus, exactly as the old ``if not tokens``
    guard did).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    tokens = _tokenize(text)
    tokens_norm = _tokenize(text, normalize=True)
    if not tokens and not tokens_norm:
        return None
    return {
        "namespace": namespace,
        "stem": path.stem,
        "mtime_ns": mtime_ns,
        "size": size,
        "length": len(tokens),
        "length_norm": len(tokens_norm),
        "tf": dict(Counter(tokens)),
        "tf_norm": dict(Counter(tokens_norm)),
    }


def _index_sig() -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of index.json, or ``None`` if it doesn't exist.

    The staleness key for the in-memory memo: an unchanged corpus never
    rewrites index.json, so an unchanged signature means the parsed dict is
    still good. A missing file (``None``) compares equal across calls too, so
    an empty-corpus session also avoids re-parsing.
    """
    try:
        st = _index_path().stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _refresh_index(*, force_refresh: bool = False) -> dict[str, Any]:
    """Bring the on-disk index in sync with the markdown corpus.

    Stats every cached markdown file (cheap, no read); reuses an existing
    entry when ``(mtime_ns, size)`` are unchanged, otherwise re-reads and
    re-tokenises it; prunes entries whose file is gone. Persists only when
    something actually changed. Always walks the *full* corpus (never the
    namespace-filtered subset) so the index stays globally complete
    regardless of which namespace the calling search filtered on.

    The parsed index is memoised in ``_INDEX_MEMO`` keyed by index.json's stat
    signature, so an unchanged corpus skips the JSON re-parse entirely (the
    cheap stat-walk still runs as the change detector). Returns a per-call
    index dict whose ``entries`` are never mutated in place after return — a
    memo hit copies them before mutating — so callers score lock-free after
    this returns.
    """
    global _INDEX_MEMO, _INDEX_MEMO_SIG
    with _INDEX_LOCK:
        sig = _index_sig()
        if not force_refresh and _INDEX_MEMO is not None and sig == _INDEX_MEMO_SIG:
            # Memo hit: reuse the parsed dict, but copy entries before any
            # mutation so a prior caller still scoring the memo isn't touched.
            entries: dict[str, Any] = dict(_INDEX_MEMO["entries"])
        else:
            # Memo miss (or forced rebuild): parse from disk. This dict is
            # freshly built and held by no other caller, so it is safe to
            # mutate in place.
            entries = _load_index()["entries"]
        index: dict[str, Any] = {"version": _INDEX_VERSION, "entries": entries}
        dirty = False
        seen: set[str] = set()
        for ns, path in _iter_markdown_files(None):
            try:
                st = path.stat()
            except OSError:
                continue
            key = _entry_key(ns, path.stem)
            seen.add(key)
            existing = entries.get(key)
            if (
                not force_refresh
                and existing is not None
                and existing.get("mtime_ns") == st.st_mtime_ns
                and existing.get("size") == st.st_size
            ):
                continue
            entry = _build_entry(ns, path, mtime_ns=st.st_mtime_ns, size=st.st_size)
            if entry is None:
                # Unreadable or no-content file — drop any stale entry.
                if key in entries:
                    del entries[key]
                    dirty = True
                continue
            entries[key] = entry
            dirty = True
        # Prune entries whose markdown file no longer exists on disk.
        for key in [k for k in entries if k not in seen]:
            del entries[key]
            dirty = True
        if dirty:
            _save_index(index)
        # Refresh the memo to this now-current index. Re-stat after a save so
        # the signature matches the bytes we just wrote (a dirty save changes
        # index.json's mtime/size); on a clean pass the signature is unchanged.
        _INDEX_MEMO = index
        _INDEX_MEMO_SIG = _index_sig()
        return index


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
            "section": "Methods",          # H1/H2 the snippet falls under
            "char_count": 48217,           # full markdown length
        }

    Hits with score 0 (no query term appeared) are dropped — returning
    them would just inflate the response without helping the agent.
    Term frequencies are served from a persistent incremental index
    (``_refresh_index``): only files that changed since the last call are
    re-read and re-tokenised, and only the ``top_k`` winners are re-read
    to extract snippets. The output is identical to a fresh full scan.

    ``normalize=True`` NFKD-folds the query and every document (and the
    snippet locator) before tokenising, so "cafe" and "café" rank
    identically. Both folded and un-folded term frequencies live in the
    index, so flipping ``normalize`` never forces a re-tokenise.

    ``force_refresh=True`` rebuilds every index entry from disk regardless
    of the staleness signal — a safety valve for the rare case where a
    file changed without its ``mtime``/``size`` changing.
    """
    if top_k <= 0:
        return []
    top_k = min(top_k, _MAX_TOP_K)
    query_tokens = _tokenize(query, normalize=normalize)
    if not query_tokens:
        return []

    index = _refresh_index(force_refresh=force_refresh)

    # Build the working doc set from the index, selecting the term
    # frequencies for the requested mode. Skip entries with no tokens in
    # this mode (a doc whose only content is accented surfaces under
    # normalize=True but is invisible under normalize=False, and vice
    # versa) so the corpus stats match the old per-mode scan exactly.
    # Namespace filtering happens here so avgdl/df stay scoped to the
    # filtered subset, as before.
    tf_field = "tf_norm" if normalize else "tf"
    len_field = "length_norm" if normalize else "length"
    docs: list[dict[str, Any]] = []
    total_length = 0
    for entry in index["entries"].values():
        if namespace is not None and entry["namespace"] != namespace:
            continue
        tf = entry[tf_field]
        if not tf:
            continue
        docs.append(
            {
                "namespace": entry["namespace"],
                "stem": entry["stem"],
                "tf": tf,
                "length": entry[len_field],
            }
        )
        total_length += entry[len_field]

    if not docs:
        return []

    avgdl = total_length / len(docs)
    n_docs = len(docs)

    # Document frequency for each query term. Using a set to dedupe
    # query tokens, since "neural neural network" should still only
    # count "neural" once toward IDF.
    unique_query_terms = set(query_tokens)
    df: dict[str, int] = {}
    for term in unique_query_terms:
        df[term] = sum(1 for d in docs if term in d["tf"])

    # BM25 scoring. The +0.5 / +0.5 IDF smoothing is the Robertson
    # form, which is non-negative and avoids the "common term punishes
    # documents that contain it" pathology of plain log(N/df).
    def _bm25(doc: dict[str, Any]) -> float:
        score = 0.0
        for term in unique_query_terms:
            term_df = df[term]
            if term_df == 0:
                continue
            idf = math.log(1 + (n_docs - term_df + 0.5) / (term_df + 0.5))
            tf = doc["tf"].get(term, 0)
            if tf == 0:
                continue
            denom = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * doc["length"] / avgdl)
            score += idf * (tf * (_BM25_K1 + 1)) / denom
        return score

    # Sort by score descending, breaking ties by (namespace, stem) so the
    # output is deterministic regardless of the order entries happened to land
    # in the incremental index (a changed file keeps its slot, a new file is
    # appended — so insertion order drifts as the corpus evolves).
    scored = [(_bm25(d), d) for d in docs]
    scored.sort(key=lambda x: (-x[0], x[1]["namespace"], x[1]["stem"]))

    out: list[dict[str, Any]] = []
    for score, doc in scored[:top_k]:
        if score <= 0:
            break
        # Re-read only the winners (≤ top_k ≤ 50) to extract the snippet,
        # section, and title. Title is recomputed from this read rather
        # than cached in the index so the output is byte-identical to a
        # fresh scan. A winner that vanished since the refresh is skipped.
        path = cache._CACHE_ROOT / doc["namespace"] / "markdown" / f"{doc['stem']}.md"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = _extract_title(text)
        snippet, snippet_offset = _extract_snippet(text, unique_query_terms, normalize=normalize)
        section = _section_for_offset(text, snippet_offset) if snippet_offset is not None else None
        canonical_id = _filename_to_canonical(doc["namespace"], doc["stem"])
        out.append(
            {
                "namespace": doc["namespace"],
                "canonical_id": canonical_id,
                "score": round(score, 3),
                "title": title,
                "snippet": snippet,
                "section": section,
                "char_count": len(text),
            }
        )
    return out
