"""Property-based tests for artifact naming and the section index.

Five invariants stronger than any example set. Each is a contract the *agent*
depends on, and each spans two functions that an example suite can only sample:

* ``safe_stem`` is the sole reason two papers cannot share a file. Injectivity
  has to hold for every identifier an operator can type into ``import_paper``,
  not for the collisions someone thought to write down.
* ``safe_stem``'s output must be a fixed point of the startup migration. The
  writer and the migration gate are two spellings of one alphabet, and the
  character that broke their agreement (``~``) was in neither example set.
* ``migrate_legacy_stems`` runs unattended at startup over a cache the operator
  cannot easily repair. It must never merge two papers onto one file, and a
  second run must find nothing left to do.
* **The chaining contract.** ``find_in_paper`` promises that
  ``get_paper_section(id, hit["section_index"], offset=hit["char_offset"])``
  lands on the match. That is an offset computed in one function and sliced in
  another, through a fold-and-map round trip when ``normalize=True``; two
  examples pin two points of that space.
* Every index ``parse_sections`` hands an agent must be one
  ``get_section_content`` accepts, for any document a converter can emit —
  including the degenerate ones (no headings, empty bodies, headings only).

The identifier strategies are shared with the corpus-search properties, which
build them from the same routing.
"""

import string

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import _stems, cache, papers

from .test_cache_search_properties import identifiers

# Documents shaped like converter output: heading lines at every level mixed
# with prose and blank lines, so the degenerate cases (no headings, empty
# sections, headings only, trailing empty section) all occur naturally.
_heading_levels = st.sampled_from(["#", "##", "###", "####"])
_titles = st.text(alphabet=string.ascii_letters + string.digits + " -é", min_size=1, max_size=12)

_lines = st.one_of(
    st.builds(lambda h, t: f"{h} {t}", _heading_levels, _titles),
    st.text(alphabet=string.ascii_letters + " .éﬁ", min_size=0, max_size=30),
    st.just(""),
)
markdown_documents = st.lists(_lines, min_size=0, max_size=25).map("\n".join)


# ---------------------------------------------------------------------------
# P1 — one stem per paper, and never one stem for two
# ---------------------------------------------------------------------------


@given(st.text(min_size=0, max_size=40), st.text(min_size=0, max_size=40))
def test_safe_stem_is_injective(left: str, right: str) -> None:
    """Distinct identifiers never share a filename.

    The one exception is deliberate and documented: ``/`` maps to ``_``, so
    ``a/b`` and ``a_b`` collide. Everything else is percent-encoded precisely
    because collapsing would let two imported papers overwrite each other.
    """
    assume(left != right)
    assume(left.replace("/", "_") != right.replace("/", "_"))
    assert _stems.safe_stem(left) != _stems.safe_stem(right)


@given(st.text(min_size=0, max_size=40))
def test_safe_stem_output_is_never_seen_as_legacy(canonical: str) -> None:
    """The writer's alphabet and the migration gate's are the same alphabet.

    A character the writer emits but the gate rejects makes startup re-encode a
    correct name (``notes~draft%202024`` → ``notes~draft%25202024``) and orphan
    the file it just renamed. ``~`` is the character that broke this: ``quote``
    passes the RFC 3986 unreserved set, which ``_SAFE_STEM_KEEP`` does not list.
    """
    stem = _stems.safe_stem(canonical)
    assert _stems._MIGRATED_STEM_RE.match(stem)
    assert not _stems._needs_stem_migration(stem)


@given(st.text(min_size=0, max_size=40))
def test_a_stem_that_needs_no_migration_is_left_alone(canonical: str) -> None:
    """``safe_stem`` is not idempotent; the migration gate is what makes the
    sweep idempotent. Re-encoding a migrated stem would double its escapes."""
    stem = _stems.safe_stem(canonical)
    assume("%" in stem)
    assert _stems.safe_stem(stem) != stem, "the fixture must actually be re-encodable"


@given(identifiers)
def test_every_derived_path_agrees_on_the_stem(identifier: str) -> None:
    """PDF, markdown and sections key name the same paper.

    Three builders over one sanitizer: if they could disagree, a paper would
    convert under one name and be read under another.
    """
    stem = _stems.safe_stem(identifier)
    assert _stems.pdf_path("ns", identifier).name == stem + ".pdf"
    assert _stems.markdown_path("ns", identifier).name == stem + ".md"
    assert _stems.sections_key(identifier) == stem


# ---------------------------------------------------------------------------
# P2 — the startup sweep is safe to run unattended
# ---------------------------------------------------------------------------


