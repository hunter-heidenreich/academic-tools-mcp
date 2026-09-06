"""Property-based tests for BibTeX rendering.

Examples cover the specials someone thought of; hypothesis covers the ones
nobody did. Three invariants have to hold for *every* upstream string, because
a single violation makes the whole .bib fail to compile rather than one entry
render oddly:

* a citation key is ASCII ``[a-z0-9]``,
* an escaped field leaves no unescaped LaTeX special and no unbalanced brace,
* an escaped DOI is losslessly recoverable — it must stay resolvable.
"""

import re

from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp import bibtex

# Prose fields: anything an upstream record can carry. Surrogates are excluded
# because they can't survive a JSON round-trip out of a provider in the first
# place.
text = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=60)
years = st.one_of(st.none(), st.integers(min_value=1400, max_value=2100), text)
# A DOI suffix is near-freeform but carries no whitespace — the same exclusion
# `test_doi_properties` makes, for the same reason: it wouldn't be a DOI.
_doi_suffix = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp")), max_size=40
).filter(lambda s: not any(c.isspace() for c in s))
dois = st.one_of(st.none(), _doi_suffix.map(lambda s: f"10.1234/{s}"))
dates = st.one_of(st.none(), st.text(max_size=12), st.just("2024-01-02"))

_KEY_RE = re.compile(r"[a-z0-9]+")

# Everything `_escape_bibtex` and `_escape_doi` are allowed to emit: a control
# word with empty braces, or a backslash-escaped single character.
_ESCAPE_RE = re.compile(r"\\(?:[a-zA-Z]+\{\}|.)")

# Chars that are fatal in a BibTeX field unless escaped. `{`/`}` are handled
# separately — they're legal when balanced.
_SPECIALS = "&%$#_~^\\"


def _bare(s: str) -> str:
    """Drop every backslash escape, leaving only the literal text around them."""
    return _ESCAPE_RE.sub("", s)


# `\url{}` takes verbatim catcodes: percent-encoding, not backslash escapes.
_URL_ARG_RE = re.compile(r"\\url\{([^{}]*)\}")


def _assert_field_is_safe(rendered: str) -> None:
    """No unescaped special survives, and group braces balance at every prefix."""
    for arg in _URL_ARG_RE.findall(rendered):
        assert not set(arg) & set("\\{}#_&$~^ "), f"unencoded char in \\url: {arg!r}"
    bare = _bare(_URL_ARG_RE.sub("", rendered))
    assert not set(bare) & set(_SPECIALS), f"unescaped special in {rendered!r}"
    depth = 0
    for char in bare:
        depth += (char == "{") - (char == "}")
        assert depth >= 0, f"closes before it opens: {rendered!r}"
    assert depth == 0, f"unbalanced braces in {rendered!r}"


# ---------------------------------------------------------------------------
# Citation keys
# ---------------------------------------------------------------------------


@given(name=text, title=text, year=years)
def test_openalex_key_is_ascii_alnum(name: str, title: str, year: object) -> None:
    key = bibtex._generate_key(
        {
            "authorships": [{"author": {"display_name": name}}],
            "title": title,
            "publication_year": year,
        }
    )
    assert _KEY_RE.fullmatch(key), key


@given(name=text, title=text, date=dates)
def test_flat_key_is_ascii_alnum(name: str, title: str, date: str | None) -> None:
    paper = {"authors": [{"name": name}], "title": title, "published": date, "date": date}
    for field in ("published", "date"):
        key = bibtex._flat_key(paper, field)
        assert _KEY_RE.fullmatch(key), key


@given(text)
def test_key_token_is_idempotent(s: str) -> None:
    once = bibtex._key_token(s)
    assert bibtex._key_token(once) == once


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


@given(text)
def test_escaped_field_is_safe(s: str) -> None:
    _assert_field_is_safe(bibtex._escape_bibtex(s))


@given(text)
def test_escaped_doi_is_safe(s: str) -> None:
    _assert_field_is_safe(bibtex._escape_doi(s))


_INVERSE = {escaped: raw for raw, escaped in bibtex._DOI_ESCAPES.items()}
_DOI_TOKEN_RE = re.compile(
    "|".join(re.escape(tok) for tok in sorted(_INVERSE, key=len, reverse=True))
)


@given(text)
def test_escaped_doi_is_recoverable(s: str) -> None:
    """A DOI must stay resolvable: escaping rewrites nothing it can't undo."""
    escaped = bibtex._escape_doi(s)
    assert _DOI_TOKEN_RE.sub(lambda m: _INVERSE[m.group(0)], escaped) == s


# ---------------------------------------------------------------------------
# Whole entries
# ---------------------------------------------------------------------------


def _assert_entry_is_well_formed(entry: str) -> None:
    head, *lines = entry.split("\n")
    entry_type, _, key = head.partition("{")
    assert re.fullmatch(r"@[a-z]+", entry_type), head
    assert key.endswith(","), head
    assert _KEY_RE.fullmatch(key[:-1]), head
    assert lines[-1] == "}"
    for line in lines[:-1]:
        assert re.fullmatch(r"  [a-z]+=\{.*\},?", line), line
    _assert_field_is_safe(entry)


@given(
    name=text,
    title=text,
    year=years,
    doi=dois,
    venue=text,
    work_type=st.sampled_from(sorted(bibtex._TYPE_MAP)),
)
def test_openalex_entry_is_well_formed(
    name: str, title: str, year: object, doi: str | None, venue: str, work_type: str
) -> None:
    _assert_entry_is_well_formed(
        bibtex.generate_bibtex(
            {
                "type": work_type,
                "title": title,
                "publication_year": year,
                "doi": doi,
                "authorships": [
                    {"author": {"display_name": name}, "institutions": [{"display_name": venue}]}
                ],
                "biblio": {"volume": "1", "issue": "2", "first_page": "1", "last_page": "9"},
                "primary_location": {
                    "source": {"display_name": venue, "host_organization_name": venue}
                },
                "ids": {"openalex": "https://openalex.org/W1"},
            }
        )
    )


@given(name=text, title=text, date=dates, doi=dois, journal_ref=st.one_of(st.none(), text))
def test_arxiv_entry_is_well_formed(
    name: str, title: str, date: str | None, doi: str | None, journal_ref: str | None
) -> None:
    _assert_entry_is_well_formed(
        bibtex.generate_arxiv_bibtex(
            {
                "id": "http://arxiv.org/abs/1706.03762v7",
                "title": title,
                "published": date,
                "authors": [{"name": name}],
                "primary_category": "cs.CL",
                "journal_ref": journal_ref,
                "doi": doi,
            }
        )
    )


@given(name=text, title=text, date=dates, doi=dois, published_doi=dois)
def test_biorxiv_entry_is_well_formed(
    name: str, title: str, date: str | None, doi: str | None, published_doi: str | None
) -> None:
    _assert_entry_is_well_formed(
        bibtex.generate_biorxiv_bibtex(
            {
                "doi": doi,
                "published_doi": published_doi,
                "title": title,
                "authors": [{"name": name}],
                "date": date,
                "server": "biorxiv",
            }
        )
    )
