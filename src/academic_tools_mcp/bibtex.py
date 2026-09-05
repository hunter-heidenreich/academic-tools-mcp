"""BibTeX rendering: citation keys, name formatting and LaTeX escaping."""

import re
from collections.abc import Callable, Iterable
from typing import Any

from ._textnorm import fold

# OpenAlex type -> BibTeX entry type
_TYPE_MAP: dict[str, str] = {
    "article": "article",
    "review": "article",
    "letter": "article",
    "editorial": "article",
    "erratum": "article",
    "preprint": "misc",
    "posted-content": "misc",
    "book": "book",
    "book-chapter": "incollection",
    "monograph": "book",
    "dissertation": "phdthesis",
    "proceedings-article": "inproceedings",
    "proceedings": "proceedings",
    "report": "techreport",
    "standard": "misc",
    "dataset": "misc",
    "other": "misc",
}

# Common surname particles. "al"/"el" are intentionally included for Arabic
# "al-" and Spanish "el"; they can false-positive on a rare standalone token,
# an accepted trade-off for correct handling of "de la", "van der", etc.
_PARTICLES = {"van", "von", "de", "del", "della", "di", "la", "le", "den", "der", "el", "al"}

# Stopwords skipped when picking the first significant title word for a key.
_TITLE_SKIP = {"a", "an", "the", "on", "in", "of", "for", "to", "with", "and", "or"}

# Characters with no NFKD decomposition that fold() leaves intact — transliterate
# to ASCII so citation keys stay ASCII-only.
_TRANSLIT = str.maketrans(
    {
        "ø": "o",
        "Ø": "o",
        "ł": "l",
        "Ł": "l",
        "đ": "d",
        "Đ": "d",
        "ı": "i",
        "ð": "d",
        "Ð": "d",
        "þ": "th",
        "Þ": "th",
        "ß": "ss",
        "æ": "ae",
        "Æ": "ae",
        "œ": "oe",
        "Œ": "oe",
    }
)

# Organisational author names (consortia, collaborations) must be brace-wrapped
# so BibTeX treats them atomically instead of splitting off a fake surname.
_ORG_RE = re.compile(
    r"\b(collaboration|consortium|group|team|project|network|initiative|survey)\b",
    re.IGNORECASE,
)


def _fold_translit(s: str) -> str:
    """ASCII-fold a string for citation-key generation.

    Transliterates non-decomposables, then NFKD-folds to strip diacritics.
    Case is preserved (callers lowercase).
    """
    return fold(s.translate(_TRANSLIT))


def _key_token(s: str) -> str:
    """Reduce a string to a BibTeX-key-safe token.

    ASCII-folded, lowercased and stripped to ``[a-z0-9]`` — apostrophes,
    hyphens, periods, spaces and any surviving non-ASCII all go.
    """
    return re.sub(r"[^a-z0-9]", "", _fold_translit(s).lower())


def _extract_last_name(display_name: str) -> str:
    """Extract a key-safe last name from an author display name.

    Handles particles like 'van Tilborg' -> 'vantilborg' and guarantees an
    ASCII ``[a-z0-9]`` result.
    """
    parts = display_name.strip().split()
    if len(parts) <= 1:
        return (_key_token(parts[0]) if parts else "") or "unknown"

    # Walk backwards from the end to collect last name + particles
    last_parts = [parts[-1]]
    for part in reversed(parts[:-1]):
        if part.lower() in _PARTICLES:
            last_parts.append(part)
        else:
            break
    last_parts.reverse()
    return _key_token("".join(last_parts)) or "unknown"


def _first_key_word(title: str) -> str:
    """First significant (non-stopword) title word, as a key-safe token."""
    for word in re.findall(r"[A-Za-z]+", _fold_translit(title)):
        if word.lower() not in _TITLE_SKIP:
            return _key_token(word)
    return "untitled"


