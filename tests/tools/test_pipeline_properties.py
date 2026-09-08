"""Property-based tests for the PDF pipeline tools.

Seven invariants that examples had been standing in for. Three seams.

The **response** seam: `tools/pipeline.py` is the last stop before the MCP
boundary, and `_strip_internal_paths` is the only thing between the cache
layout and the agent. It filters known key *names*, which is weaker than the
rule it enforces — no cache path crosses the boundary — so the property has to
be stated over values, not keys.

The **cascade** seam is the one that can destroy work. Whether the cached
markdown survives a download is decided by three independent facts (did it
error, did new bytes land, who wrote the markdown), and only one combination
may delete anything the operator cannot regenerate.

The **advice** seam: every error an agent receives has to say what to do next.
`app.not_converted_error`'s docstring makes it a contract — agents branch on
`suggestion`, never on the prose inside `error` — and the failures arrive from
four modules that each stop at their own vocabulary.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from academic_tools_mcp import manual, papers, server
from academic_tools_mcp.download import openaccess
from academic_tools_mcp.providers import acl, arxiv, biorxiv
from academic_tools_mcp.store import cache, stems
from academic_tools_mcp.tools import pipeline

from .test_cache_search_properties import identifiers
from .test_doi_properties import generic_dois

_SETTINGS = settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
_FEW = settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])

# Arbitrary JSON, for the payloads no upstream contract promises not to send.
_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3)
    ),
    max_leaves=8,
)
_payloads = st.dictionaries(st.text(max_size=6), _json_values, max_size=6)

# The domain the MCP boundary admits: SECTION_MAX_CHARS is `ge=1`, capped at
# _SECTION_HARNESS_CAP; SECTION_OFFSET is `ge=0`. Sampling outside it would
# test branches an agent cannot reach.
_max_chars = st.integers(min_value=1, max_value=200)

# What the recorded provenance of cached markdown can be.
_conversion_modes = st.sampled_from(["full", "fast", "imported", None])


def _seed_markdown(namespace: str, canonical: str, body: str) -> Path:
    md_path = stems.markdown_path(namespace, canonical)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(body, encoding="utf-8")
    return md_path


def _seed_index(namespace: str, canonical: str, markdown: str, mode: str | None) -> Path:
    md_path = _seed_markdown(namespace, canonical, markdown)
    sections, detected = papers.parse_sections_and_detect(markdown)
    cache.put(
        namespace,
        "sections",
        stems.sections_key(canonical),
        {
            "sections": sections,
            "sections_detected": detected,
            "markdown_checksum": stems.checksum_text(markdown),
            "conversion_mode": mode,
        },
    )
    return md_path


def _serve_download(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    """Make every direct provider answer with *payload*."""

    async def _download(identifier: str, *, force_refresh: bool = False) -> dict[str, Any]:
        return dict(payload)

    for module in (arxiv, acl, biorxiv, openaccess):
        monkeypatch.setattr(module, "download_pdf", _download)


# ---------------------------------------------------------------------------
# No cache path crosses the MCP boundary
# ---------------------------------------------------------------------------


@_SETTINGS
@given(payload=_payloads)
def test_stripping_removes_the_path_keys_and_nothing_else(payload: dict[str, Any]) -> None:
    """The filter is exact in both directions, and idempotent.

    A helper that drops one key too many silently truncates a tool response;
    one that drops too few is the leak it exists to prevent. Idempotence is
    what lets a caller wrap an already-stripped dict — `import_paper`'s
    markdown branch does, after `manual` has already been through it.
    """
    stripped = pipeline._strip_internal_paths(payload)

    assert set(stripped) == set(payload) - set(pipeline._INTERNAL_PATH_KEYS)
    for key, value in stripped.items():
        assert value == payload[key]
    assert pipeline._strip_internal_paths(stripped) == stripped


@_FEW
@given(identifier=identifiers, mode=_conversion_modes)
def test_no_pipeline_response_carries_a_cache_path(
    isolated_cache: Path, monkeypatch: pytest.MonkeyPatch, identifier: str, mode: str | None
) -> None:
    """The invariant behind the helper, stated over values rather than keys.

    `_strip_internal_paths` filters a fixed key list, so it is only as correct
    as that list is current. Asserting on the *values* is what fails the day a
    response gains a path under a key nobody added to `_INTERNAL_PATH_KEYS` —
    agents drive this pipeline by identifier, and a leaked path invites them to
    read the cache directly instead.
    """
    target = manual.resolve_target(identifier)
    ns, canonical = target["namespace"], target["canonical"]
    md_path = _seed_index(ns, canonical, "## A\n\nbody\n", mode)
    target["pdf_path"].parent.mkdir(parents=True, exist_ok=True)
    target["pdf_path"].write_bytes(b"%PDF-1.4 stub")

    _serve_download(monkeypatch, {"path": str(target["pdf_path"]), "size_bytes": 9, "cached": True})

    async def _convert(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "markdown_path": str(md_path),
            "sections": [],
            "sections_detected": False,
            "cached": True,
            "conversion_mode": mode,
        }

    monkeypatch.setattr(papers, "convert_pdf", _convert)

    responses = [
        asyncio.run(server.download_pdf(identifier)),
        asyncio.run(server.convert_paper(identifier)),
        asyncio.run(server.get_paper_sections(identifier)),
        asyncio.run(server.get_paper_section(identifier, "0")),
    ]

    root = str(cache.CACHE_ROOT)
    for response in responses:
        for key, value in response.items():
            assert not (isinstance(value, str) and root in value), (key, value)


# ---------------------------------------------------------------------------
# The download cascade
# ---------------------------------------------------------------------------


@_FEW
@given(
    identifier=identifiers,
    force_refresh=st.booleans(),
    cached=st.sampled_from([True, False, None]),
    errored=st.booleans(),
    mode=_conversion_modes,
)
def test_the_cascade_fires_exactly_when_replaceable_bytes_landed(
    isolated_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    identifier: str,
    force_refresh: bool,
    cached: bool | None,
    errored: bool,
    mode: str | None,
) -> None:
    """Three independent facts decide whether cached markdown survives.

    New bytes landed (`cached is False`, never merely falsy — an absent flag is
    not a claim of freshness); the download succeeded (a failed refresh keeps
    the old PDF, so its markdown is still accurate); and the markdown is
    converter output rather than a file the operator wrote, which no converter
    can reproduce. Deleting outside that intersection destroys work; not
    deleting inside it leaves `convert_paper` serving text for a PDF that is
    gone.
    """
    target = manual.resolve_target(identifier)
    ns, canonical = target["namespace"], target["canonical"]
    body = "## A\n\nbody\n"
    md_path = _seed_index(ns, canonical, body, mode)

    payload: dict[str, Any] = {"error": "boom", "retryable": True} if errored else {"size_bytes": 1}
    if cached is not None:
        payload["cached"] = cached
    _serve_download(monkeypatch, payload)

    result = asyncio.run(
        server.download_pdf(identifier, force_refresh=force_refresh, allow_oa_url=True)
    )

    should_cascade = not errored and cached is False and (force_refresh or mode != "imported")
    assert ("cascaded_invalidated" in result) is should_cascade, result

    if should_cascade:
        assert not md_path.exists()
        assert cache.get(ns, "sections", stems.sections_key(canonical)) is None
    else:
        assert md_path.read_text(encoding="utf-8") == body
        assert cache.get(ns, "sections", stems.sections_key(canonical)) is not None


# ---------------------------------------------------------------------------
# Every error says what to do next
# ---------------------------------------------------------------------------


@_FEW
@given(payload=_payloads, doi=generic_dois)
def test_every_download_error_carries_a_suggestion(
    isolated_cache: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], doi: str
) -> None:
    """Whatever the transport layer produced, the agent gets a next step.

    `http`'s vocabulary is a retry verdict, not advice, and the providers add
    none — so a withdrawn paper, a 503 and a size-cap abort all arrived as a
    dead end. The tool layer is the only place that knows `import_paper` exists.
    """
    _serve_download(monkeypatch, {**payload, "error": "upstream said no"})

    result = asyncio.run(server.download_pdf(doi, allow_oa_url=True))

    assert "suggestion" in result
    if "suggestion" in payload:
        # _enrich_error fills a gap; it never argues with the provider.
        assert result["suggestion"] == payload["suggestion"]


# The failure shapes `papers.convert_pdf` actually distinguishes. Drawn
# explicitly rather than hoped for out of arbitrary JSON — the branches only
# matter when the keys that select them are present.
_failure_shapes = st.sampled_from(
    [
        {},
        {"busy": True, "retryable": True},
        {"timed_out": True, "timeout_seconds": 120},
        {"retryable": False},
    ]
)


@_FEW
@given(payload=_payloads, shape=_failure_shapes, mode=st.sampled_from(["full", "fast"]))
def test_every_conversion_error_carries_a_suggestion(
    isolated_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    shape: dict[str, Any],
    mode: str,
) -> None:
    """And it must never contradict the `error` it rides beside.

    A single catch-all told a fast-mode timeout — whose own error says to raise
    `PDF_FAST_CONVERT_TIMEOUT` — that the PDF was corrupted and not to retry,
    which abandons a paper `mode='full'` converts. Advice that guesses at a
    cause is worse than advice that defers to the one already reported.
    """
    identifier = "convert-advice"
    target = manual.resolve_target(identifier)
    target["pdf_path"].parent.mkdir(parents=True, exist_ok=True)
    target["pdf_path"].write_bytes(b"%PDF-1.4 stub")

    async def _convert(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {**payload, **shape, "error": "conversion said no"}

    monkeypatch.setattr(papers, "convert_pdf", _convert)

    result = asyncio.run(server.convert_paper(identifier, mode=mode))
    suggestion = result["suggestion"]

    if "suggestion" in payload:
        assert suggestion == payload["suggestion"]
    elif shape.get("busy"):
        assert "mode='fast'" in suggestion
    elif shape.get("timed_out"):
        # The other mode, never the one that just ran out of time.
        assert f"mode='{'fast' if mode == 'full' else 'full'}'" in suggestion
        assert "corrupt" not in suggestion.lower()


# ---------------------------------------------------------------------------
# Reading a section
# ---------------------------------------------------------------------------


@_SETTINGS
@given(section=st.text(max_size=8))
def test_a_section_key_is_an_index_exactly_when_python_reads_it_as_one(
    isolated_cache: Path, section: str
) -> None:
    """`int()` is the whole dispatch, so its domain is the contract.

    `" 3 "` parses and `"0x3"` does not; a paper whose section is *titled* "3"
    can never be reached by that title. Pinning the split is what stops a
    hand-rolled "is it a digit" test from drifting away from it.
    """
    target = manual.resolve_target("key-dispatch")
    _seed_markdown(target["namespace"], target["canonical"], "## Alpha\n\nbody\n")

    result = asyncio.run(server.get_paper_section("key-dispatch", section))

    try:
        index = int(section)
    except ValueError:
        assert "out of range" not in result.get("error", "")
        return

    if index == 0:
        assert result["index"] == 0
    else:
        assert "out of range" in result["error"]


@_SETTINGS
@given(
    paragraphs=st.lists(
        st.text(alphabet="abc \n", min_size=1, max_size=40).filter(str.strip),
        min_size=1,
        max_size=6,
    ),
    max_chars=_max_chars,
)
def test_paging_a_section_reproduces_it_exactly(
    isolated_cache: Path, paragraphs: list[str], max_chars: int
) -> None:
    """Chaining `offset=next_offset` reassembles the body, with no gap or overlap.

    The end-to-end property `has_more` and `next_offset` exist to serve: an
    agent walks a long section by trusting them, and a slice that skips or
    repeats a character is invisible until someone reads the result.
    """
    body = "\n".join(paragraphs)
    target = manual.resolve_target("pager-prop")
    _seed_markdown(target["namespace"], target["canonical"], f"## A\n\n{body}\n")

    walked = ""
    offset = 0
    steps = 0
    while True:
        result = asyncio.run(
            server.get_paper_section("pager-prop", "A", offset=offset, max_chars=max_chars)
        )
        assert result["chars_returned"] <= max_chars
        walked += result["content"]
        if not result["has_more"]:
            assert result["next_offset"] is None
            break
        offset = result["next_offset"]
        steps += 1
        assert steps <= len(body) + 2, "the walk must terminate"

    assert len(walked) == result["total_chars"]
    assert walked == "\n".join(body.splitlines()).strip()


# ---------------------------------------------------------------------------
# import_paper routing
# ---------------------------------------------------------------------------


@_FEW
@given(suffix=st.text(alphabet="abcdefgmpMPD.", max_size=6), identifier=identifiers)
def test_import_routes_on_the_lowercased_suffix(
    isolated_cache: Path, tmp_path: Path, suffix: str, identifier: str
) -> None:
    """Routing is the extension, case-folded — and nothing else.

    A rejected extension must not have touched the cache on its way out: the
    tool resolves no namespace and writes no file, so a typo cannot leave a
    half-imported paper behind under the identifier the agent will reuse.
    """
    source = tmp_path / f"paper{suffix}"
    source.write_text("## A\n\nbody\n", encoding="utf-8")

    result = asyncio.run(server.import_paper(str(source), identifier))
    lowered = Path(source.name).suffix.lower()

    if lowered == ".pdf":
        # Text, so it fails the %PDF- sniff — but in the PDF branch, whose
        # rejection names the header rather than the extension.
        assert "%PDF-" in result["error"]
    elif lowered in pipeline._MARKDOWN_EXTS:
        assert result["section_count"] == 1
    else:
        assert "Unsupported file extension" in result["error"]
        assert "suggestion" in result
        target = manual.resolve_target(identifier)
        assert not stems.markdown_path(target["namespace"], target["canonical"]).exists()
        assert not target["pdf_path"].exists()
