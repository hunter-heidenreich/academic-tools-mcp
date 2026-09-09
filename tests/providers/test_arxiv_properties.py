"""Property-based tests for arXiv identifier normalization.

The invariant these pin is the one `providers/arxiv.py` states about itself:
every spelling of one arXiv paper must collapse to one cache key, because a
spelling the module rejects is not merely unfetchable — it is not an arXiv
*shape* either, so `manual.resolve_target` files the paper under `manual` with
a canonical key that is already arXiv's, and the same paper caches, downloads
and converts twice.

Examples cover the spellings someone thought of. The `10.48550/` DataCite DOI
and four URL forms (`www.`, `export.`, scheme-less, uppercase host) were not
among them, which is why they are generated here rather than listed.

The bare-id strategies are shared with the corpus-search properties, the same
move that file makes with
`from tests.util.test_doinorm_properties import dois`.
"""

from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp import manual
from academic_tools_mcp.providers import arxiv
from tests.test_corpus_properties import arxiv_new_ids, arxiv_old_ids

bare_arxiv_ids = st.one_of(arxiv_new_ids, arxiv_old_ids)

# `10.48550/arXiv.<tail>` where the tail is *not* a paper id. Normalization
# must leave these alone: the prefix names arXiv's DOI registrant, not a
# guarantee that whatever follows is an arXiv id.
_NON_ID_TAILS = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),
    min_size=1,
    max_size=12,
)


def _spellings(ident: str) -> list[str]:
    """Every input form ``normalize_arxiv_id`` documents, for one bare id."""
    return [
        ident,
        f"  {ident}  ",
        # arXiv's own "Cite as" prefix, in the casings it and agents emit.
        f"arXiv:{ident}",
        f"arxiv:{ident}",
        f"ARXIV:{ident}",
        f"arXiv: {ident}",
        # The prefix loop: a single pass would leave this keying separately.
        f"arXiv:arXiv:{ident}",
        # abs/pdf URLs: both schemes, both extra host labels, no scheme at all,
        # an uppercase host, a trailing slash, a query and a fragment.
        f"https://arxiv.org/abs/{ident}",
        f"http://arxiv.org/abs/{ident}",
        f"https://www.arxiv.org/abs/{ident}",
        f"http://export.arxiv.org/abs/{ident}",
        f"arxiv.org/abs/{ident}",
        f"HTTPS://ARXIV.ORG/abs/{ident}",
        f"https://arxiv.org/abs/{ident}/",
        f"https://arxiv.org/abs/{ident}?context=cs",
        f"https://arxiv.org/abs/{ident}#abstract",
        f"https://arxiv.org/pdf/{ident}",
        f"https://arxiv.org/pdf/{ident}.pdf",
        f"https://arxiv.org/pdf/{ident}.pdf?download=1",
        # arXiv's HTML rendering, the default landing page for new papers.
        f"https://arxiv.org/html/{ident}",
        f"https://arxiv.org/html/{ident}#S3",
        # The prefix must be stripped *before* the URL handling, or this nested
        # form (which occurs in pasted citations) keys separately.
        f"arXiv:https://www.arxiv.org/abs/{ident}",
        # arXiv's DataCite DOI, in each spelling `doinorm.normalize` accepts.
        f"{arxiv.ARXIV_DOI_PREFIX}{ident}",
        f"10.48550/arxiv.{ident}",
        f"https://doi.org/{arxiv.ARXIV_DOI_PREFIX}{ident}",
        f"doi:{arxiv.ARXIV_DOI_PREFIX}{ident}",
    ]


@given(bare_arxiv_ids)
def test_every_accepted_spelling_yields_one_key(ident: str) -> None:
    """All the input forms `normalize_arxiv_id` documents collapse to one key."""
    key = arxiv.canonical_arxiv_id(ident)
    for spelling in _spellings(ident):
        assert arxiv.canonical_arxiv_id(spelling) == key, spelling


