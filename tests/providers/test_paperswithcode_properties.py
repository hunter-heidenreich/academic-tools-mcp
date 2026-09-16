"""Property-based tests for the Papers with Code client.

Three seams: the identifier folds (an arXiv ID and a task or benchmark name each
become one cache key, and one URL segment that cannot escape its path), and the
response side, where no upstream contract promises the body's shape.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp.providers import paperswithcode as pwc

from .test_paperswithcode import _stub

_SETTINGS = settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])

_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3)
    ),
    max_leaves=8,
)

_new_ids = st.from_regex(r"\A\d{4}\.\d{4,5}\Z")


@_SETTINGS
@given(text=st.text(max_size=40))
def test_canonical_slug_is_idempotent_and_path_safe(text: str) -> None:
    slug = pwc.canonical_slug(text)
    assert pwc.canonical_slug(slug) == slug
    assert set(slug) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
    assert not slug.startswith("-") and not slug.endswith("-")


@_SETTINGS
@given(
    base=_new_ids,
    version=st.integers(min_value=1, max_value=20),
    prefix=st.sampled_from(["", "arXiv:", "https://arxiv.org/abs/"]),
)
def test_every_arxiv_spelling_is_one_key(base: str, version: int, prefix: str) -> None:
    assert pwc.canonical_arxiv_id(f"{prefix}{base}v{version}") == base
    assert pwc.canonical_arxiv_id(base) == base


@_SETTINGS
@given(body=_json_values)
def test_get_paper_never_raises_on_an_arbitrary_body(monkeypatch: Any, body: Any) -> None:
    _stub(monkeypatch, body)
    result = asyncio.run(pwc.get_paper("1706.03762", force_refresh=True))
    assert isinstance(result, dict)
    if "error" in result:
        assert result.get("retryable") is True
    else:
        assert isinstance(result["title"], str)
        assert all(isinstance(repo["url"], str) for repo in result["repositories"])


@_SETTINGS
@given(body=_json_values)
def test_search_never_raises_and_never_reads_a_wrong_shape_as_empty(
    monkeypatch: Any, body: Any
) -> None:
    _stub(monkeypatch, body)
    result = asyncio.run(pwc.search(f"q{id(body)}"))
    shaped = isinstance(body, dict) and isinstance(body.get("results"), list)
    if not shaped:
        assert result.get("retryable") is True
    elif "error" not in result:
        assert all(isinstance(hit["title"], str) for hit in result["results"])