def _generate_key(work: dict[str, Any]) -> str:
    """Generate a BibTeX citation key like 'vantilborg2022exposing'."""
    authorships = work.get("authorships", [])
    if authorships:
        first_author = authorships[0].get("author", {}).get("display_name", "unknown")
        last_name = _extract_last_name(first_author)
    else:
        last_name = "unknown"

    year = work.get("publication_year", "")
    first_word = _first_key_word(work.get("title", "") or "")
    return f"{last_name}{year}{first_word}"


def _format_one_name(display_name: str) -> str:
    """Format a single author display name as 'Last, First'.

    Organisational names (consortia, collaborations) are brace-wrapped so
    BibTeX treats them atomically rather than splitting off a fake surname.
    """
    # Escape first, then split. Author display names carry LaTeX specials in
    # practice ("AT&T Labs", "Sanofi-Aventis R&D", any OpenAlex org-authorship),
    # and an unescaped `&` makes the whole .bib fail to compile. Escaping is
    # safe to do before the split: it only rewrites punctuation, never the
    # word boundaries the particle and surname logic below depends on.
    name = _escape_bibtex(display_name.strip())
    if _ORG_RE.search(name):
        return f"{{{name}}}"
    parts = name.split()
    if len(parts) == 1:
        return parts[0]
    # Find where the last name starts (including particles)
    last_start = len(parts) - 1
    for i in range(len(parts) - 2, -1, -1):
        if parts[i].lower() in _PARTICLES:
            last_start = i
        else:
            break
    first = " ".join(parts[:last_start])
    last = " ".join(parts[last_start:])
    return f"{last}, {first}" if first else last


def _format_names(items: Iterable[Any], name_of: Callable[[Any], str]) -> str:
    """Join formatted author names with ' and ', skipping blanks.

    ``name_of`` pulls the display string out of each item (OpenAlex authorships
    nest it under ``author.display_name``; arXiv/bioRxiv use a flat ``name``).
    """
    names = [_format_one_name(n) for item in items if (n := name_of(item).strip())]
    return " and ".join(names)


def _format_authors_bibtex(authorships: list[dict[str, Any]]) -> str:
    """Format OpenAlex authorships: 'Last, First and Last, First'."""
    return _format_names(authorships, lambda a: a.get("author", {}).get("display_name", ""))


# LaTeX specials replaced verbatim (after backslash + brace handling) when a
# field value is treated as literal text.
_ESCAPE_SIMPLE = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_"}


def _escape_bibtex(s: str) -> str:
    """Neutralize LaTeX specials so ``s`` is safe as a literal field value.

    Treats the input as plain text — it does NOT preserve braces for
    case-protection. Order matters: literal braces are stripped first (so they
    can't unbalance the field), the backslash is escaped next (before we start
    emitting our own backslashes), then the remaining specials.
    """
    s = s.replace("{", "").replace("}", "")
    s = s.replace("\\", r"\textbackslash{}")
    for ch, repl in _ESCAPE_SIMPLE.items():
        s = s.replace(ch, repl)
    s = s.replace("~", r"\textasciitilde{}")
    return s.replace("^", r"\textasciicircum{}")


# Per-character escapes for DOI suffixes. A single pass avoids the ordering
# trap of chained str.replace: escaping "\\" first emits "\\textbackslash{}",
# whose braces a later brace-pass would then escape again.
_DOI_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "#": r"\#",
    "_": r"\_",
    "$": r"\$",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def _escape_doi(s: str) -> str:
    """Escape the BibTeX-fatal characters in a DOI, leaving it otherwise intact.

    Unlike ``_escape_bibtex`` this does not rewrite the string into prose — a
    DOI must stay resolvable — so it touches only what would break the entry.
    DOI suffixes are publisher-chosen and genuinely contain these characters;
    an unescaped ``%`` comments out the rest of the file and an unmatched
    ``{`` swallows it.
    """
    return "".join(_DOI_ESCAPES.get(ch, ch) for ch in s)