@given(bare_arxiv_ids)
def test_every_accepted_spelling_routes_to_arxiv(ident: str) -> None:
    """The shape test and the cache key agree, so dispatch can't disagree with storage.

    `is_arxiv_id` is the predicate `manual`'s two dispatchers route on. A
    spelling it rejects lands in `manual` under a key that is already arXiv's.
    """
    for spelling in _spellings(ident):
        assert arxiv.is_arxiv_id(spelling), spelling
        target = manual.resolve_target(spelling)
        assert target["namespace"] == arxiv.NAMESPACE, spelling
        assert target["canonical"] == arxiv.canonical_arxiv_id(ident), spelling


@given(bare_arxiv_ids)
def test_canonical_matches_normalize_lowercased(ident: str) -> None:
    """`canonical_arxiv_id` is exactly `normalize_arxiv_id` + `lower` — no second policy."""
    for spelling in _spellings(ident):
        assert arxiv.canonical_arxiv_id(spelling) == arxiv.normalize_arxiv_id(spelling).lower()


@given(bare_arxiv_ids)
def test_canonical_is_idempotent_and_lowercase(ident: str) -> None:
    """A key fed back through `canonical_arxiv_id` is unchanged — cache keys are stable."""
    key = arxiv.canonical_arxiv_id(ident)
    assert key == key.lower()
    assert arxiv.canonical_arxiv_id(key) == key


@given(st.text(max_size=60))
def test_normalize_is_idempotent_for_any_input(text: str) -> None:
    """Feeding `normalize_arxiv_id` its own output changes nothing — IDs and non-IDs alike.

    Without this, a form that survives one pass but not two (a repeated
    `arXiv:` prefix, a nested `10.48550/arXiv.10.48550/arXiv.…`) keys
    separately from its own normalized output.
    """
    once = arxiv.normalize_arxiv_id(text)
    assert arxiv.normalize_arxiv_id(once) == once


@given(bare_arxiv_ids)
def test_every_version_of_a_paper_shares_one_base(ident: str) -> None:
    """`base_arxiv_id` is the "whatever is current" key a bare request uses.

    It must collapse the versions and nothing else — `canonical_arxiv_id`
    keeps them apart, which is what stops v1 being served for a v2 request.
    """
    base = arxiv.base_arxiv_id(ident)
    stem = arxiv.strip_version(ident)
    for version in ("", "v1", "v2", "v11"):
        assert arxiv.base_arxiv_id(f"{stem}{version}") == base
        assert arxiv.base_arxiv_id(f"https://arxiv.org/abs/{stem}{version}") == base
    assert arxiv.base_arxiv_id(base) == base


@given(bare_arxiv_ids)
def test_strip_version_is_idempotent_and_preserves_case(ident: str) -> None:
    """The case-preserving half of the rule: BibTeX's `eprint` keeps `math.GT`."""
    once = arxiv.strip_version(ident)
    assert arxiv.strip_version(once) == once
    assert once.lower() == arxiv.base_arxiv_id(ident)


@given(bare_arxiv_ids)
def test_an_entry_id_round_trips_through_id_from_entry(ident: str) -> None:
    """`id_from_entry` inverts the `id` URL arXiv puts in an Atom entry.

    This is the key `search_papers` warms on and the `arxiv_id` the tool layer
    echoes; if it disagreed with `canonical_arxiv_id`, a search hit's promised
    free cache hit would be a miss.
    """
    for host in ("arxiv.org", "export.arxiv.org"):
        entry = {"id": f"http://{host}/abs/{ident}"}
        assert arxiv.id_from_entry(entry) == ident
        assert arxiv.canonical_arxiv_id(arxiv.id_from_entry(entry)) == arxiv.canonical_arxiv_id(
            ident
        )


@given(_NON_ID_TAILS)
def test_an_arxiv_doi_with_a_non_id_tail_is_not_claimed(tail: str) -> None:
    """The `10.48550/` prefix alone doesn't make a string an arXiv id.

    Stripping it unconditionally would mangle an unrelated DataCite record into
    a bare label, and would cost `normalize_arxiv_id` its idempotence.
    """
    doi = f"{arxiv.ARXIV_DOI_PREFIX}{tail}"
    assert not arxiv.is_arxiv_id(doi)
    assert arxiv.normalize_arxiv_id(doi) == doi
    assert manual.resolve_target(doi)["namespace"] != arxiv.NAMESPACE
