"""Shared DOI normalization — the single home for it.

**Never add a local copy.** Divergent normalization lands one paper under
several cache keys, and a key that is not a bare DOI builds a malformed
upstream URL: leave `https://dx.doi.org/10.1234/x` as a URL and OpenAlex
fetches `/works/doi:https://...`.

Per-provider *policy* (which prefix a URL path needs, whether an ID is an
Anthology ID) stays in the provider — only the normalization is shared.
"""

import re

# Exported: `cache_search` inverts a stored filename stem with this same
# pattern, and a second spelling would let the two disagree on what a DOI is.
REGISTRANT_PATTERN = r"10\.\d{4,}"

# The URL forms publishers and reference managers actually emit.
_DOI_URL_RE = re.compile(
    rf"https?://(?:dx\.|www\.)?doi\.org/({REGISTRANT_PATTERN}/[^\s?#]+)(?:[?#].*)?$",
    re.IGNORECASE,
)

_DOI_RE = re.compile(rf"^{REGISTRANT_PATTERN}/\S+$")


def normalize(doi: str) -> str:
    """Normalize a DOI to bare form (``10.1234/example``).

    Accepts a bare DOI, a ``doi:`` prefix in any case, and a ``doi.org`` /
    ``dx.doi.org`` / ``www.doi.org`` URL over http or https. A URL's query
    string and fragment are discarded; a bare DOI keeps a literal ``?`` or
    ``#``, which are legal suffix characters.

    A string that is not recognisably a DOI is returned stripped but
    otherwise untouched — callers decide whether that is an error.

    Idempotent: ``normalize(normalize(s)) == normalize(s)`` for every input.
    """
    doi = doi.strip()

    # Prefix before URL: "doi:https://doi.org/10.x/y" occurs in the wild.
    while doi[:4].lower() == "doi:":
        doi = doi[4:].strip()

    if m := _DOI_URL_RE.match(doi):
        return m.group(1)
    return doi


def canonical(doi: str) -> str:
    """Return the cache-key form: ``normalize`` plus a lowercase fold.

    DOIs are case-insensitive by spec, so the key folds case; ``normalize``
    doesn't, because a request keeps whatever case the caller supplied.
    """
    return normalize(doi).lower()


def looks_like_doi(identifier: str) -> bool:
    """Whether ``identifier`` normalizes to something DOI-shaped."""
    return bool(_DOI_RE.match(normalize(identifier)))
