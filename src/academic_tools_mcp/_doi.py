"""Shared DOI normalization.

Six copies of this logic had accumulated — ``providers/crossref``,
``providers/opencitations``, ``providers/openalex``, ``providers/acl_anthology``,
``providers/biorxiv``, and ``manual`` — four of them byte-identical. The two
that had been improved (``manual``, ``biorxiv``) were the only ones that
handled ``dx.doi.org`` and a case-insensitive ``doi:`` prefix, so the same
paper could land under three different cache keys depending on which tool the
agent called first, and two of those keys built malformed upstream URLs:

    openalex.canonical_doi("https://dx.doi.org/10.1234/x")
        -> "https://dx.doi.org/10.1234/x"      (fetched as /works/doi:https://...)
    openalex.canonical_doi("DOI:10.1234/x")    -> "doi:10.1234/x"

All six also sliced the ``doi:`` prefix without re-stripping, so a pasted
``"doi: 10.1234/x"`` became ``" 10.1234/x"``, failed the DOI regex, and was
reported to the agent as an unknown identifier.

This module is the single home, carrying the union of what those six did.
Per-provider *policy* (which prefix a URL path needs, whether an ID is an
Anthology ID) stays in the provider — only the normalization is shared.
"""

from __future__ import annotations

import re

# A DOI URL in any of the forms publishers and reference managers emit:
# http/https, with or without the `dx.` host prefix, and with a query string
# or fragment that is not part of the DOI.
_DOI_URL_RE = re.compile(
    r"https?://(?:dx\.)?doi\.org/(10\.\d{4,}/[^\s?#]+)(?:[?#].*)?$",
    re.IGNORECASE,
)

# Bare DOI shape, used to decide whether a freeform identifier *is* a DOI.
_DOI_RE = re.compile(r"^10\.\d{4,}/\S+$")


def normalize(doi: str) -> str:
    """Normalize a DOI to bare form (``10.1234/example``).

    Accepts a bare DOI, a ``doi:`` prefix in any case (with or without a
    space after the colon), and a ``doi.org`` / ``dx.doi.org`` URL over http
    or https, with any query string or fragment discarded.

    A string that is not recognisably a DOI is returned stripped but
    otherwise untouched — callers decide whether that is an error.
    """
    doi = doi.strip()

    # Prefix first: "doi:https://doi.org/10.x/y" occurs in the wild.
    if doi.lower().startswith("doi:"):
        # Re-strip: "doi: 10.1234/x" would otherwise keep its leading space
        # and fail every downstream DOI check.
        doi = doi[len("doi:") :].strip()

    m = _DOI_URL_RE.match(doi)
    if m:
        return m.group(1)
    return doi


def canonical(doi: str) -> str:
    """Return the canonical lowercase DOI used as a cache key.

    DOIs are case-insensitive by specification, so the key is lowercased.
    The *request* keeps whatever case the caller supplied where a provider
    needs it.
    """
    return normalize(doi).lower()


def looks_like_doi(identifier: str) -> bool:
    """Whether ``identifier`` normalizes to something DOI-shaped."""
    return bool(_DOI_RE.match(normalize(identifier)))
