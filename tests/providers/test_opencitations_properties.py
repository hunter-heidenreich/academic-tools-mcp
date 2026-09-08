"""Property-based tests for the OpenCitations client.

Four invariants that examples had been standing in for, all about the same
seam: what the module does with a DOI on the way out, and with an OpenCitations
*response* on the way back.

The response side is why this file exists. Every record is untyped JSON that
reaches `.get()` and `.split()` with no `except` clause between it and the
agent, and the payload is a bare list — so a body of the wrong shape has no
`id` key to fail on the way a singleton endpoint's would. Examples cover the
bodies someone thought of.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import unquote, urlsplit

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp.providers import opencitations
from academic_tools_mcp.store import cache
from academic_tools_mcp.util import doinorm

from .test_doi_properties import generic_dois
from .test_opencitations import _stub_json_responses, _stub_no_network

_SETTINGS = settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])

# `#` and `?` are included, unlike `test_doi_properties.dois`: they are exactly
# what `quote(..., safe="/")` exists to neutralise.
_path_hostile_chars = st.characters(
    min_codepoint=33,
    max_codepoint=0x2FFF,
    blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"),
).filter(lambda c: not c.isspace())

_bare_dois = st.builds(
    lambda registrant, suffix: f"10.{registrant}/{suffix}",
    st.integers(min_value=1000, max_value=99999999).map(str),
    st.text(alphabet=_path_hostile_chars, min_size=1, max_size=40),
)

# Every `doi.org` resolver spelling a pasted citation carries, in the casings a
# URL is allowed to be written in.
_RESOLVER_PREFIXES = [
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "HTTPS://DOI.ORG/",
    "Https://Dx.Doi.Org/",
    "doi:",
    "DOI: ",
]

# Arbitrary JSON, for the bodies no upstream contract promises not to send.
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3)
    ),
    max_leaves=8,
)

_DIRECTIONS = [("references", "cited"), ("citations", "citing")]


def _fetch(kind: str):
    return opencitations.get_references if kind == "references" else opencitations.get_citations


# ---------------------------------------------------------------------------
# Request side
# ---------------------------------------------------------------------------


@_SETTINGS
@given(doi=_bare_dois, direction=st.sampled_from(_DIRECTIONS))
def test_the_requested_path_round_trips_to_the_bare_doi(monkeypatch, doi, direction) -> None:
    """A surviving reserved character doesn't fail loudly — httpx fetches a
    different, existing path, whose answer then caches under this DOI's key."""
    kind, _field = direction
    requests = _stub_json_responses(monkeypatch, [])

    # force_refresh so every example fetches: hypothesis reuses one cache root,
    # and two DOIs differing only in case share a canonical key.
    result = asyncio.run(_fetch(kind)(doi, force_refresh=True))

    if not requests:
        # The other half of the same invariant: a suffix whose encoded form
        # would be *removed* from the path rather than escaped in it (a `.`/`..`
        # segment) is refused outright instead of fetching a shorter path.
        assert result["not_found"] is True
        return

    path = urlsplit(str(requests[0].url)).path
    assert path.startswith(f"/index/v2/{kind}/doi:")
    tail = path.removeprefix(f"/index/v2/{kind}/doi:")
    assert "#" not in tail
    assert "?" not in tail
    assert unquote(tail) == doinorm.normalize(doi)


@_SETTINGS
@given(
    doi=generic_dois,
    prefix=st.sampled_from(_RESOLVER_PREFIXES),
    direction=st.sampled_from(_DIRECTIONS),
)
def test_every_spelling_of_one_doi_is_one_fetch(monkeypatch, doi, prefix, direction) -> None:
    """One paper, one cache entry per direction. Divergent keying here doesn't
    fail — it silently doubles the requests and lets two spellings of one DOI
    hold two answers that can disagree."""
    kind, _field = direction
    requests = _stub_json_responses(monkeypatch, [])

    fetch = _fetch(kind)
    first = asyncio.run(fetch(doi, force_refresh=True))
    second = asyncio.run(fetch(f"{prefix}{doi}"))

    if not requests:
        # A spelling the request-side guard refuses is refused for *both*
        # spellings, before any keying happens — so the invariant here is
        # "neither fetched", not "same payload". The message echoes the
        # caller's own spelling, which every provider does by design.
        assert first["not_found"] is True
        assert second["not_found"] is True
        return

    assert first == second
    assert len(requests) == 1


