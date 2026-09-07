"""Property-based tests for the unified paper tools.

Five invariants that examples had been standing in for. Two seams, and
`tools/paper.py` is the consumer side of both.

The response side is why this file exists. `openalex._fetch_singleton` checks
that a work is a dict carrying an `id` and nothing else; every level below that
arrives verbatim, and OpenAlex *nulls* a key rather than dropping it. A work
whose `authorships` is null — or whose rows are — is positive-cached, so a read
that assumes a list is a traceback the agent cannot retry away for the whole
TTL. `bibtex.py` has held this rule since it was written; the four formatters
here are the other readers of the same objects.

The pagination side has no upstream at all. `get_paper_authors` is the only
paginating tool in the module and no example ever passed `page` or
`page_size`, so the arithmetic every author response depends on — and the
`has_more` an agent's walk terminates on — was unpinned in both directions.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import _doi, server
from academic_tools_mcp.providers import arxiv, biorxiv, openalex
from academic_tools_mcp.tools import paper

from .test_arxiv_properties import bare_arxiv_ids
from .test_biorxiv_properties import biorxiv_dois
from .test_doi_properties import generic_dois

_SETTINGS = settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
# For the properties that drive several tool calls per example; the domain is
# small enough that fewer examples still cover it.
_FEW = settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])

# Arbitrary JSON, for the bodies no upstream contract promises not to send.
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3)
    ),
    max_leaves=8,
)

# The domain the MCP boundary actually admits: AUTHORS_PAGE is `ge=1`,
# AUTHORS_PAGE_SIZE is `ge=1, le=25`. Sampling outside it would test branches
# an agent cannot reach.
_pages = st.integers(min_value=1, max_value=10)
_page_sizes = st.integers(min_value=1, max_value=25)

_author_names = st.lists(st.text(min_size=1, max_size=6), max_size=40)

_ARXIV_ID = "2301.00001"
_BIORXIV_DOI = "10.1101/2024.01.01.123"
_OPENALEX_DOI = "10.1234/x"
_IDENTIFIERS = (_ARXIV_ID, _BIORXIV_DOI, _OPENALEX_DOI)


def _serve(monkeypatch: pytest.MonkeyPatch, names: list[str] | None = None) -> None:
    """Patch all three providers to answer any identifier with one record."""
    names = names or []
    flat = [{"name": n} for n in names]

    async def fake_arxiv(ident: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": f"http://arxiv.org/abs/{_ARXIV_ID}v1",
            "title": "T",
            "summary": "abstract",
            "authors": flat,
        }

    async def fake_biorxiv(doi: str, **kwargs: Any) -> dict[str, Any]:
        return {"doi": _BIORXIV_DOI, "title": "T", "abstract": "abstract", "authors": flat}

    async def fake_openalex(doi: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "https://openalex.org/W1",
            "title": "T",
            "authorships": [{"author": {"display_name": n}} for n in names],
        }

    async def fake_batch(dois: list[str], **kwargs: Any) -> dict[str, dict[str, Any]]:
        return {openalex.canonical_doi(d): {"id": "W1", "title": "T"} for d in dois}

    monkeypatch.setattr(arxiv, "get_paper", fake_arxiv)
    monkeypatch.setattr(biorxiv, "get_paper", fake_biorxiv)
    monkeypatch.setattr(openalex, "get_work", fake_openalex)
    monkeypatch.setattr(openalex, "get_works_batch", fake_batch)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


@_FEW
@given(names=_author_names, page_size=_page_sizes)
def test_the_author_walk_closes_for_every_source(
    monkeypatch: pytest.MonkeyPatch, names: list[str], page_size: int
) -> None:
    """Paging until `has_more` is false must yield each author once, in order.

    The end-to-end property `has_more` and the offsets exist to serve, and it
    has to hold identically for all three sources: an agent branches on
    `_source` for the *fields*, never for how to walk the pages.
    """
    _serve(monkeypatch, names)

    for identifier in _IDENTIFIERS:
        walked: list[Any] = []
        page = 1
        while True:
            result = asyncio.run(
                server.get_paper_authors(identifier, page=page, page_size=page_size)
            )
            assert result["author_count"] == len(names)
            assert len(result["authors"]) <= page_size
            walked.extend(a["name"] for a in result["authors"])
            if not result["has_more"]:
                break
            page += 1
            assert page <= len(names) + 2, f"the walk must terminate for {identifier}"

        assert walked == names, identifier


@_FEW
@given(names=_author_names, page=_pages, page_size=_page_sizes)
def test_a_page_past_the_end_is_empty_not_a_slice_from_the_end(
    monkeypatch: pytest.MonkeyPatch, names: list[str], page: int, page_size: int
) -> None:
    """An overshooting page returns nothing, never a wrapped-around window,
    and never claims another page follows it."""
    _serve(monkeypatch, names)

    for identifier in _IDENTIFIERS:
        result = asyncio.run(server.get_paper_authors(identifier, page=page, page_size=page_size))
        assert result["page"] == page
        assert result["page_size"] == page_size
        if (page - 1) * page_size >= len(names):
            assert result["authors"] == [], identifier
            assert result["has_more"] is False, identifier


@_SETTINGS
@given(names=_author_names, page=_pages, page_size=_page_sizes)
def test_the_institution_rollup_describes_only_its_own_page(
    names: list[str], page: int, page_size: int
) -> None:
    """`page_institutions` is documented as per-page, so it must be derivable
    from the page alone — an agent that dedupes across pages is relying on
    exactly that, and a roll-up leaking the global list would double-count."""
    work = {
        "authorships": [
            {"author": {"display_name": n}, "institutions": [{"display_name": f"I{i % 3}"}]}
            for i, n in enumerate(names)
        ]
    }
    start, end = (page - 1) * page_size, (page - 1) * page_size + page_size
    out = paper._format_openalex_authors(work, start, end)

    on_page = {i for a in out["authors"] for i in a["institutions"]}
    assert set(out["page_institutions"]) == on_page
    assert len(out["page_institutions"]) == len(set(out["page_institutions"])), "deduped"
    assert out["page_institution_count"] == len(out["page_institutions"])


# ---------------------------------------------------------------------------
# OpenAlex response payloads
# ---------------------------------------------------------------------------


# The recognized field names plus noise. Sampling these rather than free text
# is what gives the strategy teeth: a dictionary over arbitrary keys almost
# never lands on "authorships".
_authorship_rows = st.dictionaries(
    st.sampled_from(
        ["author", "institutions", "author_position", "is_corresponding", "raw_author_name"]
    ),
    _json_values,
    max_size=5,
)

# A work carrying `authorships` in every shape the guard allows: the list of
# rows OpenAlex documents, a list with non-dict rows in it, and the non-list
# value nothing upstream rules out — including the explicit `null` OpenAlex
# emits in place of dropping the key.
_openalex_works = st.fixed_dictionaries(
    {
        "authorships": st.one_of(
            st.lists(_authorship_rows, max_size=8),
            st.lists(_json_values, max_size=8),
            _json_values,
        ),
        "primary_location": _json_values,
        "open_access": _json_values,
    }
)


@_SETTINGS
@given(work=_openalex_works, page_size=_page_sizes)
def test_any_openalex_work_pages_without_raising(work: dict[str, Any], page_size: int) -> None:
    """`get_work` guarantees only that the work is a dict carrying an `id`.

    Anything escaping here reaches the agent as a traceback rather than a page,
    and does so for the whole positive TTL. The count and the slice must also
    describe the same objects, so the survey an agent pages against is honest.
    """
    out = paper._format_openalex_authors(work, 0, page_size)

    assert out["author_count"] == len(paper._dict_list(work.get("authorships")))
    assert len(out["authors"]) <= page_size
    assert all(isinstance(a, dict) for a in out["authors"])
    assert all(isinstance(n, str) and n for n in out["page_institutions"])
    assert out["page_institution_count"] == len(out["page_institutions"])


@_SETTINGS
@given(work=_openalex_works)
def test_any_openalex_work_formats_as_metadata_without_raising(work: dict[str, Any]) -> None:
    """The same guarantee for the metadata slice, which walks two levels into
    `primary_location` for the venue."""
    out = paper._format_openalex_metadata(work, "10.1/x")
    assert out["_source"] == "openalex"
    assert out["_canonical_id"] == "10.1/x"


@_SETTINGS
@given(
    profile=st.fixed_dictionaries(
        {
            "summary_stats": _json_values,
            "last_known_institutions": _json_values,
            "topics": _json_values,
            "affiliations": _json_values,
        }
    )
)
def test_any_openalex_author_profile_formats_without_raising(
    monkeypatch: pytest.MonkeyPatch, profile: dict[str, Any]
) -> None:
    """`get_author` shares `_fetch_singleton`'s guard, so its profile carries
    the same nulls. `top_topics` stays capped and `years` stays sortable."""

    async def fake_author(author_id: str, **kwargs: Any) -> dict[str, Any]:
        return {"id": "A1", "display_name": "N", **profile}

    monkeypatch.setattr(openalex, "get_author", fake_author)
    out = asyncio.run(server.get_author("A1"))
    assert len(out["top_topics"]) <= 5
    assert all(isinstance(n, str) for n in out["current_institutions"])
    for aff in out["affiliations"]:
        assert aff["years"] == sorted(aff["years"])


# ---------------------------------------------------------------------------
# The batch, and the echoed canonical id
# ---------------------------------------------------------------------------


@_FEW
@given(
    idents=st.lists(
        st.one_of(bare_arxiv_ids, biorxiv_dois, generic_dois, st.just("a freeform label")),
        min_size=1,
        max_size=8,
    )
)
def test_the_batch_answers_every_input_exactly_once_in_input_order(
    monkeypatch: pytest.MonkeyPatch, idents: list[str]
) -> None:
    """One slot per input, in order, `_input` echoed verbatim.

    Each identifier appears twice, which is the case the provider-level dedupe
    property cannot reach: `get_works_batch` answers with a *shorter*,
    canonical-keyed dict, and re-expanding it back to one entry per input is
    this tool's own step. An agent correlates by position and by `_input`.
    """
    calls = [i for ident in idents for i in (ident, ident)]
    _serve(monkeypatch)

    result = asyncio.run(server.get_papers_metadata(identifiers=calls))

    assert result["count"] == len(calls)
    assert [p["_input"] for p in result["papers"]] == calls
    for entry in result["papers"]:
        assert "_source" in entry or "error" in entry, "no slot may be left unexplained"


@_FEW
@given(doi=generic_dois)
def test_every_spelling_of_one_doi_echoes_one_canonical_id(
    monkeypatch: pytest.MonkeyPatch, doi: str
) -> None:
    """The echo is what an agent reuses across the family instead of
    re-normalizing whatever the user typed, so all four tools must agree — and
    the batch must still give each spelling its own slot under the one id."""
    _serve(monkeypatch)
    canonical = _doi.canonical(doi)
    spellings = [doi, f"doi:{doi}", f"DOI: {doi}", f"https://doi.org/{doi}", f"  {doi}  "]

    for spelling in spellings:
        for tool in (
            server.get_paper_metadata,
            server.get_paper_authors,
            server.get_paper_abstract,
            server.get_paper_bibtex,
        ):
            result = asyncio.run(tool(spelling))
            assert result["_canonical_id"] == canonical, f"{tool.__name__}({spelling!r})"

    batch = asyncio.run(server.get_papers_metadata(identifiers=spellings))
    assert [p["_input"] for p in batch["papers"]] == spellings
    assert {p["_canonical_id"] for p in batch["papers"]} == {canonical}