def generate_bibtex(work: dict[str, Any]) -> str:
    """Generate a BibTeX entry from an OpenAlex work object."""
    work_type = work.get("type", "other") or "other"
    entry_type = _TYPE_MAP.get(work_type, "misc")

    key = _generate_key(work)
    authorships = work.get("authorships", [])
    title = work.get("title", "") or ""
    year = work.get("publication_year", "")
    doi = work.get("doi", "")
    if doi and doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/") :]

    biblio = work.get("biblio", {}) or {}
    primary_location = work.get("primary_location", {}) or {}
    source = primary_location.get("source", {}) or {}
    venue_name = source.get("display_name", "")
    publisher = source.get("host_organization_name", "")

    # Build fields list (order matters for readability)
    fields: list[tuple[str, str]] = []
    fields.append(("title", f"{{{_escape_bibtex(title)}}}"))
    if authorships:
        fields.append(("author", f"{{{_format_authors_bibtex(authorships)}}}"))

    # Type-specific venue field
    if entry_type == "article" and venue_name:
        fields.append(("journal", f"{{{_escape_bibtex(venue_name)}}}"))
    elif entry_type in ("inproceedings", "incollection") and venue_name:
        fields.append(("booktitle", f"{{{_escape_bibtex(venue_name)}}}"))
    elif entry_type == "phdthesis":
        # For dissertations, venue is typically the university
        institutions = []
        for a in authorships:
            for inst in a.get("institutions", []):
                name = inst.get("display_name", "")
                if name and name not in institutions:
                    institutions.append(name)
        if institutions:
            fields.append(("school", f"{{{_escape_bibtex(institutions[0])}}}"))
    elif entry_type == "techreport" and venue_name:
        fields.append(("institution", f"{{{_escape_bibtex(venue_name)}}}"))

    if biblio.get("volume"):
        fields.append(("volume", f"{{{biblio['volume']}}}"))
    if biblio.get("issue"):
        fields.append(("number", f"{{{biblio['issue']}}}"))
    if biblio.get("first_page"):
        pages = biblio["first_page"]
        if biblio.get("last_page"):
            pages += f"--{biblio['last_page']}"
        fields.append(("pages", f"{{{pages}}}"))
    if year:
        fields.append(("year", f"{{{year}}}"))
    if publisher:
        fields.append(("publisher", f"{{{_escape_bibtex(publisher)}}}"))
    if doi:
        fields.append(("doi", f"{{{_escape_doi(doi)}}}"))

    # Preprint-specific fields
    if entry_type == "misc" and work_type in ("preprint", "posted-content"):
        ids = work.get("ids", {}) or {}
        # Check for arXiv
        if doi and "arxiv" in doi.lower():
            # Extract the numeric arXiv ID: "10.48550/arXiv.1706.03762" -> "1706.03762"
            arxiv_id = doi.split("/")[-1]
            if arxiv_id.lower().startswith("arxiv."):
                arxiv_id = arxiv_id[len("arxiv.") :]
            fields.append(("eprint", f"{{{arxiv_id}}}"))
            fields.append(("archiveprefix", "{arXiv}"))
        elif "openalex" in (ids.get("openalex", "") or ""):
            fields.append(
                ("howpublished", f"{{\\url{{{_escape_doi(work.get('doi', '') or '')}}}}}")
            )

    # Format the entry
    field_str = ",\n".join(f"  {name}={value}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{field_str}\n}}"


# ---------------------------------------------------------------------------
# arXiv BibTeX generation
# ---------------------------------------------------------------------------


def _generate_arxiv_key(paper: dict[str, Any]) -> str:
    """Generate a BibTeX citation key from an arXiv paper dict."""
    authors = paper.get("authors", [])
    if authors:
        last_name = _extract_last_name(authors[0].get("name", "unknown"))
    else:
        last_name = "unknown"

    published = paper.get("published", "") or ""
    year = published[:4] if len(published) >= 4 else ""
    first_word = _first_key_word(paper.get("title", "") or "")

    return f"{last_name}{year}{first_word}"


def _format_arxiv_authors_bibtex(authors: list[dict[str, Any]]) -> str:
    """Format arXiv/bioRxiv author names: 'Last, First and Last, First'.

    These authors are stored as {"name": "First Last", ...}.
    """
    return _format_names(authors, lambda a: a.get("name", ""))


def generate_arxiv_bibtex(paper: dict[str, Any]) -> str:
    """Generate a BibTeX entry from a parsed arXiv paper dict."""
    key = _generate_arxiv_key(paper)
    authors = paper.get("authors", [])
    title = paper.get("title", "") or ""
    published = paper.get("published", "") or ""
    year = published[:4] if len(published) >= 4 else ""

    journal_ref = paper.get("journal_ref")
    doi = paper.get("doi")

    # Published in a journal -> @article, otherwise preprint -> @misc
    entry_type = "article" if journal_ref else "misc"

    # Extract bare arXiv ID (without version) from the id URL
    raw_id = paper.get("id", "")
    if "/abs/" in raw_id:
        eprint_id = raw_id.split("/abs/")[-1]
    else:
        eprint_id = raw_id
    eprint_id = re.sub(r"v\d+$", "", eprint_id)

    fields: list[tuple[str, str]] = []
    fields.append(("title", f"{{{_escape_bibtex(title)}}}"))
    if authors:
        fields.append(("author", f"{{{_format_arxiv_authors_bibtex(authors)}}}"))
    if entry_type == "article" and journal_ref:
        fields.append(("journal", f"{{{_escape_bibtex(journal_ref)}}}"))
    if year:
        fields.append(("year", f"{{{year}}}"))
    if eprint_id:
        fields.append(("eprint", f"{{{eprint_id}}}"))
        fields.append(("archiveprefix", "{arXiv}"))
    primary_cat = paper.get("primary_category", "")
    if primary_cat:
        fields.append(("primaryclass", f"{{{primary_cat}}}"))
    if doi:
        fields.append(("doi", f"{{{_escape_doi(doi)}}}"))

    field_str = ",\n".join(f"  {name}={value}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{field_str}\n}}"


# ---------------------------------------------------------------------------
# bioRxiv BibTeX generation
# ---------------------------------------------------------------------------


def _generate_biorxiv_key(paper: dict[str, Any]) -> str:
    """Generate a BibTeX citation key from a parsed bioRxiv paper dict."""
    authors = paper.get("authors", [])
    if authors:
        last_name = _extract_last_name(authors[0].get("name", "unknown"))
    else:
        last_name = "unknown"

    date = paper.get("date", "") or ""
    year = date[:4] if len(date) >= 4 else ""
    first_word = _first_key_word(paper.get("title", "") or "")
    return f"{last_name}{year}{first_word}"


def generate_biorxiv_bibtex(paper: dict[str, Any]) -> str:
    """Generate a BibTeX entry from a parsed bioRxiv/medRxiv paper dict.

    Uses @article if the paper has a published_doi (journal publication),
    otherwise @misc with DOI and howpublished pointing to the preprint server.
    """
    key = _generate_biorxiv_key(paper)
    authors = paper.get("authors", [])
    title = paper.get("title", "") or ""
    date = paper.get("date", "") or ""
    year = date[:4] if len(date) >= 4 else ""
    doi = paper.get("doi", "")
    published_doi = paper.get("published_doi")
    server = paper.get("server", "biorxiv")

    entry_type = "article" if published_doi else "misc"

    fields: list[tuple[str, str]] = []
    fields.append(("title", f"{{{_escape_bibtex(title)}}}"))
    if authors:
        fields.append(("author", f"{{{_format_arxiv_authors_bibtex(authors)}}}"))
    if entry_type == "article" and published_doi:
        fields.append(("doi", f"{{{_escape_doi(published_doi)}}}"))
    if year:
        fields.append(("year", f"{{{year}}}"))
    if entry_type == "misc":
        server_name = "medRxiv" if server == "medrxiv" else "bioRxiv"
        fields.append(("publisher", f"{{{server_name}}}"))
        if doi:
            fields.append(("doi", f"{{{_escape_doi(doi)}}}"))
            fields.append(("howpublished", f"{{\\url{{https://doi.org/{_escape_doi(doi)}}}}}"))

    field_str = ",\n".join(f"  {name}={value}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{field_str}\n}}"