# ---------------------------------------------------------------------------
# Response side
# ---------------------------------------------------------------------------


@_SETTINGS
@given(body=_json_values, direction=st.sampled_from(_DIRECTIONS))
def test_any_body_yields_one_of_two_shapes_and_never_raises(monkeypatch, body, direction) -> None:
    """tools/graph.py slices straight into `result[kind]`, so anything escaping
    here reaches the agent as a traceback rather than an ``{error}`` — and an
    `AttributeError` from a non-dict record is in neither `_PARSE_ERRORS` nor
    `HTTPX_ERRORS`."""
    kind, _field = direction
    _stub_json_responses(monkeypatch, body)

    result = asyncio.run(_fetch(kind)("10.1234/x", force_refresh=True))

    assert isinstance(result, dict)
    if "error" in result:
        # A wrong shape is transient, never a definitive miss: reported as
        # "no references" it would end the agent's search instead of a retry.
        assert result["retryable"] is True
        assert "not_found" not in result
        return

    entries = result[kind]
    assert isinstance(entries, list)
    assert all(isinstance(entry, dict) for entry in entries)
    assert result["count"] == len(entries)


@_SETTINGS
@given(body=_json_values, direction=st.sampled_from(_DIRECTIONS))
def test_only_a_well_formed_body_is_ever_positive_cached(monkeypatch, body, direction) -> None:
    """The expensive half: a wrong-shape 200 cached for the 7-day positive TTL
    serves the agent a wrong answer no retry can dislodge."""
    kind, _field = direction
    _stub_json_responses(monkeypatch, body)

    result = asyncio.run(_fetch(kind)("10.1234/x", force_refresh=True))
    cached = cache.get(opencitations.NAMESPACE, kind, "10.1234/x")

    if "error" in result:
        assert cached is None
    else:
        assert cached == result


@_SETTINGS
@given(doi=st.text(max_size=30), direction=st.sampled_from(_DIRECTIONS))
def test_a_path_shortening_identifier_never_reaches_the_network(
    monkeypatch, doi, direction
) -> None:
    """`quote` cannot escape a `.`/`..` segment — both characters are
    unreserved — so the guard, not the encoder, is what keeps the request on
    the record it was built for."""
    kind, _field = direction
    _stub_no_network(monkeypatch)  # any outbound request fails the test

    bare = doinorm.normalize(doi)
    segments = f"/index/v2/{kind}/doi:{bare}".split("/")
    shortens = not bare or bare.endswith("/") or any(s in (".", "..") for s in segments)
    if not shortens:
        return

    result = asyncio.run(_fetch(kind)(doi, force_refresh=True))
    assert result["not_found"] is True


# ---------------------------------------------------------------------------
# _parse_ids
# ---------------------------------------------------------------------------


@given(raw=_json_values | st.text(max_size=60))
def test_parse_ids_never_raises_and_yields_usable_pairs(raw: Any) -> None:
    """It takes `Any` because the value comes from untyped JSON. Every pair it
    emits is flattened onto a record and forwarded to the agent verbatim, so an
    empty key or value would read as a real identifier to chain a tool onto."""
    ids = opencitations._parse_ids(raw)

    assert isinstance(ids, dict)
    for prefix, value in ids.items():
        assert isinstance(prefix, str) and prefix
        assert isinstance(value, str) and value
        assert ":" not in prefix
        assert not prefix[0].isspace() and not value[0].isspace()
