"""Property-based tests for identifier routing and the arXiv re-file sweep.

Three invariants stronger than any example set:

* Storage and metadata must agree about which identifiers are arXiv's. They are
  two dispatchers over one predicate, and a disagreement means a paper whose
  metadata comes from arXiv while its PDF caches somewhere else.
* ``migrate_misrouted_arxiv`` must land a file exactly where ``resolve_target``
  looks for it. Reusing the source filename satisfies that only for the
  spellings whose legacy ``manual`` key happened to equal the arXiv one — the
  ``arXiv:`` prefix, which the manual key kept and the arXiv key drops, is the
  counterexample an example suite missed.
* ``_pdf_filename`` output reaches a ``bash -c`` command line, so its safety
  claim has to hold for arbitrary text, not for the identifiers someone thought
  of.

The identifier strategies are shared with the corpus-search properties, which
build them from the same routing; importing them here is the same move that
file makes with ``from .test_doi_properties import dois``.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import _doi, cache, cache_search, manual, papers

from .test_cache_search_properties import (
    arxiv_new_ids,
    arxiv_old_ids,
    identifiers,
    prefixed_arxiv_ids,
)

arxiv_spellings = st.one_of(arxiv_new_ids, arxiv_old_ids, prefixed_arxiv_ids)


# ---------------------------------------------------------------------------
# P1 — one arXiv predicate, two dispatchers
# ---------------------------------------------------------------------------


@given(identifiers)
def test_storage_and_metadata_agree_about_arxiv(identifier: str) -> None:
    """``resolve_target`` and ``resolve_metadata_source`` share one shape test.

    Both call ``arxiv.is_arxiv_id``; a second copy of the shape in either one
    would let a paper's metadata and its PDF route to different providers.
    """
    routes_to_arxiv = manual.resolve_target(identifier)["namespace"] == "arxiv"
    assert routes_to_arxiv == (manual.resolve_metadata_source(identifier) == "arxiv")


@given(identifiers)
def test_a_non_arxiv_identifier_never_claims_the_arxiv_namespace(identifier: str) -> None:
    """Whatever routes to ``arxiv`` round-trips through the arXiv canonicaliser."""
    from academic_tools_mcp.providers import arxiv

    target = manual.resolve_target(identifier)
    if target["namespace"] != "arxiv":
        return
    assert target["canonical"] == arxiv.canonical_arxiv_id(identifier)


@given(arxiv_old_ids)
def test_the_router_and_the_stem_inversion_share_one_grammar(identifier: str) -> None:
    """An id the router accepts inverts from its stem, and vice versa.

    ``arxiv._OLD_ID_RE`` and ``cache_search._ARXIV_OLDSTYLE_STEM_RE`` are the
    same grammar over ``/`` and over ``safe_stem``'s ``_``; both are built from
    ``providers.arxiv``'s exported patterns. Forked, one accepts an archive
    class the other doesn't and a hit's ``canonical_id`` goes nowhere.
    """
    from academic_tools_mcp.providers import arxiv

    canonical = manual.resolve_target(identifier)["canonical"]

    assert arxiv._OLD_ID_RE.match(canonical)
    assert cache_search._ARXIV_OLDSTYLE_STEM_RE.match(papers.safe_stem(canonical))


# ---------------------------------------------------------------------------
# P2 — the sweep lands the file where the router looks
# ---------------------------------------------------------------------------


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=60)
@given(arxiv_spellings)
def test_a_swept_paper_lands_where_the_router_looks(tmp_path_factory, monkeypatch, spelling):
    """Every spelling's legacy ``manual`` file becomes readable under ``arxiv``.

    The legacy key is what the pre-fix router produced: ``_doi.canonical``,
    which strips ``doi:`` but not ``arXiv:``. A sweep that reuses the source
    filename leaves the prefixed spellings in the arXiv namespace under a stem
    that namespace never builds — moved, but still unreachable.
    """
    root = tmp_path_factory.mktemp("cache")
    monkeypatch.setattr(cache, "CACHE_ROOT", root)

    legacy_key = _doi.canonical(spelling)
    legacy = papers.markdown_path(manual.NAMESPACE, legacy_key)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("# Body", encoding="utf-8")

    manual.migrate_misrouted_arxiv()

    target = manual.resolve_target(spelling)
    assert target["namespace"] == "arxiv"
    assert papers.markdown_path(target["namespace"], target["canonical"]).exists()
    assert not legacy.exists()


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=60)
@given(arxiv_spellings)
def test_the_sweep_is_idempotent(tmp_path_factory, monkeypatch, spelling):
    """A second run has nothing left to move, whatever the spelling."""
    root = tmp_path_factory.mktemp("cache")
    monkeypatch.setattr(cache, "CACHE_ROOT", root)

    legacy = papers.markdown_path(manual.NAMESPACE, _doi.canonical(spelling))
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("# Body", encoding="utf-8")

    assert manual.migrate_misrouted_arxiv() == 1
    assert manual.migrate_misrouted_arxiv() == 0


# ---------------------------------------------------------------------------
# P3 — the filename reaches a shell
# ---------------------------------------------------------------------------


@given(st.text(min_size=1, max_size=40))
def test_a_pdf_filename_is_filesystem_and_shell_safe(canonical: str) -> None:
    """No character outside ``safe_stem``'s charset survives into the name.

    The PDF path is interpolated into the converter's ``bash -c`` command, so
    a shell metacharacter in an exotic identifier must never reach it. Matching
    ``_MIGRATED_STEM_RE`` is the same statement twice over: the startup sweep
    must also see the name as already-migrated, or it renames the file to a
    stem the path builder never produces. ``~`` is the character that broke
    that agreement.
    """
    name = manual._pdf_filename(canonical)

    assert name.endswith(".pdf")
    assert papers._MIGRATED_STEM_RE.match(name.removesuffix(".pdf"))
    assert "/" not in name
