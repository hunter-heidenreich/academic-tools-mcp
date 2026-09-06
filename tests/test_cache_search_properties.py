"""Property-based tests for the corpus search engine.

Three invariants here are stronger than any example set, and each has already
been violated by code that passed a hand-written suite:

* A hit's ``canonical_id`` must chain back into the paper tools. That is a
  round-trip through two modules — ``manual.resolve_target`` picks the
  namespace, ``papers.safe_stem`` writes the filename, ``_filename_to_canonical``
  reads it back — and an identifier shape missing from any one of the three
  produces a hit that goes nowhere. Examples covered the shapes someone thought
  of; a versioned old-style arXiv id and a dotted archive were not among them.
* A snippet's ``char_offset`` indexes the ORIGINAL markdown while the match was
  found in a lowercased, optionally folded copy. Neither transform preserves
  length, so the offset is only correct if every hit is mapped back. Two
  hand-built ``U+0130`` documents pin two points of that space.
* ``search`` is called with whatever an agent types. It must return a list, or
  a well-formed error — never a stray FTS5 syntax exception.
"""

import re

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import _textnorm, cache, cache_search, manual, papers

from .test_doi_properties import dois

# ---------------------------------------------------------------------------
# Identifier strategies, one per shape the router can assign a namespace to
# ---------------------------------------------------------------------------

# Real old-style archives, hyphenated and not, with and without a subject
# class. The subject class is the case the router rejected outright: its
# separating "." was missing from `_ARXIV_OLD_RE`, so `math.GT/0309136` landed
# in `manual` under a canonical key that was already arXiv's.
_OLD_ARCHIVES = ["hep-th", "hep-ph", "cs", "math", "cond-mat", "astro-ph", "nlin", "q-bio"]
_OLD_SUBJECTS = ["", ".GT", ".CO", ".stat-mech", ".soft"]

_versions = st.sampled_from(["", "v1", "v2", "v11"])

arxiv_new_ids = st.builds(
    lambda a, b, v: f"{a}.{b}{v}",
    st.integers(1001, 9912).map(str),
    st.integers(1, 99999).map(lambda n: f"{n:05d}"),
    _versions,
)

arxiv_old_ids = st.builds(
    lambda archive, subject, digits, version: f"{archive}{subject}/{digits}{version}",
    st.sampled_from(_OLD_ARCHIVES),
    st.sampled_from(_OLD_SUBJECTS),
    st.integers(0, 9999999).map(lambda n: f"{n:07d}"),
    _versions,
)

# The suffix must not carry a further "/": `safe_stem` maps every slash to "_"
# and only the one a known prefix introduced is decidable, so such a DOI
# round-trips imperfectly by design. That exclusion is pinned by example in
# `test_cache_search.py`, not waved away here.
single_slash_dois = dois.filter(lambda d: d.count("/") == 1)

# Freeform manual labels: what `import_paper` accepts when the identifier is
# not an identifier at all. No "/" for the same reason as above, and no leading
# "10.<digits>_" shape, which the manual DOI repair would read as a registrant.
freeform_labels = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=0x2FFF, blacklist_categories=("Cs",)),
    min_size=1,
    max_size=30,
).filter(lambda s: "/" not in s and s.strip() == s and not s.startswith("10."))

# The "Cite as" spelling arXiv prints, which is what an agent pastes.
prefixed_arxiv_ids = st.builds(
    lambda prefix, gap, ident: f"{prefix}:{gap}{ident}",
    st.sampled_from(["arXiv", "arxiv", "ARXIV"]),
    st.sampled_from(["", " "]),
    st.one_of(arxiv_new_ids, arxiv_old_ids),
)

identifiers = st.one_of(
    arxiv_new_ids, arxiv_old_ids, prefixed_arxiv_ids, single_slash_dois, freeform_labels
)


def _mixed_case(text: str, flags: list[bool]) -> str:
    """Re-case *text* per *flags* — upstream old-style ids vary in case."""
    padded = flags + [False] * len(text)
    return "".join(c.upper() if f else c for c, f in zip(text, padded, strict=False))


# ---------------------------------------------------------------------------
# P1 — the canonical_id a hit carries chains back into the paper tools
# ---------------------------------------------------------------------------


@given(identifiers)
def test_a_stored_paper_inverts_to_the_key_it_was_stored_under(identifier: str) -> None:
    """`_filename_to_canonical` undoes `safe_stem` for every routable shape.

    Composed the way production does it: the namespace comes from the router,
    not from the test, so a shape the router sends somewhere unexpected fails
    here rather than passing against a namespace it never reaches.
    """
    target = manual.resolve_target(identifier)
    stem = papers.safe_stem(target["canonical"])
    assert cache_search._filename_to_canonical(target["namespace"], stem) == target["canonical"]