# What a legacy writer could actually have put on disk: the pre-``safe_stem``
# rules only ever emitted characters the filesystem accepted, so the strategy is
# drawn from the shapes that really occur — DOIs with parentheses, freeform
# labels with spaces and punctuation — rather than arbitrary Unicode, which APFS
# rejects outright and which no cache can therefore contain.
_legacy_stem_chars = string.ascii_letters + string.digits + " ()[]{}:;,.-_+=&$#@!'~é"
legacy_stems = st.text(alphabet=_legacy_stem_chars, min_size=1, max_size=12)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(st.lists(legacy_stems, min_size=1, max_size=4, unique=True))
def test_the_sweep_is_idempotent_and_never_merges_two_papers(
    tmp_path_factory, monkeypatch, names
) -> None:
    """Two runs move what one run moved, and no paper is destroyed.

    Best-effort by design, so the assertion is conservative: the file count
    never drops, and a second pass finds nothing left.
    """
    root = tmp_path_factory.mktemp("cache")
    monkeypatch.setattr(cache, "CACHE_ROOT", root)
    d = root / "manual" / "markdown"
    d.mkdir(parents=True)

    written = set()
    for i, name in enumerate(names):
        # Names are arbitrary text, so build the on-disk name the way a legacy
        # writer would and skip any that collide before the sweep even runs.
        path = d / (name.replace("/", "_") + ".md")
        if path.name in written or path.name.startswith("."):
            continue
        path.write_text(str(i), encoding="utf-8")
        written.add(path.name)
    assume(written)

    before = len(list(d.iterdir()))
    papers.migrate_legacy_stems()
    assert len(list(d.iterdir())) == before, "the sweep lost or merged a paper"
    assert papers.migrate_legacy_stems() == 0, "a second run still had work to do"
    for path in d.iterdir():
        assert not _stems._needs_stem_migration(path.stem)


# ---------------------------------------------------------------------------
# P3 — every index an agent is given is one the reader accepts
# ---------------------------------------------------------------------------


@given(markdown_documents)
def test_every_parsed_index_is_readable(markdown: str) -> None:
    """``get_paper_sections`` → ``get_paper_section`` never dead-ends."""
    for entry in papers.parse_sections(markdown):
        content = papers.get_section_content(markdown, entry["index"])
        assert "error" not in content, (entry, content)
        assert content["title"] == entry["title"]
        assert content["approx_tokens"] == entry["approx_tokens"]


@given(markdown_documents)
def test_detection_and_the_index_are_one_scan(markdown: str) -> None:
    """``parse_sections_and_detect`` is the two separate calls, in one pass."""
    sections, detected = papers.parse_sections_and_detect(markdown)
    assert sections == papers.parse_sections(markdown)
    assert detected == papers.has_detected_sections(markdown)


@given(markdown_documents)
def test_detection_agrees_with_the_title_extractor(markdown: str) -> None:
    """One policy for "which levels are title-level", two readers of it.

    ``cache_search`` names a hit's paper with ``first_section_heading`` while
    the reader indexes it with the section scan; a heading one sees and the
    other doesn't is a hit that names a section the index has no entry for.
    """
    assert papers.has_detected_sections(markdown) == (
        papers.first_section_heading(markdown) is not None
    )


@given(markdown_documents, st.integers(min_value=0, max_value=5000))
def test_section_at_offset_is_total(markdown: str, offset: int) -> None:
    """Any non-negative offset into a document with sections resolves to an
    index the reader accepts — including offsets on a heading line, inside a
    section dropped as empty, and past the end of the document."""
    spans = papers.section_boundaries(markdown)
    assume(spans)
    found = papers.section_at_offset(markdown, offset)
    assert found is not None
    index, title = found
    assert 0 <= index < len(spans)
    assert title == spans[index].title
    assert "error" not in papers.get_section_content(markdown, index)


# ---------------------------------------------------------------------------
# P4 — the chaining contract find_in_paper promises agents
# ---------------------------------------------------------------------------


@given(
    markdown_documents,
    st.text(alphabet=string.ascii_letters + "é", min_size=1, max_size=4),
    st.booleans(),
)
def test_a_hit_offset_lands_on_the_match(markdown: str, query: str, normalize: bool) -> None:
    """``get_paper_section(id, section_index, offset=char_offset)`` starts at
    the match, for both the plain and the diacritic-folded scan.

    Under ``normalize=True`` the match is found in a folded copy and the offset
    is mapped back, so this is the round trip that can drift.
    """
    hits, _ = papers.find_in_markdown(markdown, query, normalize=normalize)
    for hit in hits:
        content = papers.get_section_content(
            markdown, hit["section_index"], offset=hit["char_offset"]
        )
        assert "error" not in content, (hit, content)
        assert content["content"].startswith(hit["match"]), hit
        assert content["title"] == hit["section"]


@given(markdown_documents, st.text(min_size=1, max_size=4), st.integers(1, 6))
def test_truncated_means_a_further_match_exists(markdown: str, query: str, cap: int) -> None:
    """``truncated`` is what lets ``find_in_paper`` say "more exist" instead of
    silently capping — so it must mean exactly that, never "we hit the cap"."""
    hits, truncated = papers.find_in_markdown(markdown, query, max_results=cap)
    assert len(hits) <= cap
    all_hits, _ = papers.find_in_markdown(markdown, query, max_results=10_000)
    assert truncated == (len(all_hits) > len(hits))
