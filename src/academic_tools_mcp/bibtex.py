"""BibTeX rendering: citation keys, name formatting and LaTeX escaping."""

import re
from collections.abc import Callable, Iterable
from typing import Any

from .providers import arxiv
from .util import doinorm
from .util.textnorm import fold

# OpenAlex's `type` vocabulary (not Crossref's); anything unlisted falls to
# @misc. Re-derive: `works?group_by=type`.
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

# Particles publishers *capitalize*; the case rule covers the rest. Don't grow
# it — a capitalized "Du"/"Bin" is a given name.
_PARTICLES = {"van", "von", "de", "del", "della", "di", "la", "le", "den", "der", "el", "al"}

# First-significant-word stopwords: English closed class, plus the articles,
# prepositions and conjunctions of the other major publication languages.
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

# Strip a leading one-letter elision, the Romance article case: "L'exil" -> "exil".
_ELISION_RE = re.compile(r"^[A-Za-z]['\u2019]")

# No NFKD decomposition, so fold() leaves them — transliterate to keep keys ASCII.
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

# Consortia and collaborations are brace-wrapped so BibTeX doesn't split off a surname.
_ORG_RE = re.compile(
    r"\b(collaboration|consortium|group|team|project|network|initiative|survey)\b",
    re.IGNORECASE,
)


def _fold_translit(s: str) -> str:
    """Transliterate what ``fold`` leaves intact, then NFKD-fold; the table lowercases."""
    return fold(s.translate(_TRANSLIT))


# Everything a citation key may not contain; runs on every key word component.
_NON_KEY_RE = re.compile(r"[^a-z0-9]")


def _key_token(s: str) -> str:
    """Fold, lowercase and strip to ``[a-z0-9]`` — the gate for a key's word components."""
    return _NON_KEY_RE.sub("", _fold_translit(s).lower())


def _surname_is_cased(parts: list[str]) -> bool:
    """Is the final name token capitalized? Gates the lowercase particle rule."""
    return parts[-1][:1].isupper()


def _is_particle(token: str, *, cased: bool) -> bool:
    """Is ``token`` part of the surname's particle run (BibTeX's "von" part)?

    The wordlist, plus BibTeX's lowercase-initial rule — gated on ``cased`` so an
    all-lowercase display name doesn't collapse into one run.
    """
    return token.lower() in _PARTICLES or (cased and token[:1].islower())


def _surname_start(parts: list[str]) -> int:
    """Index where the surname begins, particle run included.

    Single home, so the key and the ``author`` field can't disagree about one
    name. Callers handle ``len(parts) <= 1``, so ``parts[-1]`` is safe.
    """
    cased = _surname_is_cased(parts)
    start = len(parts) - 1
    for i in range(len(parts) - 2, -1, -1):
        if not _is_particle(parts[i], cased=cased):
            break
        start = i
    return start


def _extract_last_name(display_name: str) -> str:
    """Key-safe last name: 'van Tilborg' -> 'vantilborg', always ``[a-z0-9]``."""
    parts = display_name.strip().split()
    if len(parts) <= 1:
        return (_key_token(parts[0]) if parts else "") or "unknown"
    return _key_token("".join(parts[_surname_start(parts) :])) or "unknown"


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
    last_start = _surname_start(parts)
    first = " ".join(parts[:last_start])
    last = " ".join(parts[last_start:])
    return f"{last}, {first}" if first else last


def _format_names(items: Iterable[Any], name_of: Callable[[Any], str]) -> str:
    """Join names with ' and ', skipping blanks."""
    names = (_format_one_name(name_of(item) or "") for item in items)
    return " and ".join(name for name in names if name)


def _format_authors_bibtex(authorships: list[dict[str, Any]]) -> str:
    """Format OpenAlex authorships: 'Last, First and Last, First'."""
    return _format_names(authorships, _author_display_name)


def _format_flat_authors_bibtex(authors: list[dict[str, Any]]) -> str:
    """Format arXiv/bioRxiv authors, stored flat as ``{"name": "First Last"}``."""
    return _format_names(authors, lambda a: (a or {}).get("name") or "")


