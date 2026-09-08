"""Property-based tests for the reference & citation graph tools.

Four invariants that examples had been standing in for. Two of them are about
the same seam: `tools/graph.py` is the *consumer* of two providers whose
guarantees differ, and it slices whatever they hand back.

The response side is why this file exists. `opencitations` builds its payload
locally, so `count == len(entries)` and every record is a dict; `crossref`
returns the upstream `message` object verbatim, and `_message_of` checks only
that it *is* a dict. Nothing types `message["reference"]` or its rows, and a
wrong-shape body is positive-cached for the full TTL — so a row that reaches
`.get()` as a string is a traceback the agent cannot retry away.

The pagination side has no upstream at all: `_page` is pure arithmetic that
every graph response depends on and that no example asserted `has_more` for.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from academic_tools_mcp.tools import graph
from academic_tools_mcp.util import doinorm

from .test_doi_properties import dois

_SETTINGS = settings(max_examples=100)

# Arbitrary JSON, for the bodies no upstream contract promises not to send.
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3)
    ),
    max_leaves=8,
)

# The domain the MCP boundary actually admits: PAGE is `ge=1`, PAGE_SIZE is
# `ge=1, le=50`. Sampling outside it would test branches an agent cannot reach.
_pages = st.integers(min_value=1, max_value=20)
_page_sizes = st.integers(min_value=1, max_value=50)
_entries = st.lists(st.dictionaries(st.text(max_size=4), st.integers(), max_size=2), max_size=60)


# ---------------------------------------------------------------------------
# Pagination arithmetic
# ---------------------------------------------------------------------------


@_SETTINGS
@given(entries=_entries, page=_pages, page_size=_page_sizes)
def test_a_page_never_exceeds_its_size_and_agrees_with_has_more(
    entries: list[Any], page: int, page_size: int
) -> None:
    """`total`, the slice and `has_more` are three views of one list.

    They are computed in one place precisely so they cannot disagree; a
    `has_more` that outlives the entries strands an agent in a loop asking for
    a page that is always empty.
    """
    result = graph._page(
        entries, source="s", key="items", doi="10.1/x", page=page, page_size=page_size
    )

    assert result["total"] == len(entries)
    assert len(result["items"]) <= page_size
    assert result["has_more"] == (page * page_size < len(entries))
    assert result["page"] == page
    assert result["page_size"] == page_size


@_SETTINGS
@given(entries=_entries, page_size=_page_sizes)
def test_walking_every_page_reproduces_the_list_exactly(entries: list[Any], page_size: int) -> None:
    """The walk an agent is told to do terminates and loses nothing.

    Paging until `has_more` is false must yield each entry once, in order —
    the end-to-end property that `has_more` and the offsets exist to serve.
    """
    walked: list[Any] = []
    page = 1
    while True:
        result = graph._page(
            entries, source="s", key="items", doi="10.1/x", page=page, page_size=page_size
        )
        walked.extend(result["items"])
        if not result["has_more"]:
            break
        page += 1
        assert page <= len(entries) + 2, "the walk must terminate"

    assert walked == entries


@_SETTINGS
@given(entries=_entries, page=_pages, page_size=_page_sizes)
def test_a_page_past_the_end_is_empty_not_a_slice_from_the_end(
    entries: list[Any], page: int, page_size: int
) -> None:
    """An overshooting page returns nothing, never a wrapped-around window."""
    result = graph._page(
        entries, source="s", key="items", doi="10.1/x", page=page, page_size=page_size
    )
    if (page - 1) * page_size >= len(entries):
        assert result["items"] == []


# ---------------------------------------------------------------------------
# Provider payloads
# ---------------------------------------------------------------------------


# The recognized field names, plus the `key` fallback and some noise. Sampling
# these rather than free text is what gives the reference strategy teeth: a
# dictionary over arbitrary keys almost never lands on "DOI".
_ref_keys = st.sampled_from(
    [
        "DOI",
        "author",
        "article-title",
        "year",
        "journal-title",
        "volume",
        "first-page",
        "unstructured",
        "key",
        "doi-asserted-by",
        "edition",
    ]
)
_crossref_rows = st.dictionaries(_ref_keys, _json_values, max_size=5)

# A work that actually carries a `reference`, in every shape the ladder allows:
# the list of rows Crossref documents, a list with non-dict rows in it, and the
# non-list `message["reference"]` nothing upstream rules out.
_crossref_works = st.fixed_dictionaries(
    {
        "reference": st.one_of(
            st.lists(_crossref_rows, max_size=8),
            st.lists(_json_values, max_size=8),
            _json_values,
        )
    }
)


@_SETTINGS
@given(work=_crossref_works, page_size=_page_sizes)
def test_any_crossref_work_pages_without_raising(work: dict[str, Any], page_size: int) -> None:
    """`crossref.get_work` guarantees only that the work is a dict.

    Anything escaping here reaches the agent as a traceback rather than a
    page — and, because the body was positive-cached, it does so for the whole
    TTL. Every emitted entry must be a dict, and a row carrying Crossref's own
    `key` must never render as a bare `{}`, which reads as a lost entry.
    """
    result = graph._crossref_refs_page(work, "10.1/x", 1, page_size)

    assert isinstance(result, dict)
    assert result["_source"] == "crossref"
    assert result["total"] == len(graph._crossref_refs(work))

    rows = graph._crossref_refs(work)[:page_size]
    for row, entry in zip(rows, result["references"], strict=True):
        assert isinstance(entry, dict)
        if row.get("key"):
            assert entry, "a row with a key must never render empty"


@_SETTINGS
@given(
    data=st.dictionaries(st.text(max_size=6), _json_values, max_size=4),
    kind=st.sampled_from(["references", "citations"]),
    page_size=_page_sizes,
)
def test_any_opencitations_payload_pages_without_raising(
    data: dict[str, Any], kind: str, page_size: int
) -> None:
    """The same guarantee in the other direction, for both of them."""
    result = graph._opencitations_page(data, "10.1/x", 1, page_size, kind=kind)

    assert isinstance(result, dict)
    assert result["_source"] == "opencitations"
    assert len(result[kind]) <= page_size


@_SETTINGS
@given(refs=st.lists(_json_values, max_size=8))
def test_the_count_tool_and_the_page_tool_never_disagree(refs: list[Any]) -> None:
    """Both read the reference list through `_crossref_refs`, so the survey
    can never send an agent to page a source that then raises on it."""
    work = {"reference": refs}
    counted = len(graph._crossref_refs(work))
    assert counted == graph._crossref_refs_page(work, "10.1/x", 1, 50)["total"]


# ---------------------------------------------------------------------------
# Auto source selection
# ---------------------------------------------------------------------------


# The counts selection actually sees. `-1` is the sentinel an errored source
# carries; both being `-1` never reaches selection, because the both-failed
# branch returns first — so the domain is "at most one source errored".
_counts = st.integers(min_value=-1, max_value=500)


@_SETTINGS
@given(cr=_counts, oc=_counts)
def test_crossref_holds_every_tie_and_near_tie(cr: int, oc: int) -> None:
    """The hysteresis is the policy, not a rounding detail.

    OpenCitations must win only on a *material* margin — a plain `oc > cr`
    would flip auto to the metadata-poor source for a row or two.
    """
    if cr == -1 and oc == -1:
        return  # handled by the both-failed branch, never by selection

    picks_opencitations = oc > cr * graph._CROSSREF_HYSTERESIS

    if 0 <= oc <= cr:
        assert not picks_opencitations, "a tie or a deficit can never take it"
    if cr >= 0 and oc >= 0 and oc <= cr * graph._CROSSREF_HYSTERESIS:
        assert not picks_opencitations, "the margin must be material"
    if cr == -1:
        assert picks_opencitations, "the surviving source must win"
    if oc == -1:
        assert not picks_opencitations, "the surviving source must win"


# ---------------------------------------------------------------------------
# The echoed DOI
# ---------------------------------------------------------------------------


@_SETTINGS
@given(doi=dois)
def test_every_spelling_of_one_doi_echoes_one_value(doi: str) -> None:
    """The echo is what an agent correlates results by across tool calls.

    Echoing the raw input gives one paper as many identities as it has
    spellings, all of which share a single cache key.
    """
    # No `.upper()` spelling: `str.upper()` on an arbitrary suffix is not a
    # respelling of the same DOI. U+00B5 MICRO SIGN uppercases to GREEK CAPITAL
    # MU, which lowercases to a *different* codepoint — that asymmetry is
    # Python's Unicode tables, not something the DOI layer can or should undo.
    canonical = doinorm.canonical(doi)
    for spelling in (
        doi,
        f"doi:{doi}",
        f"DOI: {doi}",
        f"https://doi.org/{doi}",
        f"http://dx.doi.org/{doi}",
        f"  {doi}  ",
    ):
        assert doinorm.canonical(spelling) == canonical
        assert graph._reject_non_doi(spelling) is None
