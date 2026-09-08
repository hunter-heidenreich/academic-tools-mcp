"""Property-based tests for the OpenAlex client.

Four invariants that examples had been standing in for, all of them about the
same seam: what the module does with an OpenAlex *response*.

The batch path is the reason this file exists. It is the one getter that
bypasses `cache.cached_lookup`, it builds cache keys out of untyped JSON, and
its expensive failure is silent — negative-caching a DOI the response never
disproved files a live paper as absent for the full negative TTL, under a key
nobody re-checks. Examples cover the bodies someone thought of.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import unquote, urlsplit

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp.providers import openalex
from academic_tools_mcp.store import cache
from academic_tools_mcp.util import doinorm
from tests.util.test_doinorm_properties import generic_dois

from .test_openalex import _BAD_JSON, _stub_json_responses, _work_response

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

# Every `doi.org` resolver spelling OpenAlex has been seen to store, in the
# casings a URL is allowed to carry.
_RESOLVER_PREFIXES = [
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "https://www.doi.org/",
    "HTTPS://DOI.ORG/",
    "Https://Dx.Doi.Org/",
]

# Arbitrary JSON, for the bodies no upstream contract promises not to send.
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3)
    ),
    max_leaves=8,
)


def _relax_pacing(monkeypatch) -> None:
    """Lift the burst cap so a wide fan-out isn't refused mid-property.

    Backpressure is a legitimate outcome the module has its own tests for; here
    it would only mask the invariant under test behind an error dict.
    """
    monkeypatch.setattr(openalex._throttle, "max_pending", 1000)


# ---------------------------------------------------------------------------
# Request side
# ---------------------------------------------------------------------------


@_SETTINGS
@given(doi=_bare_dois)
def test_the_requested_work_path_round_trips_to_the_bare_doi(monkeypatch, doi: str) -> None:
    """A surviving reserved character doesn't fail loudly — httpx fetches a
    different, existing record, which then caches under this DOI's key."""
    requests = _stub_json_responses(monkeypatch, _work_response(doi))

    # force_refresh so every example fetches: hypothesis reuses one cache root,
    # and two DOIs differing only in case share a canonical key.
    result = asyncio.run(openalex.get_work(doi, force_refresh=True))

    if not requests:
        # The other half of the same invariant: a suffix whose encoded form
        # would be *removed* from the path rather than escaped in it (a `.`/`..`
        # segment) is refused outright instead of fetching a shorter path — a
        # different, existing record, which would then cache under this key.
        assert result["not_found"] is True
        return

    path = urlsplit(str(requests[0].url)).path
    assert path.startswith("/works/doi:")
    tail = path.removeprefix("/works/doi:")
    assert "#" not in tail
    assert "?" not in tail
    assert unquote(tail) == doinorm.normalize(doi)


@_SETTINGS
@given(author_id=st.text(alphabet=_path_hostile_chars, min_size=1, max_size=30))
def test_the_requested_author_path_round_trips_to_the_normalized_id(
    monkeypatch, author_id: str
) -> None:
    """The path `.claude/rules/providers.md` used to name as the one identifier
    interpolated without `quote`."""
    requests = _stub_json_responses(monkeypatch, {"id": "https://openalex.org/A1"})

    result = asyncio.run(openalex.get_author(author_id, force_refresh=True))

    if not requests:
        # The other half of the same invariant: an id whose encoded form would
        # be *removed* from the path rather than escaped in it (a `.`/`..`
        # segment) is refused outright instead of fetching a different endpoint.
        assert result["not_found"] is True
        return

    path = urlsplit(str(requests[0].url)).path
    assert path.startswith("/authors/")
    tail = path.removeprefix("/authors/")
    assert "#" not in tail
    assert "?" not in tail
    assert unquote(tail) == openalex._normalize_author_id(author_id)


# ---------------------------------------------------------------------------
# Response side: the request/response key agreement the batch rests on
# ---------------------------------------------------------------------------


@given(doi=generic_dois, prefix=st.sampled_from(_RESOLVER_PREFIXES))
def test_a_response_doi_canonicalizes_onto_the_key_we_asked_for(doi: str, prefix: str) -> None:
    """The invariant batch matching rests on. Disagree here and a work OpenAlex
    *did* return reads as unattributed, which (correctly, by design) blocks
    negative caching for the entire chunk — so the whole batch degrades to
    retryable errors with no way to tell why."""
    assert openalex._canonical_from_response_doi(f"{prefix}{doi}") == openalex.canonical_doi(doi)


