"""Property-based tests for the search tools.

Six invariants that examples had been standing in for. Three seams, and
`tools/search.py` is the last consumer of two providers the reviews above it
already hardened.

The **provider** seam is why this file exists. `crossref.search_works` filters
`items` to dicts and stops there; the upstream `message` arrives verbatim below
`_message_of`, so nothing types `item["author"]` or its rows. A row that
reaches `.get()` as a bare string is a traceback the agent cannot retry away —
the same defect class `graph._crossref_refs` was fixed for. arXiv's entries are
built locally by `_parse_entry`, but its *text* is upstream, and `int()` on a
four-character slice that only `isdigit()` vouched for raises on the
superscripts `arxiv.search_papers` documents avoiding two functions away.

The **identity** seam: `find_in_paper` resolves an identifier to one markdown
file, so every spelling of one paper must come back as one `paper_identifier`.
The graph tools hold this for `doi` and the paper family for `_canonical_id`.

The **envelope** seam is family-wide but was only ever asserted pairwise.
`.claude/rules/server.md` requires every search tool to report `result_count`
and to owe the agent some "more exist" signal; nothing checked all five at once,
so a tool could ship with none.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import cache_search, manual, server
from academic_tools_mcp.providers import arxiv, crossref, wikipedia
from academic_tools_mcp.store import stems
from academic_tools_mcp.tools import search

from .test_cache_search_properties import identifiers

_SETTINGS = settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
# For the properties that drive several tool calls per example.
_FEW = settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])

# Arbitrary JSON, for the bodies no upstream contract promises not to send.
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3)
    ),
    max_leaves=8,
)

# The recognized author keys plus noise. Sampling these rather than free text is
# what gives the strategy teeth: a dictionary over arbitrary keys almost never
# lands on "family".
_author_keys = st.sampled_from(["given", "family", "name", "sequence", "ORCID", "affiliation"])
_author_rows = st.dictionaries(_author_keys, _json_values, max_size=4)

# A row is a dict or it is not — the whole point of the guard.
_author_lists = st.one_of(
    st.lists(_author_rows, max_size=5),
    st.lists(_json_values, max_size=5),
    _json_values,
)

_DATE_KEYS = ["issued", "published-print", "published-online", "published", "posted"]

_crossref_items = st.fixed_dictionaries(
    {},
    optional={
        "DOI": _json_values,
        "title": _json_values,
        "author": _author_lists,
        **dict.fromkeys(_DATE_KEYS, _json_values),
    },
)

# The domain the MCP boundary admits: max_results is `ge=1, le=MAX_SEARCH_ROWS`.
_crossref_rows = st.integers(min_value=1, max_value=crossref.MAX_SEARCH_ROWS)
_arxiv_max = st.integers(min_value=1, max_value=arxiv.MAX_SEARCH_RESULTS)


def _serve_crossref(monkeypatch: pytest.MonkeyPatch, items: list[Any], total: Any = 7) -> None:
    async def fake(bibliographic: str, year: int | None = None, rows: int = 5) -> dict[str, Any]:
        return {"items": [i for i in items if isinstance(i, dict)], "total_results": total}

    monkeypatch.setattr(crossref, "search_works", fake)


def _serve_arxiv(monkeypatch: pytest.MonkeyPatch, entries: list[Any], total: Any = 7) -> None:
    async def fake(query: str, max_results: int = 10) -> dict[str, Any]:
        return {"total_results": total, "entries": entries}

    monkeypatch.setattr(arxiv, "search_papers", fake)


def _serve_error(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    async def fake(query: str, max_results: int = 10) -> dict[str, Any]:
        return dict(payload)

    monkeypatch.setattr(arxiv, "search_papers", fake)


# ---------------------------------------------------------------------------
# The provider seam
# ---------------------------------------------------------------------------


@_SETTINGS
@given(items=st.lists(_crossref_items, max_size=5), rows=_crossref_rows)
def test_any_crossref_payload_triages_without_raising(
    monkeypatch: pytest.MonkeyPatch, items: list[Any], rows: int
) -> None:
    """Whatever hangs off a Crossref item, the agent gets a triage list.

    `search_works` types the items and nothing beneath them, and a wrong-shape
    body is positive-cached for the full TTL — so an author row that is a bare
    string used to be an AttributeError on every call until the entry expired.
    """
    _serve_crossref(monkeypatch, items)

    result = asyncio.run(server.search_crossref_by_title("anything", max_results=rows))

    assert result["result_count"] == len(result["results"])
    for hit in result["results"]:
        assert set(hit) == {"doi", "title", "first_author", "author_count", "year"}
        assert isinstance(hit["author_count"], int)
        assert hit["first_author"] is None or isinstance(hit["first_author"], str)
        assert hit["year"] is None or isinstance(hit["year"], int)


@_SETTINGS
@given(authors=_author_lists)
def test_author_count_counts_the_rows_first_author_was_chosen_from(authors: Any) -> None:
    """The count and the name must describe one list, not two.

    Counting the raw value instead would report a bare string's *characters*
    as authors, sending an agent to paginate a list that does not exist.
    """
    rows = search._dict_list(authors)
    name = search._crossref_first_author(rows)

    if isinstance(authors, list):
        assert rows == [a for a in authors if isinstance(a, dict)]
    else:
        assert rows == []
    # A name can only have come from a row that was counted.
    assert name is None or rows


@_SETTINGS
@given(published=st.text(max_size=12) | _json_values)
def test_any_published_value_yields_a_year_or_none_never_a_raise(published: Any) -> None:
    """`int()` must never see a slice only `isdigit()` vouched for.

    "²⁰²³".isdigit() is True and int("²⁰²³") raises ValueError, which no
    except clause in the tool catches. `arxiv.search_papers` documents the
    same trap for its own `totalResults` parse.
    """
    year = search._published_year({"published": published})

    assert year is None or (isinstance(year, int) and 0 <= year <= 9999)


@_SETTINGS
@given(entries=st.lists(_json_values, max_size=5), total=_json_values, max_results=_arxiv_max)
def test_any_arxiv_entry_list_triages_without_raising(
    monkeypatch: pytest.MonkeyPatch, entries: list[Any], total: Any, max_results: int
) -> None:
    """The slim hit is built for every entry or for none — never half of one."""
    _serve_arxiv(monkeypatch, entries, total=total)

    result = asyncio.run(server.search_arxiv("anything", max_results=max_results))

    assert result["result_count"] == len(result["results"])
    for hit in result["results"]:
        assert set(hit) == {
            "arxiv_id",
            "title",
            "first_author",
            "author_count",
            "published_year",
        }
        assert isinstance(hit["arxiv_id"], str)


@_SETTINGS
@given(payload=st.dictionaries(st.text(max_size=6), _json_values, max_size=3))
def test_every_search_error_carries_a_suggestion_that_matches_its_verdict(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    """Advice must not argue with the retry verdict it rides beside.

    arXiv answers a malformed query with `retryable: False`; telling the agent
    to wait for the outage to pass sends it back at a call that cannot succeed.
    One message per cause, as `_convert_suggestion` and `_UNINDEXABLE_REASONS`
    already hold.
    """
    _serve_error(monkeypatch, {**payload, "error": "upstream said no"})

    result = asyncio.run(server.search_arxiv("anything"))

    assert "suggestion" in result
    # An error response never leaks a partial result list.
    assert "results" not in result and "result_count" not in result
    if payload.get("retryable") is False:
        assert "Rewrite the query" in result["suggestion"]
    elif "suggestion" in payload:
        # _enrich_error fills a gap; it never argues with the provider.
        assert result["suggestion"] == payload["suggestion"]


# ---------------------------------------------------------------------------
# The identity seam
# ---------------------------------------------------------------------------


@_FEW
@given(identifier=identifiers)
def test_every_spelling_of_one_paper_echoes_one_identifier(
    isolated_cache: Any, identifier: str
) -> None:
    """One markdown file, one `paper_identifier`.

    An agent correlating hits across calls sees `arXiv:2301.00001v2` and
    `2301.00001v2` as two papers when they are one file — the contract
    `_canonical_id` holds for the paper family and `doi` for the graph tools.
    """
    target = manual.resolve_target(identifier)
    md_path = stems.markdown_path(target["namespace"], target["canonical"])
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("# T\n\n## Intro\n\nvariational dropout.\n", encoding="utf-8")

    result = asyncio.run(server.find_in_paper(identifier, "dropout"))

    assert result["paper_identifier"] == target["canonical"]
    assert result["result_count"] == len(result["results"])


# ---------------------------------------------------------------------------
# The envelope seam
# ---------------------------------------------------------------------------


@_FEW
@given(
    items=st.lists(_crossref_items, max_size=4),
    entries=st.lists(_json_values, max_size=4),
    titles=st.lists(st.text(max_size=6), max_size=4),
)
def test_every_search_tool_reports_result_count_and_a_more_exist_signal(
    monkeypatch: pytest.MonkeyPatch, items: list[Any], entries: list[Any], titles: list[str]
) -> None:
    """`result_count` is `len(results)` everywhere, and no tool ships mute.

    Every search tool owes the agent some way to learn more matches exist:
    `total_results` for the two with an upstream count, `truncated` for
    find_in_paper, `result_count` alone where the provider reports no total.
    """
    _serve_crossref(monkeypatch, items)
    _serve_arxiv(monkeypatch, entries)

    async def fake_wiki(query: str, limit: int = 5) -> dict[str, Any]:
        return {"results": [{"title": t, "url": f"https://x/{t}"} for t in titles]}

    monkeypatch.setattr(wikipedia, "search", fake_wiki)

    ax = asyncio.run(server.search_arxiv("anything"))
    cr = asyncio.run(server.search_crossref_by_title("anything"))
    wk = asyncio.run(server.search_wikipedia("anything"))

    for response in (ax, cr, wk):
        assert response["result_count"] == len(response["results"])

    # The pair with an upstream count reports it, and it is an int on both --
    # Crossref omits `total-results` on some responses.
    assert set(ax) == set(cr) == {"total_results", "result_count", "results"}
    assert isinstance(ax["total_results"], int)
    assert isinstance(cr["total_results"], int)
    # Wikipedia has no upstream total and must not invent one.
    assert set(wk) == {"query", "result_count", "results"}


# ---------------------------------------------------------------------------
# The unindexable report
# ---------------------------------------------------------------------------

_reasons = st.sets(
    st.sampled_from([*sorted(search._UNINDEXABLE_REASONS), "a_future_reason"]), max_size=3
)


@_SETTINGS
@given(reasons=_reasons)
def test_the_note_never_asserts_a_cause_that_is_not_present(reasons: set[str]) -> None:
    """One explanation per reason, and the residual asserts none.

    The note claimed non-Latin scripts yield no terms, which was never true of
    the files that reach it. An agent reading a cause that isn't there goes
    looking for a problem it doesn't have.
    """
    note = search._unindexable_note(reasons)

    for reason, explanation in search._UNINDEXABLE_REASONS.items():
        assert (explanation in note) == (reason in reasons)
    # A reason with no explanation falls through to the neutral residual
    # rather than borrowing another reason's.
    if not reasons & set(search._UNINDEXABLE_REASONS):
        assert "could not be indexed" in note
    assert "find_in_paper" in note


def test_every_reason_the_engine_records_has_an_explanation() -> None:
    """`_UNINDEXABLE_REASONS` must cover `cache_search`'s whole vocabulary.

    A reason added to the engine and not here degrades silently to the
    residual, which is honest but says less than the engine knew.
    """
    assert set(search._UNINDEXABLE_REASONS) == set(cache_search.UNINDEXABLE_REASONS)
