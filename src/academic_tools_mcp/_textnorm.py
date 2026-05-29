"""Unicode folding for diacritic-insensitive search.

NFKD-normalize then drop combining marks so "cafe" matches "café" and
vice versa. Two entry points:

- ``fold(text)`` — the folded string, for tokenisation / IDF where the
  character position doesn't matter.
- ``fold_with_map(text)`` — the folded string plus an index map back to
  ORIGINAL character offsets, so a match found in folded text can be
  sliced out of the original text with correct offsets.

NFKD changes length: combining marks contribute zero folded chars, and a
ligature like "ﬁ" expands to "fi" (one original char → two folded
chars). The index map carries enough information to invert either case.

This mirrors — but does not repurpose — ``bibtex._strip_accents_for_key``,
which folds for citation-key generation where offsets are irrelevant.
"""

from __future__ import annotations

import unicodedata


def fold(text: str) -> str:
    """NFKD-fold ``text`` and strip combining marks.

    Case is left untouched — folding is independent of case-folding;
    callers that want case-insensitivity apply ``re.IGNORECASE`` (or
    ``.lower()``) separately.
    """
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """Fold ``text`` and return ``(folded, index_map)``.

    ``index_map`` has length ``len(folded) + 1``. For a match found at
    folded ``[start, end)``, the corresponding ORIGINAL span is
    ``[index_map[start], index_map[end])``:

    - ``index_map[i]`` = the original-string index that folded char ``i``
      derives from.
    - ``index_map[len(folded)]`` = ``len(text)`` (the end sentinel), so a
      match ending at the very end of the folded text maps to the end of
      the original text.

    NFKD is applied per original character so each folded char is
    attributed to exactly one original index:

    - A combining mark folds to "" → contributes no folded char and no
      map entry (it is skipped).
    - A base char folds to one char → one map entry pointing at it.
    - A ligature ("ﬁ" → "fi") folds to N chars → N map entries, all
      pointing at the single original index. A match covering only part
      of the expansion still maps to that original index, so the reported
      original span is the whole original char (correct — you can't
      address half of "ﬁ").

    Edge cases: empty input → ``("", [0])``; leading/trailing combining
    marks are skipped; input that is entirely combining marks folds to
    ``("", [0])`` as well.
    """
    folded_parts: list[str] = []
    index_map: list[int] = []
    for orig_idx, ch in enumerate(text):
        for d in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(d):
                continue
            folded_parts.append(d)
            index_map.append(orig_idx)
    index_map.append(len(text))  # end sentinel
    return "".join(folded_parts), index_map
