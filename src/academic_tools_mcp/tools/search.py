"""Search tools: arXiv / Crossref / in-paper / cached corpus / Wikipedia."""

import asyncio
import sqlite3
from typing import Annotated, Any

from pydantic import Field

from .. import cache_search, papers
from .._app import (
    _CACHE_SEARCH_NAMESPACE,
    _CACHE_SEARCH_TOP_K,
    _FIND_MAX_RESULTS,
    FORCE_REFRESH,
    PAPER_ID,
    _crossref_date,
    _dict_list,
    _enrich_error,
    _first,
    mcp,
    read_markdown,
)
from ..providers import arxiv, crossref, wikipedia


def _first_author_name(paper: dict[str, Any]) -> str | None:
    """The first arXiv author's name, or None when the entry lists none."""
    authors = _dict_list(paper.get("authors"))
    return authors[0].get("name") if authors else None


def _crossref_first_author(authors: list[dict[str, Any]]) -> str | None:
    """The first Crossref author's name, falling back to a consortium ``name``.

    Values are type-checked, not truth-checked: the rows arrive verbatim.
    """
    for a in authors:
        name_parts = [p for p in (a.get("given"), a.get("family")) if isinstance(p, str) and p]
        if name_parts:
            return " ".join(name_parts)
        org = a.get("name")
        if isinstance(org, str) and org:
            return org
    return None


def _published_year(paper: dict[str, Any]) -> int | None:
    """Leading year of an arXiv entry's ``published`` timestamp.

    ``isdecimal``, not ``isdigit``: the latter admits superscripts ``int()``
    rejects. Same trap as ``arxiv.search_papers``, same guard as ``_key_year``.
    """
    published = paper.get("published")
    if not isinstance(published, str) or len(published) < 4:
        return None
    return int(published[:4]) if published[:4].isdecimal() else None


@mcp.tool
async def search_arxiv(
    query: Annotated[
        str,
        Field(
            description="arXiv search query. Supports field prefixes: "
            "ti: (title), au: (author), abs: (abstract), cat: (category). "
            "Boolean operators: AND, OR, ANDNOT. "
            "Example: 'ti:attention AND au:vaswani'"
        ),
    ],
    max_results: Annotated[
        int,
        Field(
            description=f"Maximum results to return (1-{arxiv.MAX_SEARCH_RESULTS}).",
            ge=1,
            le=arxiv.MAX_SEARCH_RESULTS,
        ),
    ] = 10,
) -> dict[str, Any]:
    """Search arXiv papers. Returns a slim triage list.

    Returns ``{total_results, result_count, results: [{arxiv_id, title,
    first_author, author_count, published_year}, ...]}`` — the same envelope
    as search_crossref_by_title. ``total_results`` is how many matches exist
    upstream, ``result_count`` how many this call returned, so a much larger
    ``total_results`` means more exist. ``author_count`` without the author
    list keeps consortium papers from ballooning the response.

    Call get_paper_metadata(arxiv_id) for the full record — every hit is
    already cached, so it costs no request.
    """
    result = await arxiv.search_papers(query, max_results=max_results)
    if "error" in result:
        # A malformed query is `retryable: False`; advising a wait would send
        # the agent back at a call that cannot succeed.
        return _enrich_error(
            result,
            "Rewrite the query — arXiv rejected this one. Check the field "
            "prefixes (ti:/au:/abs:/cat:) and the AND/OR/ANDNOT operators."
            if result.get("retryable") is False
            else "Refine the query or retry if arXiv is temporarily unavailable.",
        )

    results = [
        {
            "arxiv_id": arxiv.id_from_entry(p),
            "title": p.get("title"),
            "first_author": _first_author_name(p),
            "author_count": len(_dict_list(p.get("authors"))),
            "published_year": _published_year(p),
        }
        for p in _dict_list(result.get("entries"))
    ]
    return {
        "total_results": result.get("total_results") or 0,
        "result_count": len(results),
        "results": results,
    }


@mcp.tool
async def search_crossref_by_title(
    title: Annotated[
        str,
        Field(description="Paper title or bibliographic query string."),
    ],
    year: Annotated[
        int | None,
        Field(description="Publication year to filter results. Optional but recommended."),
    ] = None,
    max_results: Annotated[
        int,
        Field(
            description=f"Maximum results to return (1-{crossref.MAX_SEARCH_ROWS}).",
            ge=1,
            le=crossref.MAX_SEARCH_ROWS,
        ),
    ] = 5,
) -> dict[str, Any]:
    """Search Crossref by title (bibliographic query). Returns a slim triage list.

    Finds the published DOI when you have only a title or an arXiv ID, and is
    the de facto bioRxiv search since Crossref indexes every bioRxiv DOI.

    Returns ``{total_results, result_count, results: [{doi, title,
    first_author, author_count, year}, ...]}`` — the same envelope as
    search_arxiv. A bibliographic query matches broadly, so ``total_results``
    runs far ahead of ``result_count``: refine the title rather than raising
    ``max_results``. Crossref dates may differ from arXiv preprint dates.

    Call get_paper_metadata(doi) for the full record.
    """
    response = await crossref.search_works(title, year=year, rows=max_results)
    if "error" in response:
        return _enrich_error(
            response, "Try a more specific title or use search_arxiv if it's a preprint."
        )

    results = []
    for item in response.get("items", []):
        # Rows arrive untyped; count the list the name was picked from.
        authors = _dict_list(item.get("author"))
        results.append(
            {
                "doi": item.get("DOI"),
                "title": _first(item.get("title")),
                "first_author": _crossref_first_author(authors),
                "author_count": len(authors),
                "year": _crossref_date(item)[0],
            }
        )

    return {
        # Crossref omits `total-results` on some responses; the key has to mean
        # the same thing here as in search_arxiv.
        "total_results": response.get("total_results") or 0,
        "result_count": len(results),
        "results": results,
    }


