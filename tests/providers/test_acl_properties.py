"""Property-based tests for the ACL Anthology identifier layer."""

from urllib.parse import urlsplit

from hypothesis import given
from hypothesis import strategies as st

from academic_tools_mcp import corpus, manual
from academic_tools_mcp.providers import acl
from academic_tools_mcp.store import stems

new_format_ids = st.builds(
    "{}.{}-{}.{}".format,
    st.integers(min_value=2020, max_value=2029).map(str),
    st.sampled_from(["acl", "emnlp", "naacl", "findings", "eacl", "lrec", "tacl"]),
    st.sampled_from(["long", "short", "main", "demo", "acl", "1"]),
    st.integers(min_value=0, max_value=999).map(str),
)

old_format_ids = st.builds(
    "{}{:02d}-{:04d}".format,
    st.sampled_from("ACDEFHIJKLMNOPQRSTUWXY"),
    st.integers(min_value=0, max_value=99),
    # Volumes start at 1, so no real ID has a zero volume digit (`W18-0501`'s is "05").
    st.integers(min_value=1000, max_value=9999),
)

anthology_ids = st.one_of(new_format_ids, old_format_ids)


def _spellings(aid: str) -> list[str]:
    return [
        aid,
        aid.lower(),
        aid.upper(),
        f"  {aid}  ",
        f"https://aclanthology.org/{aid}/",
        f"aclanthology.org/{aid}.pdf",
        f"https://www.aclweb.org/anthology/{aid}",
        f"{acl.ACL_DOI_PREFIX}{aid}",
        f"https://doi.org/{acl.ACL_DOI_PREFIX.upper()}{aid.lower()}",
        f"doi:{acl.ACL_LEGACY_DOI_PREFIX}{aid}",
    ]


@given(anthology_ids)
def test_every_spelling_yields_one_key(aid: str) -> None:
    """A paper reached by any spelling of its ID is one cached paper."""
    key = acl.canonical_key(aid)
    for spelling in _spellings(aid):
        assert acl.is_anthology_id(spelling), spelling
        assert acl.canonical_key(spelling) == key, spelling


@given(st.text(max_size=60))
def test_normalize_is_idempotent_and_claims_agree(identifier: str) -> None:
    """Normalizing is idempotent and keeps a claim."""
    once = acl.normalize_anthology_id(identifier)
    assert acl.normalize_anthology_id(once) == once
    if acl.is_anthology_id(identifier):
        assert acl.is_anthology_id(once)


@given(anthology_ids)
def test_parse_and_build_are_inverses(aid: str) -> None:
    key = acl.canonical_key(aid)
    assert acl._build_id(*acl.parse_id(key)) == key


@given(anthology_ids)
def test_pdf_url_is_one_path_segment(aid: str) -> None:
    parts = urlsplit(acl.pdf_url(acl.canonical_key(aid)))
    assert parts.netloc == "aclanthology.org"
    assert (parts.query, parts.fragment) == ("", "")
    assert parts.path.count("/") == 1


@given(anthology_ids)
def test_a_stored_stem_inverts_to_the_key_the_router_files_under(aid: str) -> None:
    """PDF, markdown and section index share one stem, and it round-trips."""
    target = manual.resolve_target(f"https://doi.org/{acl.ACL_DOI_PREFIX}{aid}")
    stem = stems.safe_stem(target["canonical"])

    assert target["namespace"] == acl.NAMESPACE
    assert target["pdf_path"].stem == stem
    assert stems.markdown_path(acl.NAMESPACE, target["canonical"]).stem == stem
    assert stems.sections_key(target["canonical"]) == stem

    recovered = corpus._filename_to_canonical(acl.NAMESPACE, stem)
    assert manual.resolve_target(recovered) == target
