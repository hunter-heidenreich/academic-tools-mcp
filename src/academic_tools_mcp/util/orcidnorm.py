"""The single home for ORCID normalization.

**Never add a local copy** — of these functions or of the shape regex. An ORCID
reaches us in four spellings and only the ``https://orcid.org/...`` one resolves
upstream, so a divergent normalizer keys one person several ways.

Per-provider *policy* (which prefix a URL path needs) stays in the provider.
"""

import re

# 16 digits in four groups; the final check character may be `X` (ISO 7064 mod 11-2).
_ORCID_BODY = r"\d{4}-\d{4}-\d{4}-\d{3}[\dXx]"

_ORCID_URL_RE = re.compile(
    rf"^(?:https?://)?(?:www\.)?orcid\.org/({_ORCID_BODY})/?(?:[?#].*)?$",
    re.IGNORECASE,
)

_ORCID_RE = re.compile(rf"^{_ORCID_BODY}$")


def normalize(orcid: str) -> str:
    """Normalize an ORCID to bare form (``0000-0002-1825-0097``).

    Accepts a bare ORCID, an any-case ``orcid:`` prefix, and an orcid.org URL in
    either scheme. Anything unrecognised comes back stripped of whitespace and
    any prefix; the caller decides whether that's an error.

    Idempotent: ``normalize(normalize(s)) == normalize(s)`` for every input.
    """
    orcid = orcid.strip()

    # Prefix before URL, and in a loop: "orcid:https://orcid.org/..." occurs in pasted citations.
    while orcid[:6].lower() == "orcid:":
        orcid = orcid[6:].strip()

    if m := _ORCID_URL_RE.match(orcid):
        return m.group(1)
    return orcid


def canonical(orcid: str) -> str:
    """Cache-key form: ``normalize`` plus a case fold.

    OpenAlex resolves the check character in either case, so folding it cannot
    cost a lookup — and not folding it keys one person twice.
    """
    return normalize(orcid).lower()


def looks_like_orcid(identifier: str) -> bool:
    """Whether ``identifier`` normalizes to something ORCID-shaped."""
    return bool(_ORCID_RE.match(normalize(identifier)))
