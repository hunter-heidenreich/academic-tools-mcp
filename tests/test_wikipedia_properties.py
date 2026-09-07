"""Property-based tests for the Wikipedia client.

Four invariants that examples had been standing in for, on the two seams this
module owns: what a *title* becomes on the way out, and what an arbitrary
response becomes on the way back.

The title side is why this file exists. Wikipedia has no DOI to normalize, so
`canonical_title` is the only thing standing between a pasted article title and
both the cache key and the URL path segment — and unlike a DOI, a title is
free-form user text. Examples cover the titles someone thought of.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import unquote

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import cache
from academic_tools_mcp.providers import wikipedia

from .test_wikipedia import _stub, _stub_no_network

_SETTINGS = settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])

# Free-form article titles, including the characters a path segment reserves
# (`/`, `#`, `?`) — which is exactly what `quote(..., safe="")` exists to
# neutralise — and the whitespace MediaWiki collapses.
_titles = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    min_size=1,
    max_size=40,
)

# Arbitrary JSON, for the bodies no upstream contract promises not to send.
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3)
    ),
    max_leaves=8,
)

_SUMMARY_PATH_PREFIX = "/api/rest_v1/page/summary/"


# ---------------------------------------------------------------------------
# canonical_title
# ---------------------------------------------------------------------------


@given(title=_titles)
def test_canonicalizing_twice_changes_nothing(title: str) -> None:
    """Idempotence, as `_doi.canonical` holds it. A canonical form that keeps
    moving keys the same article twice — once under the pasted spelling and
    once under whatever the second pass produced."""
    once = wikipedia.canonical_title(title)
    assert wikipedia.canonical_title(once) == once


@given(title=_titles)
def test_a_canonical_title_carries_no_whitespace_and_no_empty_segment(title: str) -> None:
    """MediaWiki treats space and underscore as one character and collapses
    runs of them, so the canonical form must too: otherwise "Cytochrome P450",
    "Cytochrome_P450" and "Cytochrome  P450" are three cache entries for one
    article, free to disagree."""
    canonical = wikipedia.canonical_title(title)

    assert not any(c.isspace() for c in canonical)
    assert "__" not in canonical
    assert not canonical.startswith("_")
    assert not canonical.endswith("_")


@given(title=_titles)
def test_only_the_leading_character_is_case_folded(title: str) -> None:
    """Wikipedia auto-capitalizes the first letter and nothing else, so `PET`
    (the scan) and `Pet` (the animal) are distinct articles. Folding further
    serves one summary under the other's key."""
    canonical = wikipedia.canonical_title(title)
    underscored = "_".join(title.replace("_", " ").split())

    assert canonical[1:] == underscored[1:]
    # Length-preserving too: `"ß".upper()` is `"SS"`, which would shift the
    # rest of the title and name a different article.
    assert len(canonical) == len(underscored)


# ---------------------------------------------------------------------------
# Request side
# ---------------------------------------------------------------------------


@_SETTINGS
@given(title=_titles)
def test_the_requested_path_round_trips_to_the_canonical_title(monkeypatch, title: str) -> None:
    """A surviving reserved character doesn't fail loudly — httpx fetches a
    different, existing path, whose answer then caches under this title's key.
    `AC/DC` is the everyday case: the slash is part of the title, not a
    separator."""
    requests = _stub(monkeypatch, {"type": "standard", "title": title})

    # force_refresh so every example fetches: hypothesis reuses one cache root,
    # and two titles differing only in spacing share a canonical key.
    result = asyncio.run(wikipedia.get_summary(title, force_refresh=True))

    if not requests:
        # The other half of the same invariant: a title whose encoded form
        # would be *removed* from the path rather than escaped in it (a `.`/`..`
        # segment, or nothing at all) is refused instead of fetching /page.
        assert result["not_found"] is True
        return

    raw_path = requests[0].url.raw_path.decode()
    assert raw_path.startswith(_SUMMARY_PATH_PREFIX)
    tail = raw_path.removeprefix(_SUMMARY_PATH_PREFIX)
    assert "/" not in tail
    assert "#" not in tail
    assert "?" not in tail
    assert unquote(tail) == wikipedia.canonical_title(title)


