import re
import unicodedata
from typing import Any


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

# Common surname particles
_PARTICLES = {"van", "von", "de", "del", "della", "di", "la", "le", "den", "der", "el", "al"}


def _strip_accents_for_key(s: str) -> str:
    """Remove accents for BibTeX key generation only."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _extract_last_name(display_name: str) -> str:
    """Extract a usable last name from an author display name.

    Handles particles like 'van Tilborg' -> 'vantilborg'.
    """
    parts = display_name.strip().split()
    if len(parts) <= 1:
        return _strip_accents_for_key(parts[0]).lower() if parts else "unknown"

    # Walk backwards from the end to collect last name + particles
    last_parts = [parts[-1]]
    for part in reversed(parts[:-1]):
        if part.lower() in _PARTICLES:
            last_parts.append(part)
        else:
            break
    last_parts.reverse()
    return _strip_accents_for_key("".join(last_parts)).lower()


def _generate_key(work: dict[str, Any]) -> str:
    """Generate a BibTeX citation key like 'vantilborg2022exposing'."""
    authorships = work.get("authorships", [])
    if authorships:
        first_author = authorships[0].get("author", {}).get("display_name", "unknown")
        last_name = _extract_last_name(first_author)
    else:
        last_name = "unknown"

    year = work.get("publication_year", "")

    title = work.get("title", "") or ""
    # Get first meaningful word from title (skip articles/prepositions)
    skip = {"a", "an", "the", "on", "in", "of", "for", "to", "with", "and", "or"}
    title_words = re.findall(r"[a-zA-Z]+", title)
    first_word = "untitled"
    for w in title_words:
        if w.lower() not in skip:
            first_word = _strip_accents_for_key(w).lower()
            break

    return f"{last_name}{year}{first_word}"


def _format_authors_bibtex(authorships: list[dict[str, Any]]) -> str:
    """Format author names for BibTeX: 'Last, First and Last, First'."""
    names = []
    for authorship in authorships:
        display_name = authorship.get("author", {}).get("display_name", "")
        if not display_name:
            continue
        parts = display_name.strip().split()
        if len(parts) == 1:
            names.append(parts[0])
        else:
            # Find where the last name starts (including particles)
            last_start = len(parts) - 1
            for i in range(len(parts) - 2, -1, -1):
                if parts[i].lower() in _PARTICLES:
                    last_start = i
                else:
                    break
            first = " ".join(parts[:last_start])
            last = " ".join(parts[last_start:])
            if first:
                names.append(f"{last}, {first}")
            else:
                names.append(last)
    return " and ".join(names)


def _escape_bibtex(s: str) -> str:
    """Escape special BibTeX characters."""
    # Protect text that shouldn't be lowercased by BibTeX
    s = s.replace("&", r"\&")
    s = s.replace("%", r"\%")
    s = s.replace("_", r"\_")
    s = s.replace("#", r"\#")
    return s


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
        doi = doi[len("https://doi.org/"):]

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
    elif entry_type == "inproceedings" and venue_name:
        fields.append(("booktitle", f"{{{_escape_bibtex(venue_name)}}}"))
    elif entry_type == "incollection" and venue_name:
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
        fields.append(("doi", f"{{{doi}}}"))

    # Preprint-specific fields
    if entry_type == "misc" and work_type in ("preprint", "posted-content"):
        ids = work.get("ids", {}) or {}
        # Check for arXiv
        if doi and "arxiv" in doi.lower():
            # Extract the numeric arXiv ID: "10.48550/arXiv.1706.03762" -> "1706.03762"
            arxiv_id = doi.split("/")[-1]
            if arxiv_id.lower().startswith("arxiv."):
                arxiv_id = arxiv_id[len("arxiv."):]
            fields.append(("eprint", f"{{{arxiv_id}}}"))
            fields.append(("archiveprefix", "{arXiv}"))
        elif "openalex" in (ids.get("openalex", "") or ""):
            fields.append(("howpublished", f"{{\\url{{{work.get('doi', '')}}}}}"))

    # Format the entry
    field_str = ",\n".join(f"  {name}={value}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{field_str}\n}}"
