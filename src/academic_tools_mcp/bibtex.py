"""BibTeX rendering: citation keys, name formatting and LaTeX escaping."""

import re
from collections.abc import Callable, Iterable
from typing import Any

from . import _doi
from ._textnorm import fold

# OpenAlex's own `type` vocabulary (not Crossref's) -> BibTeX entry type;
# anything unlisted falls to @misc. Re-derive: `works?group_by=type`.
_TYPE_MAP: dict[str, str] = {
    "article": "article",
    "review": "article",
    "letter": "article",
    "editorial": "article",
    "erratum": "article",
    "retraction": "article",
    "book-review": "article",
    "data-paper": "article",
    "software-paper": "article",
    "conference-paper": "inproceedings",
    "book": "book",
    "book-chapter": "incollection",
    "reference-entry": "incollection",
    # Master's theses land here too; @phdthesis is the closer of the two.
    "dissertation": "phdthesis",
    "report": "techreport",
    "preprint": "misc",
    # Deliberate @misc: an abstract is a supplement page, not a paper.
    "conference-abstract": "misc",
    "dataset": "misc",
    "software": "misc",
    "standard": "misc",
    "paratext": "misc",
    "libguides": "misc",
    "peer-review": "misc",
    "supplementary-materials": "misc",
    "other": "misc",
}

# Particles publishers *capitalize*; `_is_particle`'s case rule covers the
# lowercase ones. Don't grow this — a capitalized "Du"/"Bin" is a given name.
_PARTICLES = {"van", "von", "de", "del", "della", "di", "la", "le", "den", "der", "el", "al"}

# Stopwords for the first significant title word: English closed class, plus
# the articles and prepositions of the other major publication languages.
_TITLE_STOPWORDS = """
a an the and or but nor if than then so yet
of in on at by to for from with within without into onto over under about
above below between among across against after before during through
toward towards upon via per
is are was were be been being do does did has have had
can could may might must shall should will would
this that these those it its their our his her we you they i
how what why when where which who whom whose
no not all any some more most such each both
le la les un une du des et dans sur pour par au aux
el los las una unos unas y en con para por
il lo gli uno di del della nel nella su e
der die das ein eine einer den dem und im von mit auf bei aus uber zur zum
o os as um uma da do dos das na em sobre
het een van voor op
av og och til
"""
_TITLE_SKIP = frozenset(_TITLE_STOPWORDS.split())

