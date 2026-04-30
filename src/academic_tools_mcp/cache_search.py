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

The corpus is small (tens to hundreds of papers for a personal MCP),
the per-doc tokenisation is cheap, and the BM25 score is computed in
pure Python in a single pass — no embeddings, no external index, no
new dependencies. If keyword recall ever becomes the bottleneck,
embedding-based rerank is the natural follow-up.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import cache

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
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at",
    "for", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "this", "that", "these", "those", "it", "its",
    "we", "our", "their", "them", "they", "he", "she", "his", "her",
    "i", "you", "your", "if", "then", "than", "so", "such", "into",
    "about", "over", "under", "between",
})

# Heading regex matching the same shape used by papers.parse_sections.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def _tokenize(text: str) -> list[str]:
    """Lowercase, drop stopwords, return a list of content tokens.

    Preserves intra-word hyphens and dots so domain terms like
    ``self-attention`` and ``BM25`` survive intact.
    """
    return [
        tok
        for tok in _TOKEN_RE.findall(text.lower())
        if tok not in _STOPWORDS and len(tok) > 1
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
) -> tuple[str, int | None]:
    """Return ``(snippet, char_offset)`` for the best matching position.

    "Best matching" = the position with the most distinct query terms
    in the surrounding window (so we prefer "variational dropout"
    cooccurrence over a lone "dropout"). Falls back to the document
    head if no term appears at all.
    """
    if not query_terms:
        return markdown[:window].strip(), 0

    # Find every occurrence of every query term, collecting (offset, term).
    # Word-boundary match so "drop" doesn't hit inside "dropout".
    hits: list[tuple[int, str]] = []
    lowered = markdown.lower()
    for term in query_terms:
        # Escape regex metacharacters in the term itself.
        pattern = re.compile(rf"\b{re.escape(term)}\b")
        for m in pattern.finditer(lowered):
            hits.append((m.start(), term))

    if not hits:
        return markdown[:window].strip(), None

    # Score each hit by counting distinct query terms within ±window/2
    # chars. Sort hits by offset so the sliding window stays linear.
    hits.sort()
    half = window // 2
    best_offset = hits[0][0]
    best_distinct = 1
    # Two-pointer sweep: for each hit, how many distinct terms fall
    # inside [hit - half, hit + half]?
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
    # arxiv canonical IDs have no slashes (e.g. "2301.00001",
    # "hep-th/9901001" canonicalises to lowercase but the slash is
    # stripped by _canonical_arxiv_id only via re.sub on version
    # suffixes — actual old-style IDs DO carry a slash). Both forms
    # write through the same `replace("/", "_")` step, so we restore
    # the one slash for old-style IDs and leave new-style alone.
    "arxiv": [("hep-th_", "hep-th/"), ("hep-ph_", "hep-ph/"),
              ("astro-ph_", "astro-ph/"), ("cond-mat_", "cond-mat/"),
              ("gr-qc_", "gr-qc/"), ("nucl-th_", "nucl-th/"),
              ("math-ph_", "math-ph/"), ("quant-ph_", "quant-ph/")],
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


def _filename_to_canonical(namespace: str, stem: str) -> str:
    """Invert ``canonical.replace("/", "_")`` for the given namespace.

    ``stem`` is the filename without the ``.md`` extension. Returns the
    canonical form the original code would have used as a cache key.
    """
    repairs = _NAMESPACE_PREFIX_REPAIRS.get(namespace, [])
    for needle, replacement in repairs:
        if stem.startswith(needle):
            return replacement + stem[len(needle):]
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


def search(
    query: str,
    *,
    top_k: int = 10,
    namespace: str | None = None,
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
    The corpus is read fresh on every call; for the personal-MCP scale
    this is well under 100ms and avoids any index-staleness concerns.
    """
    top_k = max(1, min(top_k, _MAX_TOP_K))
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    files = _iter_markdown_files(namespace)
    if not files:
        return []

    # First pass: tokenise each doc, collect term-frequency Counters and
    # document lengths. Memory cost is one Counter per doc — fine for
    # tens to hundreds of papers.
    docs: list[dict[str, Any]] = []
    total_length = 0
    for ns, path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # A file that vanished mid-walk (concurrent eviction, etc.)
            # — skip it rather than fail the whole search.
            continue
        tokens = _tokenize(text)
        if not tokens:
            continue
        tf = Counter(tokens)
        docs.append({
            "namespace": ns,
            "path": path,
            "text": text,
            "tf": tf,
            "length": len(tokens),
        })
        total_length += len(tokens)

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
            denom = tf + _BM25_K1 * (
                1 - _BM25_B + _BM25_B * doc["length"] / avgdl
            )
            score += idf * (tf * (_BM25_K1 + 1)) / denom
        return score

    scored = [(_bm25(d), d) for d in docs]
    scored.sort(key=lambda x: x[0], reverse=True)

    out: list[dict[str, Any]] = []
    for score, doc in scored[:top_k]:
        if score <= 0:
            break
        text = doc["text"]
        title = _extract_title(text)
        snippet, snippet_offset = _extract_snippet(text, unique_query_terms)
        section = (
            _section_for_offset(text, snippet_offset)
            if snippet_offset is not None
            else None
        )
        canonical_id = _filename_to_canonical(doc["namespace"], doc["path"].stem)
        out.append({
            "namespace": doc["namespace"],
            "canonical_id": canonical_id,
            "score": round(score, 3),
            "title": title,
            "snippet": snippet,
            "section": section,
            "char_count": len(text),
        })
    return out