@given(work_doi=_json_values)
def test_canonicalizing_a_response_doi_never_raises(work_doi: Any) -> None:
    """It runs *after* the request's try block has closed, so a raise here takes
    down the whole batch rather than becoming one error dict."""
    result = openalex._canonical_from_response_doi(work_doi)
    assert result is None or (isinstance(result, str) and result)


# ---------------------------------------------------------------------------
# get_works_batch
# ---------------------------------------------------------------------------


@_SETTINGS
@given(dois=st.lists(generic_dois, max_size=6))
def test_the_batch_answers_every_input_exactly_once_in_input_order(monkeypatch, dois) -> None:
    """Totality and ordering in one: covers dedupe, the safe/unsafe filter
    split, the `zip(..., strict=True)` alignment and the empty case."""
    _stub_json_responses(monkeypatch, {"meta": {"count": 0}, "results": []})
    _relax_pacing(monkeypatch)

    out = asyncio.run(openalex.get_works_batch(dois, force_refresh=True))

    expected: list[str] = []
    for d in dois:
        canonical = openalex.canonical_doi(d)
        if canonical not in expected:
            expected.append(canonical)
    assert list(out) == expected


@_SETTINGS
@given(body=_json_values)
def test_the_batch_never_raises_and_every_value_is_a_work_or_an_error(monkeypatch, body) -> None:
    """The tool layer indexes straight into these values, so anything escaping
    here reaches the agent as a traceback rather than an ``{error}``."""
    _stub_json_responses(monkeypatch, body)

    dois = ["10.1/a", "10.2/b"]
    out = asyncio.run(openalex.get_works_batch(dois, force_refresh=True))

    assert list(out) == dois
    for value in out.values():
        assert isinstance(value, dict)
        if "error" in value:
            # Definitive or retryable, never both and never neither.
            assert value.get("not_found") is True or value.get("retryable") is True


@_SETTINGS
@given(
    extra=st.lists(_json_values, max_size=2),
    reported=st.integers(min_value=0, max_value=5),
)
def test_a_miss_is_negative_cached_only_when_the_response_accounted_for_itself(
    monkeypatch, extra, reported
) -> None:
    """Negative-caching a DOI that a truncated page — or a record we could not
    attribute — merely failed to mention poisons a live entry for the full
    negative TTL, under a key nothing re-checks."""
    asked = ["10.1/present", "10.1/absent"]
    results = [_work_response("10.1/present"), *extra]
    _stub_json_responses(monkeypatch, {"meta": {"count": reported}, "results": results})

    asyncio.run(openalex.get_works_batch(asked, force_refresh=True))

    attributable = {
        openalex._canonical_from_response_doi(r.get("doi")) if isinstance(r, dict) else None
        for r in results
    }
    accounted = attributable <= set(asked) and reported <= len(results)

    negative = cache.get_negative("openalex", "works", "10.1/absent")
    assert (negative is not None) == accounted


@_SETTINGS
@given(n=st.integers(min_value=1, max_value=openalex._BATCH_CHUNK_SIZE))
def test_every_error_in_a_failed_chunk_is_its_own_object(monkeypatch, n: int) -> None:
    """`dict.fromkeys` put ONE dict behind up to `_BATCH_CHUNK_SIZE` keys, so a
    caller mutating its own error corrupted every other DOI in the batch."""
    _stub_json_responses(monkeypatch, _BAD_JSON)

    dois = [f"10.1/{i}" for i in range(n)]
    out = asyncio.run(openalex.get_works_batch(dois, force_refresh=True))

    assert len({id(v) for v in out.values()}) == n


# ---------------------------------------------------------------------------
# reconstruct_abstract
# ---------------------------------------------------------------------------

_words = st.text(alphabet=_path_hostile_chars, min_size=1, max_size=10)


@given(words=st.lists(_words, min_size=1, max_size=40))
def test_inverting_an_abstract_round_trips(words: list[str]) -> None:
    """The inverse of what OpenAlex did to build the index. Repeated words share
    a position list, which is the case the ordering has to survive."""
    index: dict[str, list[int]] = {}
    for position, word in enumerate(words):
        index.setdefault(word, []).append(position)

    assert openalex.reconstruct_abstract(index) == " ".join(words)


@given(index=_json_values)
def test_reconstructing_a_malformed_abstract_never_raises(index: Any) -> None:
    """`get_paper_abstract` has no try, and none of the AttributeError /
    TypeError a wrong shape raises is in `_PARSE_ERRORS` or `HTTPX_ERRORS`."""
    assert isinstance(openalex.reconstruct_abstract(index), str)
