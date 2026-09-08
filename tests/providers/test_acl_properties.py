"""Property-based tests for the ACL Anthology identifier layer.

Three invariants are stronger than any example can state, and each one has a
concrete failure the module has to be safe from:

* one paper, one cache key — the reason `doinorm` is single-homed, restated for the
  provider that layers a prefix on top of it;
* the PDF, the markdown and the section index all key on that same string, so
  `corpus` can invert an ACL filename back to the identifier the router
  accepts;
* `pdf_url` addresses exactly the resource it names, whatever the DOI suffix
  turns out to contain.
"""

from urllib.parse import quote, urlsplit

from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp import corpus, manual
from academic_tools_mcp.providers import acl
from academic_tools_mcp.store import stems
from academic_tools_mcp.util import doinorm

# An Anthology ID is the DOI suffix, verbatim — `_strip_acl_prefix` hands back
# whatever followed the prefix. So the alphabet is `dois`'s suffix alphabet
# (`?`/`#` excluded for the reason `test_doi_properties` gives: the bare and URL
# spellings deliberately diverge on them), not the shapes ACL actually mints.
_SUFFIX_ALPHABET = st.characters(
    min_codepoint=33,
    max_codepoint=0x2FFF,
    blacklist_characters="?#",
    blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"),
).filter(lambda c: not c.isspace())

anthology_suffixes = st.text(alphabet=_SUFFIX_ALPHABET, min_size=1, max_size=40)
acl_dois = anthology_suffixes.map(lambda s: acl.ACL_DOI_PREFIX + s)

# `safe_stem` maps "/" to "_" and that step is lossy by design; `_restore_slashes`
# recovers only the slashes a known prefix introduced. So the stem round trip is
# stated over suffixes carrying none — which is every Anthology ID there is.
slashless_acl_dois = acl_dois.filter(lambda d: "/" not in d[len(acl.ACL_DOI_PREFIX) :])

# The two real spellings, for the properties that care about the grammar rather
# than the plumbing.
real_anthology_ids = st.one_of(
    st.builds(
        "{}.{}-{}.{}".format,
        st.integers(min_value=2020, max_value=2029).map(str),
        st.sampled_from(["acl", "emnlp", "naacl", "findings-acl", "eacl"]),
        st.sampled_from(["long", "short", "main", "demo"]),
        st.integers(min_value=1, max_value=999).map(str),
    ),
    st.builds(
        "{}{:02d}-{}".format,
        st.sampled_from("PWDCEJKLNQRSTUX"),
        st.integers(min_value=0, max_value=99),
        st.integers(min_value=1, max_value=9999).map(str),
    ),
)


@given(acl_dois)
def test_every_spelling_yields_one_key_and_one_anthology_id(doi: str) -> None:
    """A paper reached by any spelling of its DOI is one cached paper."""
    key = acl.canonical_key(doi)
    aid = acl.doi_to_anthology_id(doi)
    for spelling in (
        f"  {doi}  ",
        f"doi:{doi}",
        f"DOI: {doi}",
        f"https://doi.org/{doi}",
        f"https://dx.doi.org/{doi}",
        acl.ACL_DOI_PREFIX.upper() + doi[len(acl.ACL_DOI_PREFIX) :],
    ):
        assert acl.canonical_key(spelling) == key, spelling
        assert acl.doi_to_anthology_id(spelling) == aid, spelling


@given(acl_dois)
def test_canonical_key_is_the_shared_one(doi: str) -> None:
    """No second normalization policy: ACL layers a prefix, not a key rule."""
    assert acl.canonical_key(doi) == doinorm.canonical(doi)


@given(st.text(max_size=60))
def test_the_shape_test_and_the_extractor_agree(identifier: str) -> None:
    """`manual` routes on `is_acl_doi`; `download_pdf` refuses on a `None` id.

    If the two could disagree, an identifier would route to this namespace and
    then be rejected by the only tool that serves it.
    """
    assert acl.is_acl_doi(identifier) == (acl.doi_to_anthology_id(identifier) is not None)


@given(real_anthology_ids)
def test_normalize_anthology_id_is_idempotent(aid: str) -> None:
    once = acl._normalize_anthology_id(aid)
    assert acl._normalize_anthology_id(once) == once


@given(acl_dois)
def test_pdf_url_is_one_path_segment(doi: str) -> None:
    """The URL addresses exactly the id it names — no query, no extra segment."""
    aid = acl.doi_to_anthology_id(doi)
    assert aid is not None
    parts = urlsplit(acl.pdf_url(aid))
    assert parts.netloc == "aclanthology.org"
    assert parts.query == ""
    assert parts.fragment == ""
    assert parts.path == "/" + quote(aid, safe="") + ".pdf"


@given(acl_dois)
def test_every_artifact_keys_on_the_canonical_doi(doi: str) -> None:
    """PDF, markdown and section index share one stem.

    The PDF used to key on the Anthology ID, which made this namespace the one
    place the three disagreed.
    """
    canonical = acl.canonical_key(doi)
    stem = stems.safe_stem(canonical)
    assert acl.pdf_path(doi).stem == stem
    assert stems.markdown_path(acl.NAMESPACE, canonical).stem == stem
    assert stems.sections_key(canonical) == stem


@given(slashless_acl_dois)
def test_a_stored_stem_inverts_to_an_identifier_the_router_accepts(doi: str) -> None:
    """`search_cached_papers` reports a `canonical_id` an agent can chain with.

    Inverting the stem has to land back on the same key *and* on a value
    `manual.resolve_target` still files under this namespace — otherwise a
    chained `get_paper_section` looks in `manual` and misses.
    """
    canonical = acl.canonical_key(doi)
    stem = acl.pdf_path(doi).stem
    recovered = corpus._filename_to_canonical(acl.NAMESPACE, stem)
    assert recovered == canonical
    target = manual.resolve_target(recovered)
    assert target["namespace"] == acl.NAMESPACE
    assert target["canonical"] == canonical