# One `str.translate` pass: chained replaces would eat the braces
# `\textbackslash{}` emits with the `{`/`}` deletions in this same table.
_BIBTEX_ESCAPES: dict[str, str | None] = {
    "{": None,
    "}": None,
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

# Same table, but a DOI keeps its braces — escaped, not dropped — to stay resolvable.
_DOI_ESCAPES = _BIBTEX_ESCAPES | {"{": r"\{", "}": r"\}"}
_DOI_TABLE = str.maketrans(_DOI_ESCAPES)

# url.sty takes verbatim catcodes, so a backslash escape would land in the
# link target — percent-encode instead.
_URL_TABLE = str.maketrans({ch: f"%{ord(ch):02X}" for ch in "%#\\{}^_&$~ "})


def _escape_bibtex(s: str) -> str:
    """Neutralize LaTeX specials for a literal field value.

    Braces dropped, not kept for case-protection; whitespace collapsed, since an
    Atom-wrapped title would break the one-field-per-line layout.
    """
    return " ".join(s.split()).translate(_BIBTEX_TABLE)


def _escape_doi(s: str) -> str:
    """Escape only what would break the entry — a DOI must stay resolvable."""
    return s.translate(_DOI_TABLE)


def _arxiv_eprint_from_doi(doi: str) -> str:
    """Bare, unversioned arXiv id out of an arXiv DOI; ``""`` if it isn't one.

    Through the provider's grammar, so the router and ``eprint`` agree on which
    papers are arXiv's. ``strip_version``, not ``base_arxiv_id``, so an
    old-style archive class keeps its case (``math.GT/0309136``).
    """
    return arxiv.strip_version(arxiv.normalize_arxiv_id(doi)) if arxiv.is_arxiv_id(doi) else ""


def _title_field(title: str) -> tuple[str, str]:
    """Title field, double-braced so no style can case-fold acronyms away."""
    return ("title", f"{{{{{_escape_bibtex(title)}}}}}")


def _url_field(url: str) -> tuple[str, str]:
    """``howpublished`` pointing at a resolvable URL."""
    return ("howpublished", f"{{\\url{{{url.translate(_URL_TABLE)}}}}}")


def _render_entry(entry_type: str, key: str, fields: list[tuple[str, str]]) -> str:
    """Assemble ``@type{key, ...}`` — the one formatting site. Values arrive brace-delimited."""
    body = ",\n".join(f"  {name}={value}" for name, value in fields)
    return f"@{entry_type}{{{key},\n{body}\n}}"


def generate_bibtex(work: dict[str, Any]) -> str:
    """Generate a BibTeX entry from an OpenAlex work object."""
    work_type = work.get("type") or ""
    entry_type = _TYPE_MAP.get(work_type, "misc")

    key = _generate_key(work)
    authorships = work.get("authorships") or []
    year = _key_year(work.get("publication_year"))
    # OpenAlex's DOI is a resolver URL, http or https — a local prefix strip
    # would emit `doi={http://doi.org/...}`.
    doi = doinorm.normalize(work.get("doi") or "")

    biblio = work.get("biblio") or {}
    source = (work.get("primary_location") or {}).get("source") or {}
    venue_name = source.get("display_name") or ""
    publisher = source.get("host_organization_name") or ""

    fields: list[tuple[str, str]] = [_title_field(work.get("title") or "")]
    if authors := _format_authors_bibtex(authorships):
        fields.append(("author", f"{{{authors}}}"))

    if entry_type == "article" and venue_name:
        fields.append(("journal", f"{{{_escape_bibtex(venue_name)}}}"))
    elif entry_type in ("inproceedings", "incollection") and venue_name:
        fields.append(("booktitle", f"{{{_escape_bibtex(venue_name)}}}"))
    elif entry_type == "phdthesis":
        # `or {}` at both levels: OpenAlex's tree carries a null authorship or institution.
        school = next(
            (
                name
                for a in authorships
                for inst in ((a or {}).get("institutions") or [])
                if (name := (inst or {}).get("display_name"))
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

    # Keys on the work type, not `@misc`: datasets and software land there too.
    # arXiv DOI -> eprint; else a resolver URL, then the OpenAlex landing page,
    # then no URL field at all — never a bare \url{}.
    if work_type == "preprint":
        if eprint := _arxiv_eprint_from_doi(doi):
            fields.append(("eprint", f"{{{_escape_doi(eprint)}}}"))
            fields.append(("archiveprefix", "{arXiv}"))
        elif doi:
            fields.append(_url_field(f"https://doi.org/{doi}"))
        elif landing_page := (work.get("ids") or {}).get("openalex"):
            fields.append(_url_field(landing_page))

    return _render_entry(entry_type, key, fields)


def generate_arxiv_bibtex(paper: dict[str, Any]) -> str:
    """Generate a BibTeX entry from a parsed arXiv paper dict."""
    key = _flat_key(paper, "published")
    year = _year_from_date(paper, "published")
    journal_ref = paper.get("journal_ref")
    doi = doinorm.normalize(paper.get("doi") or "")

    entry_type = "article" if journal_ref else "misc"
    # The id may be a URL or already bare; case survives for old-style ids.
    eprint_id = arxiv.strip_version(arxiv.id_from_entry(paper))

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


def generate_biorxiv_bibtex(paper: dict[str, Any]) -> str:
    """Generate a BibTeX entry from a parsed bioRxiv/medRxiv paper dict.

    ``@article`` on a ``published_doi``, with no ``journal`` — bioRxiv reports
    the DOI, never the venue. Otherwise ``@misc``, ``publisher`` naming the
    server and a resolver URL only if the preprint has a DOI.
    """
    key = _flat_key(paper, "date")
    year = _year_from_date(paper, "date")
    doi = doinorm.normalize(paper.get("doi") or "")
    published_doi = doinorm.normalize(paper.get("published_doi") or "")
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