@given(st.one_of(arxiv_new_ids, arxiv_old_ids, prefixed_arxiv_ids))
def test_a_spelling_of_an_arxiv_id_is_never_a_second_cache_entry(spelling: str) -> None:
    """Every spelling of one arXiv id collapses to one namespace and one key.

    The `arXiv:` prefix is what arXiv's own "Cite as" box prints; left
    unstripped it was not an arXiv shape, so the paper cached under `manual`
    alongside the copy a bare id had already fetched.
    """
    from academic_tools_mcp.providers import arxiv

    target = manual.resolve_target(spelling)
    assert target["namespace"] == "arxiv"
    assert target["canonical"] == arxiv._normalize_arxiv_id(spelling).lower()
    assert ":" not in target["canonical"]


@given(st.text(max_size=40))
def test_arxiv_normalization_is_idempotent_for_any_input(text: str) -> None:
    """Feeding `_normalize_arxiv_id` its own output changes nothing.

    Without the prefix loop, `arXiv:arXiv:2301.00001` survives one pass and
    keys separately from its own normalized form.
    """
    from academic_tools_mcp.providers import arxiv

    once = arxiv._normalize_arxiv_id(text)
    assert arxiv._normalize_arxiv_id(once) == once


@given(arxiv_old_ids, st.lists(st.booleans(), max_size=30))
def test_an_old_style_arxiv_id_routes_to_arxiv_in_any_case(
    identifier: str, case_flags: list[bool]
) -> None:
    """Case is not part of an old-style id's identity, so it cannot pick a namespace.

    Two spellings routing to two namespaces means two cache entries, two
    downloads and two conversions of one paper.
    """
    spelled = _mixed_case(identifier, case_flags)
    target = manual.resolve_target(spelled)
    assert target["namespace"] == "arxiv"
    assert target["canonical"] == identifier.lower()


@given(identifiers)
def test_the_round_trip_is_stable_under_a_second_pass(identifier: str) -> None:
    """Re-routing a recovered canonical id lands on the same namespace and key."""
    first = manual.resolve_target(identifier)
    second = manual.resolve_target(first["canonical"])
    assert (second["namespace"], second["canonical"]) == (first["namespace"], first["canonical"])


# ---------------------------------------------------------------------------
# P2 — a snippet offset points at the match, in the ORIGINAL text
# ---------------------------------------------------------------------------

# Characters whose lower()/NFKD expansion is not length-preserving, so an
# unmapped offset drifts: U+0130 lowercases to two chars, the ligatures
# decompose, the sigma is context-sensitive, and the combining marks vanish
# under folding.
_TRICKY = st.text(alphabet=st.sampled_from("İﬁﬂǄΣΟΔΥΣΣΕΥΣéñ́̈ \nx"), max_size=120)


@given(_TRICKY, _TRICKY, st.booleans())
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
def test_snippet_offset_lands_on_the_match_not_beside_it(
    prefix: str, suffix: str, normalize: bool
) -> None:
    """`char_offset` slices the ORIGINAL markdown back to the matched term.

    The transforms are not length-preserving, so this holds only if every hit
    is mapped through `_textnorm`'s index map — including on the default
    `normalize=False` path, where a raw `str.lower()` looks harmless.
    """
    term = "zqxwidget"
    markdown = f"{prefix} {term} {suffix}"
    snippet, offset = cache_search._extract_snippet(markdown, {term}, normalize=normalize)
    assert offset is not None
    recovered = markdown[offset : offset + len(term)]
    assert (_textnorm.fold(recovered) if normalize else recovered).lower() == term
    assert term in snippet


@given(_TRICKY)
def test_a_term_that_is_absent_reports_no_offset(prefix: str) -> None:
    """No match means no offset — the caller must not attribute a section to it."""
    snippet, offset = cache_search._extract_snippet(prefix, {"zqxwidget"})
    assert offset is None
    # The head of the document, shaped like any other snippet: trimmed and
    # whitespace-collapsed, so the key means one thing in both cases.
    assert snippet == re.sub(r"\s+", " ", prefix[:200].strip())


# ---------------------------------------------------------------------------
# P3 — search is total, and its response shape holds for any query
# ---------------------------------------------------------------------------