@_SETTINGS
@given(title=_titles, spacing=st.sampled_from([" ", "_", "  ", " _ ", "\t"]))
def test_every_spelling_of_one_title_is_one_fetch(monkeypatch, title: str, spacing: str) -> None:
    """One article, one cache entry. Divergent keying here doesn't fail — it
    silently doubles the requests and lets two spellings of one title hold two
    answers that can disagree."""
    requests = _stub(monkeypatch, {"type": "standard", "title": title})

    padded = f"{spacing}{title.replace(' ', spacing)}{spacing}"
    first = asyncio.run(wikipedia.get_summary(title, force_refresh=True))
    second = asyncio.run(wikipedia.get_summary(padded))

    if not requests:
        # A title the request-side guard refuses is refused identically
        # whichever way it was spaced — the same invariant. Only the error
        # message differs, echoing the spelling the caller passed.
        assert first["not_found"] is True
        assert second["not_found"] is True
        return
    assert len(requests) == 1
    assert first == second


# Every spelling that canonicalizes to a segment RFC 3986 removes or empties.
# Built rather than filtered out of `_titles`: random text lands on one of
# these about never, so a filter would leave the property untested.
_path_shortening_titles = st.builds(
    lambda lead, core, trail: f"{lead}{core}{trail}",
    st.text(alphabet=" _\t\n", max_size=4),
    st.sampled_from(["", ".", ".."]),
    st.text(alphabet=" _\t\n", max_size=4),
)


@_SETTINGS
@given(title=_path_shortening_titles)
def test_a_path_shortening_title_never_reaches_the_network(monkeypatch, title: str) -> None:
    """`quote` cannot escape a `.`/`..` segment — both characters are
    unreserved — so the guard, not the encoder, is what keeps the request on
    the record it was built for. `/page` answers 200 with a dict."""
    _stub_no_network(monkeypatch)  # any outbound request fails the test

    assert wikipedia.canonical_title(title) in ("", ".", "..")

    result = asyncio.run(wikipedia.get_summary(title, force_refresh=True))
    assert result["not_found"] is True


# ---------------------------------------------------------------------------
# Response side
# ---------------------------------------------------------------------------


@_SETTINGS
@given(body=_json_values)
def test_any_body_yields_one_of_two_shapes_and_never_raises(monkeypatch, body: Any) -> None:
    """`get_wikipedia_summary` passes the provider's dict straight to the
    agent, so anything escaping here arrives as a traceback — and the
    `AttributeError` a non-dict `content_urls` raises is in neither
    `_PARSE_ERRORS` nor `HTTPX_ERRORS`."""
    _stub(monkeypatch, body)

    result = asyncio.run(wikipedia.get_summary("Photosynthesis", force_refresh=True))

    assert isinstance(result, dict)
    if "error" in result:
        # A wrong shape is transient, never a definitive miss: reported as
        # "no such article" it would end the agent's search instead of a retry.
        assert result["retryable"] is True
        assert "not_found" not in result
        return

    assert set(result) == {"title", "description", "extract", "url", "type", "pageid"}
    assert isinstance(result["url"], str)


@_SETTINGS
@given(body=_json_values)
def test_only_a_well_formed_body_is_ever_positive_cached(monkeypatch, body: Any) -> None:
    """The expensive half: a wrong-shape 200 cached for the 30-day positive TTL
    serves the agent a wrong answer no retry can dislodge."""
    _stub(monkeypatch, body)

    result = asyncio.run(wikipedia.get_summary("Photosynthesis", force_refresh=True))
    cached = cache.get(wikipedia.NAMESPACE, "summaries", "Photosynthesis")

    if "error" in result:
        assert cached is None
    else:
        assert cached == result


@_SETTINGS
@given(body=_json_values)
def test_search_never_reports_a_wrong_shape_as_no_matches(monkeypatch, body: Any) -> None:
    """Every hit must be a title/url pair of strings, and a body that isn't the
    OpenSearch array must read as "retry" rather than "no such article" — two
    strings zip into per-character hits an agent would chain onto."""
    _stub(monkeypatch, body)

    result = asyncio.run(wikipedia.search("photosynthesis"))

    assert isinstance(result, dict)
    if "results" not in result:
        assert "error" in result
        return

    assert isinstance(result["results"], list)
    for hit in result["results"]:
        assert set(hit) == {"title", "url"}
        assert isinstance(hit["title"], str)
        assert isinstance(hit["url"], str)