@mcp.tool
async def find_in_paper(
    identifier: PAPER_ID,
    query: Annotated[
        str,
        Field(
            description=(
                "Text to find. Always matched literally — regex characters are "
                "escaped, so you cannot pass a pattern."
            ),
            min_length=1,
        ),
    ],
    max_results: _FIND_MAX_RESULTS = 20,
    case_sensitive: Annotated[
        bool,
        Field(
            description=(
                "Match case-sensitively. Default False — academic prose "
                "capitalisation is unreliable."
            ),
        ),
    ] = False,
    whole_words: Annotated[
        bool,
        Field(
            description=(
                "Wrap the query in word boundaries so 'set' won't match "
                "'subset'. Default False (substring match)."
            ),
        ),
    ] = False,
    normalize: Annotated[
        bool,
        Field(
            description=(
                "Fold diacritics (NFKD + strip combining marks) before "
                "matching, so 'cafe' matches 'café' and vice versa. Offsets, "
                "match and snippet stay aligned to the original text. Word "
                "boundaries remain ASCII-oriented, so whole_words stays "
                "unreliable for non-Latin scripts."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Find every occurrence of a query inside one converted paper.

    Jumps straight to the part of a paper that discusses X instead of paging
    through sections. Pairs with search_cached_papers, which finds *which*
    paper mentions X.

    Returns ``{query, paper_identifier, result_count, truncated, results:
    [{section_index, section, char_offset, match, snippet}, ...]}``. Chain into
    ``get_paper_section(identifier, section_index, offset=char_offset)``;
    offsets align with that tool's stripped text. ``truncated`` means more
    matches exist than ``max_results`` returned. ``paper_identifier`` is the
    canonical cache key, which may differ from the spelling you passed
    (``arXiv:2301.00001v2`` → ``2301.00001v2``).

    Errors: not converted yet → ``{error, suggestion}`` naming the pipeline.
    No matches is not an error — ``{result_count: 0, results: []}``.
    """
    read = await read_markdown(
        identifier,
        lambda markdown: papers.find_in_markdown(
            markdown,
            query,
            max_results=max_results,
            case_sensitive=case_sensitive,
            whole_words=whole_words,
            normalize=normalize,
        ),
    )
    if isinstance(read, dict):
        return read
    target, (hits, truncated) = read
    return {
        "query": query,
        # Canonical, so every spelling of one paper correlates to one value.
        "paper_identifier": target["canonical"],
        "result_count": len(hits),
        "truncated": truncated,
        "results": hits,
    }


# One explanation per reason; never assert one cause for all of them.
_UNINDEXABLE_REASONS: dict[str, str] = {
    cache_search.NO_INDEXABLE_TOKENS: (
        "contain no letters or digits in any script (punctuation- or "
        "symbol-only), so there is nothing to index"
    ),
    cache_search.UNREADABLE: (
        "could not be read from the cache — re-import them with import_paper"
    ),
}

# How many to name; `unindexable_count` carries the true total.
_UNINDEXABLE_SAMPLE = 10


def _unindexable_note(reasons: set[str]) -> str:
    """Explain why the reported papers are absent from the keyword index."""
    described = [_UNINDEXABLE_REASONS[r] for r in sorted(reasons) if r in _UNINDEXABLE_REASONS]
    if not described:
        described = ["could not be indexed"]
    return (
        "These cached papers are not in the keyword index: they "
        + "; ".join(described)
        + ". They will never match this search. Use find_in_paper on them "
        "directly if you need to check their contents."
    )


@mcp.tool
async def search_cached_papers(
    query: Annotated[
        str,
        Field(
            description=(
                "Free-text query against the converted-markdown cache. "
                "Tokenised on words, stopwords dropped, and matched as a "
                "bag-of-words — 'variational dropout' ranks by how often each "
                "term appears, not strictly the bigram."
            ),
        ),
    ],
    top_k: _CACHE_SEARCH_TOP_K = 10,
    namespace: _CACHE_SEARCH_NAMESPACE = None,
    normalize: Annotated[
        bool,
        Field(
            description=(
                "Fold diacritics on both the query and the documents before "
                "tokenising, so 'cafe' and 'café' rank identically. Snippet "
                "offsets stay aligned to the original markdown."
            ),
        ),
    ] = False,
    force_refresh: Annotated[
        bool,
        Field(
            description=(
                "Rebuild every index entry instead of trusting the per-file "
                "mtime/size staleness check. The index updates incrementally "
                "on its own; use this only if a cached file changed without "
                "its modification time changing."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """BM25 keyword search across every paper you've already converted.

    Answers "which paper mentioned X?" — pair with find_in_paper for "where in
    that paper?". Also the only search for manually-imported papers, whose
    identifiers are freeform labels no provider search can find.

    Returns ``{query, result_count, results: [{namespace, canonical_id, score,
    title, snippet, section, section_index, char_offset, char_count}, ...]}``.
    Chain with ``get_paper_section(canonical_id, section_index)`` — not
    ``section``, which is heading text and repeats often enough to be rejected
    as ambiguous. ``char_offset`` and ``section_index`` are null when the term
    could not be located; the hit is real, just not centrable, so reach for
    find_in_paper to place it.

    When part of the corpus can never match, the response also carries
    ``unindexable_count``, ``unindexable`` (a sample of up to
    ``_UNINDEXABLE_SAMPLE`` ``{namespace, stem, canonical_id, reason}``
    records — chain ``canonical_id``, never ``stem``) and
    ``unindexable_note``. All three are absent when the corpus is clean.

    Empty results mean no cached paper matched, never a failure: an unreadable
    index returns ``{error, retryable: True, suggestion}`` instead.

    Limits: pure keyword match, no synonyms ("self-attention" won't surface a
    paper that only says "scaled dot-product attention"). Only converted
    papers are indexed. Scripts without whitespace word breaks (CJK) are
    indexed but findable only by whole runs, not sub-phrases — use
    find_in_paper there.
    """

    # One hop off the event loop, and one corpus walk: `search` refreshes the
    # index, so `unindexable` reads the state it just left behind.
    def _search_and_diagnose() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        hits = cache_search.search(
            query,
            top_k=top_k,
            namespace=namespace,
            normalize=normalize,
            force_refresh=force_refresh,
        )
        return hits, cache_search.unindexable(namespace, refresh=False)

    try:
        results, skipped = await asyncio.to_thread(_search_and_diagnose)
    except (sqlite3.Error, OSError) as exc:
        # Derived state, so a read failure is recoverable — but it must never
        # come back as "no paper mentions this". `_connect` rebuilds a corrupt
        # file already; what reaches here is a locked database or an
        # unreachable cache directory.
        return {
            "error": f"The local search index could not be read: {exc}",
            "retryable": True,
            "suggestion": (
                "Retry the search. If it keeps failing, call it once with "
                "force_refresh=True to rebuild the index, or use find_in_paper "
                "to search a specific paper directly."
            ),
        }
    response: dict[str, Any] = {
        "query": query,
        "result_count": len(results),
        "results": results,
    }

    # Reported only when non-empty, so the common response stays lean.
    if skipped:
        response["unindexable_count"] = len(skipped)
        response["unindexable"] = skipped[:_UNINDEXABLE_SAMPLE]
        response["unindexable_note"] = _unindexable_note({r["reason"] for r in skipped})
    return response


@mcp.tool
async def search_wikipedia(
    query: Annotated[
        str,
        Field(description="Search term or phrase to find Wikipedia articles for."),
    ],
    limit: Annotated[
        int,
        Field(
            description=f"Maximum results to return (1-{wikipedia.MAX_SEARCH_LIMIT}).",
            ge=1,
            le=wikipedia.MAX_SEARCH_LIMIT,
        ),
    ] = 5,
) -> dict[str, Any]:
    """Search Wikipedia for articles matching a query (titles + URLs only).

    Returns ``{query, result_count, results: [{title, url}, ...]}``. Wikipedia
    reports no upstream total, so ``result_count`` is the only "more exist"
    signal: a full page means refine the query. Pass a hit's title to
    get_wikipedia_summary for the article extract.

    Errors: outage / rate limit → ``{error, suggestion}`` with a retry hint.
    """
    response = await wikipedia.search(query, limit=limit)
    if "error" in response:
        return _enrich_error(
            response, "Wikipedia is temporarily unavailable; retry in a few seconds."
        )
    results = response.get("results", [])
    return {"query": query, "result_count": len(results), "results": results}


@mcp.tool
async def get_wikipedia_summary(
    title: Annotated[
        str,
        Field(
            description="Wikipedia article title (e.g. 'Cytochrome P450'). "
            "Spaces and underscores both work."
        ),
    ],
    force_refresh: FORCE_REFRESH = False,
) -> dict[str, Any]:
    """Fetch the structured summary (extract) of a Wikipedia article.

    Returns ``{title, description, extract, url, type, pageid}``. ``type`` is
    ``"standard"``, or ``"disambiguation"`` when ``extract`` is a list of
    candidate meanings instead of an article.

    Errors: page not found / outage → ``{error, suggestion}``. Use
    search_wikipedia first if you don't know the canonical title.
    """
    result = await wikipedia.get_summary(title, force_refresh=force_refresh)
    if "error" in result:
        return _enrich_error(
            result,
            "Try search_wikipedia to find the correct title, or retry if "
            "Wikipedia is temporarily unavailable.",
        )
    return result
