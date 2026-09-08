"""Unicode folding for diacritic-insensitive search.

NFKD-normalize then drop combining marks so "cafe" matches "café". ``fold``
returns just the folded string; the ``*_with_map`` pair also returns an index
map back to ORIGINAL offsets, which ``original_span`` reads to slice a match
out of the untransformed text. Rationale in ``.claude/rules/utils.md``.
"""

import re
import unicodedata

# ASCII is NFKD-identity, non-combining and one-to-one under lower(), so such a
# run maps to itself index-for-index and skips the loop — ~4x on a whole paper.
_SEGMENT_RE = re.compile(r"[\x00-\x7f]+|[^\x00-\x7f]+")


def fold(text: str) -> str:
    """NFKD-fold ``text`` and strip combining marks. Case is left untouched."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """Fold ``text``: ``(fold(text), index_map)``."""
    return _transform_with_map(text, fold_marks=True, lower=False)


def lower_with_map(text: str, *, fold: bool = False) -> tuple[str, list[int]]:
    """Lowercase ``text``, NFKD-folding first when ``fold``: ``(lowered, index_map)``."""
    return _transform_with_map(text, fold_marks=fold, lower=True)


def _transform_with_map(text: str, *, fold_marks: bool, lower: bool) -> tuple[str, list[int]]:
    """Transform ``text``, plus each output char's index in the ORIGINAL string.

    ``index_map`` has length ``len(transformed) + 1``; ``[-1]`` is ``len(text)``,
    the sentinel that lets a match ending at the end of ``transformed`` reach the
    end of ``text``. ``original_span`` reads it.

    The string is always the *whole-string* transform. ``str.lower()`` is
    context-sensitive at a word-final Greek sigma, so lowercasing character by
    character would emit a string the tokeniser never produces and lose the
    match. Only the map is built per ORIGINAL character — that is what absorbs
    the length changes: a combining mark contributes no entry, "ﬁ" contributes
    two pointing at one index, 'İ' lowercases to two chars. The two halves stay
    in step because per-character and whole-string transforms always agree in
    *length*; that is pinned by property test, not asserted here.
    """
    folded = fold(text) if fold_marks else text
    transformed = folded.lower() if lower else folded

    index_map: list[int] = []
    for segment in _SEGMENT_RE.finditer(text):
        start, end = segment.span()
        if text[start].isascii():
            index_map.extend(range(start, end))
            continue
        for orig_idx in range(start, end):
            decomposed = (
                unicodedata.normalize("NFKD", text[orig_idx]) if fold_marks else text[orig_idx]
            )
            for char in decomposed:
                if fold_marks and unicodedata.combining(char):
                    continue
                index_map.extend((orig_idx,) * (len(char.lower()) if lower else 1))
    index_map.append(len(text))
    return transformed, index_map


def original_span(index_map: list[int], start: int, end: int) -> tuple[int, int]:
    """Original ``[lo, hi)`` covering the transformed span ``[start, end)``.

    Indexing the map twice is not enough: a span ending inside one character's
    expansion (the "f" of a folded "ﬁ") resolves both ends to the same index and
    slices to nothing, hence the widening. ``index_map[end]`` still wins when
    larger — it is the entry that swallows trailing combining marks, so "cafe"
    against a decomposed "café" keeps its accent.
    """
    lo = index_map[start]
    if end <= start:
        return lo, lo
    return lo, max(index_map[end], index_map[end - 1] + 1)