def _seed_once() -> None:
    """Write a small fixed corpus, once — rewriting would re-index per example."""
    for namespace, stem, body in (
        ("arxiv", "1706.03762", "# Attention Is All You Need\n\n## Methods\n\nThe transformer.\n"),
        ("arxiv", "hep-th_9901001v2", "# Old\n\n## Results\n\nString duality and branes.\n"),
        ("manual", "10.1038_s41586-021-03819-2", "# Folding\n\n## Intro\n\nProtein structure.\n"),
    ):
        path = cache._CACHE_ROOT / namespace / "markdown" / f"{stem}.md"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")


@given(st.text(max_size=60), st.integers(min_value=-3, max_value=80))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=150)
def test_search_returns_a_well_formed_list_for_any_query(query: str, top_k: int) -> None:
    """Whatever an agent types, `search` answers with hits that obey the contract.

    FTS5 operators (`NOT`, `*`, `-`, `:`) and stray quotes are literal terms
    because every word is quoted; a syntax error escaping to the tool layer
    would be an unhandled exception, not an empty result.
    """
    _seed_once()
    hits = cache_search.search(query, top_k=top_k)
    assert isinstance(hits, list)
    assert len(hits) <= max(0, min(top_k, cache_search._MAX_TOP_K))
    for hit in hits:
        # Invariant the docstring states and the rounding must not break.
        assert hit["score"] > 0
        assert 0 <= hit["char_offset"] < hit["char_count"]
        assert hit["namespace"] and hit["canonical_id"]
        if hit["section_index"] is not None:
            assert hit["section_index"] >= 0
    ordering = [(-h["score"], h["namespace"], h["canonical_id"]) for h in hits]
    assert ordering == sorted(ordering)


# ---------------------------------------------------------------------------
# P4 — the MATCH expression FTS5 receives
# ---------------------------------------------------------------------------


@given(st.text(max_size=80))
def test_every_surviving_word_is_a_word_of_the_query(query: str) -> None:
    """`_query_words` filters; it never invents or rewrites a term.

    Substring rather than `query.split()`, because the split is on the
    separators `unicode61` uses — whitespace *and* NUL — not Python's.
    `unicode61` strips neither stopwords nor single characters, so an
    unfiltered "the" would OR in a term matching essentially every document.
    """
    for word in cache_search._query_words(query):
        assert word in query
        assert len(word) > 1
        assert word.lower() not in cache_search._STOPWORDS


@given(st.text(max_size=80))
def test_the_match_expression_is_empty_or_executable(query: str) -> None:
    """Whatever `_fts_query` builds, FTS5 parses — or it is the empty string.

    Executed against a real FTS5 table rather than eyeballed: quoting is what
    keeps an operator in a user's query from being parsed as one, and only
    SQLite can settle whether it worked.
    """
    import sqlite3

    expression = cache_search._fts_query(query)
    if not expression:
        return
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(body, content='')")
        con.execute("INSERT INTO t(rowid, body) VALUES (1, 'alpha beta gamma')")
        con.execute("SELECT rowid FROM t WHERE t MATCH ?", (expression,)).fetchall()
    finally:
        con.close()


@given(st.text(max_size=80))
def test_the_match_expression_is_deduplicated(query: str) -> None:
    """A word repeated in the query contributes one term, not two."""
    expression = cache_search._fts_query(query)
    assume(expression)
    terms = expression.split(" OR ")
    assert len(terms) == len(set(terms))


@given(st.text(max_size=40))
def test_snippet_terms_cover_the_words_the_index_matched(query: str) -> None:
    """Every word handed to FTS5 has a snippet term, so a hit can be centred.

    `_content_tokens` alone drops a non-Latin word entirely and splits an accented
    one, which centres the snippet on the document head and reports no
    section — a hit the index found perfectly well, come back unnavigable.
    """
    terms = cache_search._snippet_terms(query, normalize=False)
    for word in cache_search._query_words(query):
        assert word.lower() in terms


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------


@given(st.text(max_size=80), st.booleans())
def test_tokens_are_lowercase_content_words(text: str, normalize: bool) -> None:
    """Every `_content_tokens` token is lowercase, longer than one char, and not a stopword."""
    for token in cache_search._content_tokens(text, normalize=normalize):
        assert token == token.lower()
        assert len(token) > 1
        assert token not in cache_search._STOPWORDS
        assert re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", token)


@given(st.text(max_size=80))
def test_normalizing_is_folding_then_tokenizing(text: str) -> None:
    """`_content_tokens(normalize=True)` is exactly `_content_tokens(fold(text))`.

    The query and the documents must agree on the folded vocabulary; a second
    normalization policy here would let them diverge.
    """
    assert cache_search._content_tokens(text, normalize=True) == cache_search._content_tokens(
        _textnorm.fold(text)
    )
