"""Search tools: arXiv / Crossref / in-paper / cached corpus / Wikipedia."""

import asyncio
from typing import Annotated, Any

from pydantic import Field

from .. import cache_search, manual, papers
from .._app import (
    _CACHE_SEARCH_NAMESPACE,
    _CACHE_SEARCH_TOP_K,
    _FIND_MAX_RESULTS,
    FORCE_REFRESH,
    PAPER_ID,
    _arxiv_id_from_entry,
    _crossref_date,
    _enrich_error,
    _first,
    mcp,
    not_converted_error,
)
from ..providers import arxiv, crossref, wikipedia


def _first_author_name(paper: dict[str, Any]) -> str | None:
    authors = paper.get("authors") or []
    if not authors:
        return None
    return authors[0].get("name")


def _published_year(paper: dict[str, Any]) -> int | None:
    published = paper.get("published") or ""
    if len(published) >= 4 and published[:4].isdigit():
        return int(published[:4])
    return None


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
        Field(description="Maximum results to return (1-50).", ge=1, le=50),
    ] = 10,
) -> dict[str, Any]:
    """Search arXiv papers. Returns a slim triage list.

    Each hit carries ``{arxiv_id, title, first_author, author_count,
    published_year}`` — enough to recognize the paper without the full
    author list (which can balloon to tens of KB on HEP/biology
    consortium papers). ``author_count`` lets the agent decide whether
    to call get_paper_authors directly or paginate. Call
    get_paper_metadata(arxiv_id) for the full record (free cache hit —
    each search entry is opportunistically cached).

    Returns ``{total_results, result_count, results: [...]}`` — same shape
    as search_crossref_by_title so an agent can branch on the source
    without learning per-tool field names. ``total_results`` is the total
    number of matches upstream (how many exist); ``result_count`` is how
    many hits this call returned (``len(results)``, capped by
    ``max_results``). A ``total_results`` far larger than ``result_count``
    means more matches exist than were returned.
    """
    result = await arxiv.search_papers(query, max_results=max_results)
    if "error" in result:
        return _enrich_error(
            result,
            "Refine the query or retry if arXiv is temporarily unavailable.",
        )

    results = [
        {
            "arxiv_id": _arxiv_id_from_entry(p),
            "title": p.get("title"),
            "first_author": _first_author_name(p),
            "author_count": len(p.get("authors") or []),
            "published_year": _published_year(p),
        }
        for p in result.get("entries", [])
    ]
    return {
        "total_results": result.get("total_results"),
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
) -> dict[str, Any]:
    """Search Crossref by title (bibliographic query). Returns a slim triage list.

    Each hit carries ``{doi, title, first_author, author_count, year}`` —
    enough to recognize the paper without the full author list (which
    can balloon on HEP/biology consortium papers). ``author_count`` lets
    the agent decide whether to call get_paper_authors directly or
    paginate. Call get_paper_metadata(doi) for the full record.

    Useful for finding the published DOI when you only have a title or
    arXiv ID. Also serves as the de facto search for bioRxiv papers,
    since Crossref indexes all bioRxiv DOIs.

    Returns ``{total_results, result_count, results: [...]}`` (same shape
    as search_arxiv). ``total_results`` is Crossref's upstream match count
    (how many exist); ``result_count`` is how many hits this call returned.
    Capped at 5 hits per call, so ``total_results`` is typically far larger
    than ``result_count`` — refine the title to narrow it. Year filtering
    is optional but recommended; note that Crossref publication dates may
    differ from arXiv preprint dates.
    """
    response = await crossref.search_works(title, year=year, rows=5)
    if "error" in response:
        return _enrich_error(
            response, "Try a more specific title or use search_arxiv if it's a preprint."
        )
    items = response.get("items", [])

    results = []
    for item in items:
        authors = item.get("author") or []
        first_author = None
        for a in authors:
            name_parts = [p for p in (a.get("given"), a.get("family")) if p]
            if name_parts:
                first_author = " ".join(name_parts)
                break
            # Consortium / organisational authors carry a `name` field
            # with no given/family — surface it rather than dropping to None.
            if a.get("name"):
                first_author = a["name"]
                break

        results.append(
            {
                "doi": item.get("DOI"),
                "title": _first(item.get("title")),
                "first_author": first_author,
                "author_count": len(authors),
                "year": _crossref_date(item)[0],
            }
        )

    return {
        "total_results": response.get("total_results"),
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
                "Text to find. Always matched literally — special regex "
                "characters are escaped automatically, so you cannot pass a "
                "pattern. whole_words=True only adds word boundaries around "
                "this literal so 'set' won't match 'subset'."
            ),
            min_length=1,
        ),
    ],
    max_results: _FIND_MAX_RESULTS = 20,
    case_sensitive: Annotated[
        bool,
        Field(
            description=(
                "If True, match the query case-sensitively. Default "
                "False — academic prose capitalisation is unreliable."
            ),
        ),
    ] = False,
    whole_words: Annotated[
        bool,
        Field(
            description=(
                "If True, wrap the query in word boundaries so 'set' "
                "won't match 'subset'. Default False (substring match)."
            ),
        ),
    ] = False,
    normalize: Annotated[
        bool,
        Field(
            description=(
                "If True, fold diacritics before matching (NFKD + strip "
                "combining marks) so 'cafe' matches 'café' and "
                "'Gutierrez' matches 'Gutiérrez' (and vice versa). "
                "Default False (literal match). char_offset, match, and "
                "snippet are still reported against the original "
                "(un-folded) text, so chaining into get_paper_section "
                "still lands on the match. Caveat: word boundaries are "
                "ASCII-oriented — folding makes diacritic Latin words "
                "work with whole_words, but non-Latin scripts (CJK, "
                "Arabic) stay unreliable for whole_words and are largely "
                "unaffected by folding."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """Find every occurrence of a query inside one converted paper.

    Use this when you want to jump straight to the part of a paper that
    discusses X, instead of paging through every section with
    get_paper_section. Pairs well with the BM25 corpus search
    (search_cached_papers) — that one tells you which paper mentions X,
    this one tells you where in the paper.

    Returns ``{query, paper_identifier, result_count, truncated, results:
    [...]}`` where each hit has ``{section_index, section, char_offset,
    match, snippet}``. Chain into get_paper_section(identifier,
    section_index, offset=char_offset) to read the surrounding context —
    offsets are aligned with that tool's stripped section text.

    ``truncated`` is ``True`` when more matches exist than ``max_results``
    returned — raise ``max_results`` (or refine the query) to see the rest.

    ``normalize=True`` folds diacritics (NFKD + strip combining marks) so
    'cafe' matches 'café' (and vice versa); offsets/match/snippet stay
    aligned to the original text. Word boundaries remain ASCII-oriented.

    Errors: paper not converted yet → ``{error, suggestion}`` pointing at
    the download_pdf → convert_paper pipeline. No matches → ``{result_count:
    0, results: []}`` (an empty result is not an error).
    """
    target = manual.resolve_target(identifier)
    md_path = papers.markdown_path(target["namespace"], target["canonical"])

    if not md_path.exists():
        return not_converted_error(identifier)

    # Read + scan off the event loop — a large converted paper's disk read and
    # a query with thousands of matches would each otherwise pin it.
    #
    # Read UTF-8 explicitly: this was the one read path in the pipeline that
    # relied on the host locale, so under LC_ALL=C (containers, systemd units)
    # it raised UnicodeDecodeError straight out of the tool instead of
    # returning the {error} contract. Its siblings — get_paper_section,
    # _reparse_sections_locked, _convert_fast, import_markdown — were all
    # explicit already.
    def _read_and_scan() -> tuple[list[dict[str, Any]], bool]:
        markdown = md_path.read_text(encoding="utf-8")
        return papers.find_in_markdown(
            markdown,
            query,
            max_results=max_results,
            case_sensitive=case_sensitive,
            whole_words=whole_words,
            normalize=normalize,
        )

    try:
        hits, truncated = await asyncio.to_thread(_read_and_scan)
    except FileNotFoundError:
        # A concurrent force_refresh cascade unlinked the markdown between the
        # exists() check and the read. Degrade to the same clean error rather
        # than letting it escape, matching get_paper_section.
        return not_converted_error(identifier)
    return {
        "query": query,
        "paper_identifier": identifier,
        "result_count": len(hits),
        "truncated": truncated,
        "results": hits,
    }


# Per-reason explanations for the ``unindexable`` report. Built from the
# reasons actually present rather than asserting one cause for all of them:
# the note claimed non-Latin scripts yield no terms, which stopped being true
# when the probe moved to any-Unicode-letter-or-digit — and was never true of
# the files that reach it, which have no letters in any script at all.
_UNINDEXABLE_REASONS: dict[str, str] = {
    "no_indexable_tokens": (
        "contain no letters or digits in any script (punctuation- or "
        "symbol-only), so there is nothing to index"
    ),
    "unreadable": "could not be read from the cache — re-import them with import_paper",
}


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
                "Tokenised on words; stopwords dropped. Phrasal queries "
                "work as a bag-of-words (no positional matching), so "
                "'variational dropout' ranks docs by how often each "
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
                "If True, fold diacritics (NFKD + strip combining marks) "
                "on both the query and the documents before BM25 "
                "tokenisation, so 'cafe' and 'café' rank identically. "
                "Default False. Snippet offsets stay aligned to the "
                "original markdown."
            ),
        ),
    ] = False,
    force_refresh: Annotated[
        bool,
        Field(
            description=(
                "If True, rebuild every entry of the on-disk search index "
                "from scratch instead of trusting the per-file mtime/size "
                "staleness check. Default False — the index updates "
                "incrementally on its own. Use only if a cached markdown "
                "file changed without its modification time changing."
            ),
        ),
    ] = False,
) -> dict[str, Any]:
    """BM25 full-text search across every paper you've already converted.

    Walks ``.cache/<namespace>/markdown/*.md`` for every namespace (or
    just the one passed via ``namespace=``) and ranks each document
    against the query using standard BM25. Useful for:

      - Recovering a paper by content when you don't remember the
        identifier ("which paper mentioned variational dropout?")
      - Finding all cached papers that discuss a concept
      - Triage on a manual-import collection where the identifier is a
        freeform label and search_arxiv / search_crossref_by_title
        can't help

    Returns ``{query, result_count, results: [{namespace, canonical_id,
    score, title, snippet, section, section_index, char_offset,
    char_count}, ...]}``. ``snippet`` is a ~200-char window centred on the
    most-distinct cluster of matching terms.

    Chain with **section_index**, not ``section``:
    ``get_paper_section(canonical_id, section_index)``. ``section`` is the
    heading's text, and headings repeat — roughly one paper in nine has two
    sections with the same title, and passing a repeated title back is
    rejected as an ambiguous match. ``section_index`` is unambiguous and is
    computed with the same boundaries get_paper_section uses.

    Hits with score 0 (no query term appears) are dropped — empty
    results means the cache contains no relevant paper, not that the
    search failed. Backed by a persistent incremental index: only papers
    that changed since the last search are re-tokenised, so repeat
    searches stay fast as the corpus grows.

    ``normalize=True`` folds diacritics on both query and documents
    before tokenising, so 'cafe' and 'café' rank identically (useful for
    diacritic-heavy author names and terms); pure keyword matching still
    applies.

    Limits: pure keyword match (BM25 doesn't know synonyms — "self-
    attention" won't surface a paper that only says "scaled dot-
    product attention"). Only converted papers are searchable; PDFs
    that haven't been through convert_paper / import_paper are not in
    the index.

    **Scripts without whitespace word breaks (CJK) are indexed but only
    findable by whole runs.** The tokeniser splits on whitespace and
    punctuation, so an unbroken run of Han/Kana/Hangul is a single term: a
    query must repeat that entire run to match, and a sub-phrase of it returns
    nothing. There is no ``unindexable`` warning for this — those papers *are*
    indexed. Use find_in_paper for sub-phrase lookups in such a paper.
    """
    # Wrap the synchronous BM25 pass in to_thread so it doesn't pin the
    # event loop on a large corpus. Even at hundreds of papers this is
    # tens of milliseconds, but agents may run searches concurrently
    # with HTTP fetches and we shouldn't starve those.
    results = await asyncio.to_thread(
        cache_search.search,
        query,
        top_k=top_k,
        namespace=namespace,
        normalize=normalize,
        force_refresh=force_refresh,
    )
    response: dict[str, Any] = {
        "query": query,
        "result_count": len(results),
        "results": results,
    }

    # Papers the index could not use are invisible to BM25 — correctly, they
    # have no searchable terms, but silently, which left an agent no way to
    # learn that part of the corpus was never considered. Reported only when
    # non-empty so the common response stays lean.
    skipped = await asyncio.to_thread(cache_search.unindexable, namespace)
    if skipped:
        response["unindexable_count"] = len(skipped)
        response["unindexable"] = skipped[:10]
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
        Field(description="Maximum results to return (1-10).", ge=1, le=10),
    ] = 5,
) -> dict[str, Any]:
    """Search Wikipedia for articles matching a query (titles + URLs only).

    Returns ``{query, result_count, results: [{title, url}, ...]}``. Capped
    at 10 hits. Use the title from a hit with get_wikipedia_summary to
    fetch the article extract.

    Errors: Wikipedia outage / rate limit → ``{error, suggestion}`` with a
    retry hint.
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

    Returns ``{title, description, extract, url, type, pageid}``. ``type``
    is ``"standard"`` for normal articles or ``"disambiguation"`` for
    disambiguation pages (where ``extract`` is typically a list of
    candidate meanings). Cached per article (30-day TTL); pass
    ``force_refresh=True`` to re-fetch an article that may have been edited.

    Errors: page not found / Wikipedia outage → ``{error, suggestion}``.
    Use search_wikipedia first if you don't already know the canonical
    title.
    """
    result = await wikipedia.get_summary(title, force_refresh=force_refresh)
    if "error" in result:
        return _enrich_error(
            result,
            "Try search_wikipedia to find the correct title, or retry if "
            "Wikipedia is temporarily unavailable.",
        )
    return result
