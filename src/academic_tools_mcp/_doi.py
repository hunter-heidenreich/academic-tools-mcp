"""Shared DOI normalization — the single home for it.

**Never add a local copy.** Divergent normalization is not a cosmetic problem. It lands one paper under
several cache keys depending on which tool the agent called first, and a key
that is not a bare DOI builds a malformed upstream URL:

    canonical_doi("https://dx.doi.org/10.1234/x")   must not stay a URL
        (openalex would fetch it as /works/doi:https://...)
    canonical_doi("DOI:10.1234/x")                  must not keep the prefix
    normalize("doi: 10.1234/x")                     must re-strip the space,
        or the result fails the DOI regex and the agent is told the
        identifier is unrecognised

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