# Keeps compounds whole: "Pre-exposure" -> "preexposure", not "pre".
_TITLE_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['\u2019-][A-Za-z0-9]+)*")

# A one-letter Romance elision is an article: "L'exil" -> "exil".
_ELISION_RE = re.compile(r"^[A-Za-z]['\u2019]")

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

# The id is what follows the prefix, never the last "/" segment: an old-style
# id keeps its archive path ("10.48550/arXiv.hep-th/9901001").
_ARXIV_DOI_RE = re.compile(r"10\.48550/arxiv\.(?P<id>.+)$", re.IGNORECASE)
_VERSION_SUFFIX_RE = re.compile(r"v\d+$")


def _fold_translit(s: str) -> str:
    """Transliterate non-decomposables, then NFKD-fold. Case is preserved."""
    return fold(s.translate(_TRANSLIT))


def _key_token(s: str) -> str:
    """Fold, lowercase and strip to ``[a-z0-9]`` — the only citation-key gate."""
    return re.sub(r"[^a-z0-9]", "", _fold_translit(s).lower())


def _surname_is_cased(parts: list[str]) -> bool:
    """Is the final name token capitalized? Gates the lowercase particle rule."""
    return parts[-1][:1].isupper()


def _is_particle(token: str, *, cased: bool) -> bool:
    """Is ``token`` part of the surname's particle run (BibTeX's "von" part)?

    The wordlist, plus BibTeX's own rule that a lowercase-initial word before
    the last one is the von part — gated on ``cased`` so an all-lowercase
    display name doesn't collapse into one run.
    """
    return token.lower() in _PARTICLES or (cased and token[:1].islower())


def _extract_last_name(display_name: str) -> str:
    """Extract a key-safe last name from an author display name.

    Handles particles like 'van Tilborg' -> 'vantilborg' and guarantees an
    ASCII ``[a-z0-9]`` result.
    """
    parts = display_name.strip().split()
    if len(parts) <= 1:
        return (_key_token(parts[0]) if parts else "") or "unknown"

    # Walk backwards from the end to collect last name + particles
    cased = _surname_is_cased(parts)
    last_parts = [parts[-1]]
    for part in reversed(parts[:-1]):
        if _is_particle(part, cased=cased):
            last_parts.append(part)
        else:
            break
    last_parts.reverse()
    return _key_token("".join(last_parts)) or "unknown"


def _first_key_word(title: str) -> str:
    """First significant (non-stopword) title word, as a key-safe token."""
    for word in _TITLE_WORD_RE.findall(_fold_translit(title)):
        token = _key_token(_ELISION_RE.sub("", word))
        # "100 Years of..." distinguishes nothing; "3D" does — hence the letter test.
        if token in _TITLE_SKIP or not any(c.isalpha() for c in token):
            continue
        return token
    return "untitled"


def _author_display_name(authorship: dict[str, Any]) -> str:
    """Display name out of an OpenAlex authorship, which nulls keys it lacks."""
    return ((authorship or {}).get("author") or {}).get("display_name") or ""


def _key_year(value: Any) -> str:
    """Year as a key component: ASCII digits or nothing, so keys stay ``[a-z0-9]``."""
    year = str(value or "")
    return year if year.isascii() and year.isdigit() else ""


def _year_from_date(paper: dict[str, Any], field: str) -> str:
    """Leading year of a flat provider's date field ('2017-06-12T...' -> '2017')."""
    return _key_year((paper.get(field) or "")[:4])


def _generate_key(work: dict[str, Any]) -> str:
    """Generate a BibTeX citation key like 'vantilborg2022exposing'."""
    authorships = work.get("authorships") or []
    last_name = (
        _extract_last_name(_author_display_name(authorships[0])) if authorships else "unknown"
    )
    year = _key_year(work.get("publication_year"))
    first_word = _first_key_word(work.get("title") or "")
    return f"{last_name}{year}{first_word}"


def _flat_key(paper: dict[str, Any], date_field: str) -> str:
    """Citation key for a provider with a flat author list (arXiv, bioRxiv)."""
    authors = paper.get("authors") or []
    last_name = _extract_last_name((authors[0] or {}).get("name") or "") if authors else "unknown"
    year = _year_from_date(paper, date_field)
    first_word = _first_key_word(paper.get("title") or "")
    return f"{last_name}{year}{first_word}"


def _format_one_name(display_name: str) -> str:
    """Format one display name as 'Last, First'; brace-wrap an organisation."""
    # Escape before the split: it rewrites punctuation, never word boundaries.
    name = _escape_bibtex(display_name.strip())
    if _ORG_RE.search(name):
        return f"{{{name}}}"
    parts = name.split()
    if len(parts) <= 1:
        # Empty when escaping consumed the whole name (a display name of "{").
        return name
    # Find where the last name starts (including particles)
    cased = _surname_is_cased(parts)
    last_start = len(parts) - 1
    for i in range(len(parts) - 2, -1, -1):
        if _is_particle(parts[i], cased=cased):
            last_start = i
        else:
            break
    first = " ".join(parts[:last_start])
    last = " ".join(parts[last_start:])
    return f"{last}, {first}" if first else last


def _format_names(items: Iterable[Any], name_of: Callable[[Any], str]) -> str:
    """Join names with ' and ', skipping blanks. ``name_of`` reads each item."""
    names = (_format_one_name(name_of(item) or "") for item in items)
    return " and ".join(name for name in names if name)


def _format_authors_bibtex(authorships: list[dict[str, Any]]) -> str:
    """Format OpenAlex authorships: 'Last, First and Last, First'."""
    return _format_names(authorships, _author_display_name)


def _format_flat_authors_bibtex(authors: list[dict[str, Any]]) -> str:
    """Format arXiv/bioRxiv authors, stored flat as ``{"name": "First Last"}``."""
    return _format_names(authors, lambda a: (a or {}).get("name") or "")


# `str.translate` is one pass, so the braces `\textbackslash{}` emits are never
# re-escaped — the trap that chained `str.replace` falls into.
_BIBTEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_BIBTEX_TABLE = str.maketrans(_BIBTEX_ESCAPES)

# Same set, but a DOI keeps its braces (escaped, not stripped): it has to stay
# resolvable rather than read as prose.
_DOI_ESCAPES = _BIBTEX_ESCAPES | {"{": r"\{", "}": r"\}"}
_DOI_TABLE = str.maketrans(_DOI_ESCAPES)

# Inside `\url{}` url.sty takes verbatim catcodes, so a backslash escape would
# land in the link target — percent-encode instead; the resolver decodes it.
_URL_TABLE = str.maketrans({ch: f"%{ord(ch):02X}" for ch in "%#\\{}^_&$~ "})


def _escape_bibtex(s: str) -> str:
    """Neutralize LaTeX specials so ``s`` is safe as a literal field value.

    Plain text: braces are stripped rather than kept for case-protection, and
    whitespace runs collapse (Atom feeds wrap a title across lines).
    """
    return " ".join(s.split()).replace("{", "").replace("}", "").translate(_BIBTEX_TABLE)


def _escape_doi(s: str) -> str:
    """Escape only what would break the entry — a DOI must stay resolvable."""
    return s.translate(_DOI_TABLE)


def _arxiv_eprint_from_doi(doi: str) -> str:
    """Bare, unversioned arXiv id out of an arXiv DOI; ``""`` if it isn't one."""
    match = _ARXIV_DOI_RE.search(doi)
    return _VERSION_SUFFIX_RE.sub("", match["id"]) if match else ""


def _title_field(title: str) -> tuple[str, str]:
    """Title field, double-braced so no style can case-fold acronyms away."""
    return ("title", f"{{{{{_escape_bibtex(title)}}}}}")


def _url_field(url: str) -> tuple[str, str]:
    """``howpublished`` pointing at a resolvable URL."""
    return ("howpublished", f"{{\\url{{{url.translate(_URL_TABLE)}}}}}")


def _render_entry(entry_type: str, key: str, fields: list[tuple[str, str]]) -> str:
    """Assemble ``@type{key, name=value, ...}`` — the one place entries are formatted."""
    body = ",\n".join(f"  {name}={value}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{body}\n}}"


def generate_bibtex(work: dict[str, Any]) -> str:
    """Generate a BibTeX entry from an OpenAlex work object."""
    work_type = work.get("type") or ""
    entry_type = _TYPE_MAP.get(work_type, "misc")

    key = _generate_key(work)
    authorships = work.get("authorships") or []
    year = _key_year(work.get("publication_year"))
    # OpenAlex returns the DOI as a resolver URL, and not always over https —
    # strip it through the shared normalizer rather than a local prefix test,
    # or an http:// record emits `doi={http://doi.org/...}`, which is not a DOI.
    doi = _doi.normalize(work.get("doi") or "")

    biblio = work.get("biblio") or {}
    source = (work.get("primary_location") or {}).get("source") or {}
    venue_name = source.get("display_name") or ""
    publisher = source.get("host_organization_name") or ""

    fields: list[tuple[str, str]] = [_title_field(work.get("title") or "")]
    if authors := _format_authors_bibtex(authorships):
        fields.append(("author", f"{{{authors}}}"))

    # Type-specific venue field
    if entry_type == "article" and venue_name:
        fields.append(("journal", f"{{{_escape_bibtex(venue_name)}}}"))
    elif entry_type in ("inproceedings", "incollection") and venue_name:
        fields.append(("booktitle", f"{{{_escape_bibtex(venue_name)}}}"))
    elif entry_type == "phdthesis":
        school = next(
            (
                name
                for a in authorships
                for inst in (a.get("institutions") or [])
                if (name := inst.get("display_name"))
            ),
            "",
        )
        if school:
            fields.append(("school", f"{{{_escape_bibtex(school)}}}"))
    elif entry_type == "techreport" and venue_name:
        fields.append(("institution", f"{{{_escape_bibtex(venue_name)}}}"))

    # biblio values arrive as freeform strings, occasionally as numbers.
    if volume := biblio.get("volume"):
        fields.append(("volume", f"{{{_escape_bibtex(str(volume))}}}"))
    if issue := biblio.get("issue"):
        fields.append(("number", f"{{{_escape_bibtex(str(issue))}}}"))
    if first_page := biblio.get("first_page"):
        pages = _escape_bibtex(str(first_page))
        if last_page := biblio.get("last_page"):
            pages += f"--{_escape_bibtex(str(last_page))}"
        fields.append(("pages", f"{{{pages}}}"))
    if year:
        fields.append(("year", f"{{{year}}}"))
    if publisher:
        fields.append(("publisher", f"{{{_escape_bibtex(publisher)}}}"))
    if doi:
        fields.append(("doi", f"{{{_escape_doi(doi)}}}"))

    # arXiv DOI -> eprint; any other preprint -> a resolvable URL.
    if work_type == "preprint":
        if eprint := _arxiv_eprint_from_doi(doi):
            fields.append(("eprint", f"{{{_escape_doi(eprint)}}}"))
            fields.append(("archiveprefix", "{arXiv}"))
        elif doi:
            fields.append(_url_field(f"https://doi.org/{doi}"))
        elif landing_page := (work.get("ids") or {}).get("openalex"):
            fields.append(_url_field(landing_page))

    return _render_entry(entry_type, key, fields)


# ---------------------------------------------------------------------------
# arXiv BibTeX generation
# ---------------------------------------------------------------------------


def generate_arxiv_bibtex(paper: dict[str, Any]) -> str:
    """Generate a BibTeX entry from a parsed arXiv paper dict."""
    key = _flat_key(paper, "published")
    year = _year_from_date(paper, "published")
    journal_ref = paper.get("journal_ref")
    doi = _doi.normalize(paper.get("doi") or "")

    entry_type = "article" if journal_ref else "misc"
    # The id may be a URL or already bare.
    eprint_id = _VERSION_SUFFIX_RE.sub("", (paper.get("id") or "").split("/abs/")[-1])

    fields: list[tuple[str, str]] = [_title_field(paper.get("title") or "")]
    if authors := _format_flat_authors_bibtex(paper.get("authors") or []):
        fields.append(("author", f"{{{authors}}}"))
    if journal_ref:
        fields.append(("journal", f"{{{_escape_bibtex(journal_ref)}}}"))
    if year:
        fields.append(("year", f"{{{year}}}"))
    if eprint_id:
        fields.append(("eprint", f"{{{_escape_doi(eprint_id)}}}"))
        fields.append(("archiveprefix", "{arXiv}"))
    if primary_cat := paper.get("primary_category"):
        fields.append(("primaryclass", f"{{{_escape_doi(primary_cat)}}}"))
    if doi:
        fields.append(("doi", f"{{{_escape_doi(doi)}}}"))

    return _render_entry(entry_type, key, fields)


# ---------------------------------------------------------------------------
# bioRxiv BibTeX generation
# ---------------------------------------------------------------------------


def generate_biorxiv_bibtex(paper: dict[str, Any]) -> str:
    """Generate a BibTeX entry from a parsed bioRxiv/medRxiv paper dict.

    Uses @article if the paper has a published_doi (journal publication),
    otherwise @misc with DOI and howpublished pointing to the preprint server.
    """
    key = _flat_key(paper, "date")
    year = _year_from_date(paper, "date")
    doi = _doi.normalize(paper.get("doi") or "")
    published_doi = _doi.normalize(paper.get("published_doi") or "")
    server = paper.get("server") or "biorxiv"

    entry_type = "article" if published_doi else "misc"

    fields: list[tuple[str, str]] = [_title_field(paper.get("title") or "")]
    if authors := _format_flat_authors_bibtex(paper.get("authors") or []):
        fields.append(("author", f"{{{authors}}}"))
    if published_doi:
        fields.append(("doi", f"{{{_escape_doi(published_doi)}}}"))
    if year:
        fields.append(("year", f"{{{year}}}"))
    if entry_type == "misc":
        server_name = "medRxiv" if server == "medrxiv" else "bioRxiv"
        fields.append(("publisher", f"{{{server_name}}}"))
        if doi:
            fields.append(("doi", f"{{{_escape_doi(doi)}}}"))
            fields.append(_url_field(f"https://doi.org/{doi}"))

    return _render_entry(entry_type, key, fields)
