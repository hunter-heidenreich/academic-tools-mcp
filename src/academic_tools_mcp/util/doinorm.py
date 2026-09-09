"""The single home for DOI normalization.

**Never add a local copy** — of these functions or of the DOI-shape regex.
Divergent normalization keys one paper several ways, and a non-bare key builds
a malformed upstream URL: OpenAlex fetches `/works/doi:https://dx.doi.org/...`.

Per-provider *policy* (which prefix a URL path needs, whether an ID is an
Anthology ID) stays in the provider — only the normalization is shared.
"""

import re

# Exported: `corpus` inverts a stored filename stem with this same pattern.
REGISTRANT_PATTERN = r"10\.\d{4,}"

# The URL forms publishers and reference managers actually emit.
_DOI_URL_RE = re.compile(
    rf"https?://(?:dx\.|www\.)?doi\.org/({REGISTRANT_PATTERN}/[^\s?#]+)(?:[?#].*)?$",
    re.IGNORECASE,
)

_DOI_RE = re.compile(rf"^{REGISTRANT_PATTERN}/\S+$")


def normalize(doi: str) -> str:
    """Normalize a DOI to bare form (``10.1234/example``).

    Accepts a bare DOI, an any-case ``doi:`` prefix, and a resolver URL (hosts and
    schemes per ``_DOI_URL_RE``). A URL's query and fragment are cut; a bare DOI keeps
    a literal ``?``/``#`` — legal suffix characters, so cutting would key another paper.

    Anything unrecognised comes back stripped of whitespace and any ``doi:``
    prefix; the caller decides whether that's an error.

    Idempotent: ``normalize(normalize(s)) == normalize(s)`` for every input.
    """
    doi = doi.strip()

    # Prefix before URL, and in a loop: both "doi:https://doi.org/10.x/y" and "doi:doi:" occur.
    while doi[:4].lower() == "doi:":
        doi = doi[4:].strip()

    if m := _DOI_URL_RE.match(doi):
        return m.group(1)
    return doi


def canonical(doi: str) -> str:
    """Cache-key form: ``normalize`` plus a case fold.

    DOIs are case-insensitive by spec; ``normalize`` doesn't fold because the
    request keeps the caller's case.
    """
    return normalize(doi).lower()


def looks_like_doi(identifier: str) -> bool:
    """Whether ``identifier`` normalizes to something DOI-shaped."""
    return bool(_DOI_RE.match(normalize(identifier)))
