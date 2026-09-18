"""Search tools: arXiv / Crossref / in-paper / cached corpus / Wikipedia (search + summary)."""

import asyncio
import sqlite3
from typing import Annotated, Any

from pydantic import Field

from .. import corpus, papers
from ..app import (
    CACHE_SEARCH_NAMESPACE,
    CACHE_SEARCH_TOP_K,
    FIND_MAX_RESULTS,
    FORCE_REFRESH,
    PAGE,
    PAPER_ID,
    SEARCH_YEAR_MAX,
    SEARCH_YEAR_MIN,
    as_dict,
    crossref_date,
    dict_list,
    enrich_error,
    mcp,
    page_bounds,
    read_markdown,
    unwrap_first,
)
from ..providers import arxiv, crossref, openalex, wikipedia
from ..util import doinorm

ARXIV_SORT_BY = Annotated[
    arxiv.SearchSortBy,
    Field(
        description=(
            "'relevance' (default), 'submitted' (original submission date) or "
            "'updated' (latest version's date)."
        ),
    ),
]

ARXIV_SORT_ORDER = Annotated[
    arxiv.SearchSortOrder,
    Field(description="'descending' (default) or 'ascending'."),
]


def _first_author_name(paper: dict[str, Any]) -> str | None:
    """The first arXiv author's name, or None when the entry lists none."""
    authors = dict_list(paper.get("authors"))
    return authors[0].get("name") if authors else None


def _crossref_first_author(authors: list[dict[str, Any]]) -> str | None:
    """The first Crossref author's name, falling back to a consortium ``name``.

    Values are ``isinstance``-checked and non-empty-checked: the rows arrive verbatim.
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

    ``isdecimal``, not ``isdigit``: the latter admits superscripts ``int()`` rejects.
    Same trap as ``arxiv.search_papers``.
    """
    published = paper.get("published")
    if not isinstance(published, str) or len(published) < 4:
        return None
    return int(published[:4]) if published[:4].isdecimal() else None


def _arxiv_search_suggestion(result: dict[str, Any]) -> str:
    """Recovery advice for a failed arXiv search, one per cause."""
    if result.get("beyond_window"):
        return (
            "Narrow the query instead of paging further — add a cat: prefix or a "
            "submittedDate range, or sort by 'submitted' and split by date."
        )
    # A malformed query is `retryable: False`; a wait cannot help.
    if result.get("retryable") is False:
        return (
            "Rewrite the query — arXiv rejected this one. Check the field "
            "prefixes (ti:/au:/abs:/cat:) and the AND/OR/ANDNOT operators."
        )
    return "Refine the query or retry if arXiv is temporarily unavailable."


def _wikipedia_search_suggestion(result: dict[str, Any]) -> str:
    """Recovery advice for a failed Wikipedia search, one per cause."""
    # A refused request is `retryable: False`; a wait cannot help.
    if result.get("retryable") is False:
        return "Rewrite the query — Wikipedia rejected this one. Plain keywords work best."
    return "Wikipedia is temporarily unavailable; retry in a few seconds."


@mcp.tool
async def search_arxiv(
    query: Annotated[
        str,
        Field(
            description="arXiv search query. Supports field prefixes: "
            "ti: (title), au: (author), abs: (abstract), cat: (category). "
            "Boolean operators: AND, OR, ANDNOT. "
            "Date range: submittedDate:[YYYYMMDDTTTT TO YYYYMMDDTTTT] (GMT). "
            "Example: 'ti:attention AND au:vaswani AND "
            "submittedDate:[201701010000 TO 201712312359]'"
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
    page: PAGE = 1,
    sort_by: ARXIV_SORT_BY = "relevance",
    sort_order: ARXIV_SORT_ORDER = "descending",
) -> dict[str, Any]:
    """Search arXiv papers. Returns a slim triage list.

    Returns ``{total_results, result_count, results: [{arxiv_id, title,
    first_author, author_count, published_year}, ...]}``. ``total_results`` is
    the upstream match count, ``result_count`` what this call returned — a larger
    ``total_results`` means more exist. The author list is omitted so consortium
    papers can't balloon the response. Pages are ``max_results`` long; a page past
    ``total_results`` has empty ``results``.

    Errors: ``{error, suggestion}`` plus arXiv's verdict — ``retryable: false``
    for a query arXiv rejected, ``retryable: true`` for a transport or parse
    failure. arXiv serves only a query's leading results (the error names how
    many), so a page past them is refused without a request as ``retryable:
    false`` plus ``beyond_window: true``: narrow the query instead of paging on.

    Call get_paper_metadata(arxiv_id) for the full record — every hit is
    already cached, so it costs no request.
    """
    start, _ = page_bounds(page, max_results)
    result = await arxiv.search_papers(
        query, max_results=max_results, start=start, sort_by=sort_by, sort_order=sort_order
    )
    if "error" in result:
        return enrich_error(result, _arxiv_search_suggestion(result))

    results = [
        {
            "arxiv_id": arxiv.id_from_entry(p),
            "title": p.get("title"),
            "first_author": _first_author_name(p),
            "author_count": len(dict_list(p.get("authors"))),
            "published_year": _published_year(p),
        }
        for p in dict_list(result.get("entries"))
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
        Field(
            description="Publication year to filter results. Optional but recommended.",
            ge=SEARCH_YEAR_MIN,
            le=SEARCH_YEAR_MAX,
        ),
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

    Returns ``{total_results, result_count, results: [{doi, title, first_author,
    author_count, year}, ...]}``. A bibliographic query matches broadly, so
    ``total_results`` runs far ahead of ``result_count``: refine the title rather
    than raising ``max_results``. Crossref dates may differ from arXiv preprint
    dates.

    Errors: ``{error, suggestion}``, plus ``retryable: true`` on a transient or
    parse failure; an unclassified 4xx carries no verdict.

    Call get_paper_metadata(doi) for the full record.
    """
    response = await crossref.search_works(title, year=year, rows=max_results)
    if "error" in response:
        return enrich_error(
            response, "Try a more specific title or use search_arxiv if it's a preprint."
        )

    results = []
    for item in response.get("items", []):
        # Rows arrive untyped; count the list the name was picked from.
        authors = dict_list(item.get("author"))
        results.append(
            {
                "doi": item.get("DOI"),
                "title": unwrap_first(item.get("title")),
                "first_author": _crossref_first_author(authors),
                "author_count": len(authors),
                "year": crossref_date(item)[0],
            }
        )

    return {
        # Crossref sometimes omits `total-results`; the key must mean what it does in search_arxiv.
        "total_results": response.get("total_results") or 0,
        "result_count": len(results),
        "results": results,
    }


def _openalex_first_author(work: dict[str, Any]) -> str | None:
    """The first OpenAlex authorship's display name, or None."""
    authorships = dict_list(work.get("authorships"))
    return as_dict(authorships[0].get("author")).get("display_name") if authorships else None


@mcp.tool
async def search_openalex(
    query: Annotated[
        str,
        Field(
            description="Free-text query, matched against title, abstract and "
            "fulltext. Plain words, not a field syntax — for field-scoped "
            "queries use search_arxiv."
        ),
    ],
    year: Annotated[
        int | None,
        Field(
            description="Publication year to filter results. Optional.",
            ge=SEARCH_YEAR_MIN,
            le=SEARCH_YEAR_MAX,
        ),
    ] = None,
    max_results: Annotated[
        int,
        Field(
            description=f"Maximum results to return (1-{openalex.MAX_SEARCH_RESULTS}).",
            ge=1,
            le=openalex.MAX_SEARCH_RESULTS,
        ),
    ] = 10,
) -> dict[str, Any]:
    """Search all of OpenAlex by free text. Returns a slim triage list.

    The broadest discovery tool here — every discipline, where search_arxiv is
    preprints only and search_crossref_by_title matches bibliographically rather
    than on content. Use it when you have a topic rather than a title.

    Returns ``{total_results, result_count, results: [{doi, openalex_id, title,
    first_author, author_count, publication_year, cited_by_count, is_oa}, ...]}``.
    ``total_results`` is OpenAlex's own match count, ``result_count`` what this
    call returned. ``doi`` is null for the works OpenAlex indexes without one;
    ``openalex_id`` is always present. The author list is omitted so consortium
    papers can't balloon the response.

    Errors: ``{error, suggestion}``, plus ``retryable: true`` on a transient or
    parse failure.

    Chain get_paper_metadata(doi) for the full record: unlike a
    search_crossref_by_title hit, every hit here is already in the cache it
    reads, so the follow-up costs no request.
    """
    response = await openalex.search_works(query, year=year, rows=max_results)
    if "error" in response:
        return enrich_error(
            response,
            "Retry if OpenAlex is temporarily unavailable, or broaden the query — "
            "it matches words, not a field syntax.",
        )

    results = []
    for work in response.get("items", []):
        authorships = dict_list(work.get("authorships"))
        results.append(
            {
                "doi": doinorm.normalize(work["doi"]) if isinstance(work.get("doi"), str) else None,
                "openalex_id": work.get("id"),
                "title": work.get("title"),
                "first_author": _openalex_first_author(work),
                "author_count": len(authorships),
                "publication_year": work.get("publication_year"),
                "cited_by_count": work.get("cited_by_count"),
                "is_oa": as_dict(work.get("open_access")).get("is_oa"),
            }
        )

    return {
        # An int on every search tool that reports it, so agents can branch on it.
        "total_results": response.get("total_results") or 0,
        "result_count": len(results),
        "results": results,
    }


def _last_known_institution(author: dict[str, Any]) -> str | None:
    """The first of an OpenAlex author's last-known institutions, or None.

    Singular where ``get_author`` returns the whole list: enough to tell two
    same-named people apart, and no shared helper between the two tools.
    """
    for inst in dict_list(author.get("last_known_institutions")):
        if isinstance(name := inst.get("display_name"), str) and name:
            return name
    return None


@mcp.tool
async def search_authors(
    query: Annotated[
        str,
        Field(
            description="Author name to search for, e.g. 'Yoshua Bengio'. "
            "Plain text, not a field syntax.",
            min_length=1,
        ),
    ],
    max_results: Annotated[
        int,
        Field(
            description=f"Maximum results to return (1-{openalex.MAX_SEARCH_RESULTS}).",
            ge=1,
            le=openalex.MAX_SEARCH_RESULTS,
        ),
    ] = 10,
) -> dict[str, Any]:
    """Find an author by name on OpenAlex. Returns a slim triage list.

    The way into the author tools: get_author needs an ID only
    get_paper_authors could otherwise give you.

    Returns ``{total_results, result_count, results: [{openalex_id, name,
    orcid, last_known_institution, works_count, cited_by_count, h_index},
    ...]}``. ``total_results`` is OpenAlex's own match count, ``result_count``
    what this call returned. Every field but ``openalex_id`` may be null.

    **One name is often several records** — OpenAlex disambiguates imperfectly.
    Pick on ``works_count`` / ``cited_by_count`` / ``last_known_institution``,
    preferring the one with an ``orcid``.

    Errors: ``{error, suggestion}``, plus ``retryable: true`` on a transient or
    parse failure.

    Chain get_author(``openalex_id``, not ``orcid``) for affiliation history and
    top topics; every hit warms the cache it reads, so that call is free.
    """
    response = await openalex.search_authors(query, rows=max_results)
    if "error" in response:
        return enrich_error(
            response,
            "Retry if OpenAlex is temporarily unavailable, or try a different "
            "spelling — this matches names, not affiliations or topics.",
        )

    results = [
        {
            "openalex_id": author.get("id"),
            "name": author.get("display_name"),
            # Verbatim: OpenAlex returns the full https://orcid.org/... URL,
            # the one spelling get_author resolves.
            "orcid": author.get("orcid"),
            "last_known_institution": _last_known_institution(author),
            "works_count": author.get("works_count"),
            "cited_by_count": author.get("cited_by_count"),
            "h_index": as_dict(author.get("summary_stats")).get("h_index"),
        }
        for author in response.get("items", [])
    ]

    return {
        # An int on every search tool that reports it, so agents can branch on it.
        "total_results": response.get("total_results") or 0,
        "result_count": len(results),
        "results": results,
    }


AUTOCOMPLETE_ENTITY = Annotated[
    openalex.AutocompleteEntity,
    Field(description="Which OpenAlex entity collection to match the name against."),
]


@mcp.tool
async def autocomplete_openalex(
    query: Annotated[
        str,
        Field(
            description="A name or title prefix, e.g. 'university of mich' or "
            "'attention is all'. Matched as typeahead, not full text.",
            min_length=1,
        ),
    ],
    entity_type: AUTOCOMPLETE_ENTITY = "works",
) -> dict[str, Any]:
    """Match a name to an OpenAlex ID. Costs no credit budget.

    Use it when you know what a thing is *called*; search_openalex matches content
    and is metered ten times as heavily. The only route to get_institution.

    Returns ``{total_results, result_count, results: [{openalex_id, name, hint,
    entity_type, external_id, works_count, cited_by_count}, ...]}``.
    ``external_id`` is the entity's other identifier — a DOI for a work, a ROR for
    an institution, an ORCID for an author, an ISSN for a source. ``hint``
    disambiguates: authors for a work, a location for an institution. Every field
    but ``openalex_id`` may be null.

    OpenAlex serves a fixed page, so ``result_count`` caps at 10 whatever
    ``total_results`` reports; narrow the query rather than paging.

    Errors: ``{error, suggestion}``, plus ``retryable: true`` on a transient or
    parse failure.

    Chain get_paper_metadata(``external_id``) for a work, get_author /
    get_institution(``openalex_id``) otherwise. **These hits are not cached**,
    unlike search_openalex's, so that follow-up costs a request.
    """
    response = await openalex.autocomplete(query, entity_type=entity_type)
    if "error" in response:
        return enrich_error(
            response,
            "Retry if OpenAlex is temporarily unavailable, or shorten the query — "
            "this matches a name from its start, not words anywhere in a record.",
        )

    results = [
        {
            "openalex_id": item.get("id"),
            "name": item.get("display_name"),
            "hint": item.get("hint"),
            "entity_type": item.get("entity_type"),
            "external_id": item.get("external_id"),
            "works_count": item.get("works_count"),
            "cited_by_count": item.get("cited_by_count"),
        }
        for item in response.get("items", [])
    ]

    return {
        # An int on every search tool that reports it, so agents can branch on it.
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
    max_results: FIND_MAX_RESULTS = 20,
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

    Jumps to the part of a paper that discusses X instead of paging sections;
    search_cached_papers finds *which* paper mentions X.

    Returns ``{query, paper_identifier, result_count, truncated, results:
    [{section_index, section, char_offset, match, snippet}, ...]}``. Chain into
    ``get_paper_section(identifier, str(section_index), offset=char_offset)``;
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
    corpus.NO_INDEXABLE_TOKENS: (
        "contain no letters or digits in any script (punctuation- or "
        "symbol-only), so there is nothing to index"
    ),
    corpus.UNREADABLE: ("could not be read from the cache — re-import them with import_paper"),
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
    top_k: CACHE_SEARCH_TOP_K = 10,
    namespace: CACHE_SEARCH_NAMESPACE = None,
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
    Chain with ``get_paper_section(canonical_id, str(section_index))`` — not
    ``section``, which is heading text and repeats often enough to be rejected
    as ambiguous. ``char_offset`` and ``section_index`` are null when the term
    could not be located; the hit is real, just not centrable, so reach for
    find_in_paper to place it.

    When part of the corpus can never match, the response also carries
    ``unindexable_count``, ``unindexable`` (a capped sample of ``{namespace, stem,
    canonical_id, reason}`` records — chain ``canonical_id``, never ``stem``) and
    ``unindexable_note``, absent otherwise.

    Empty results mean no cached paper matched, never a failure: an unreadable
    index returns ``{error, retryable: True, suggestion}`` instead.

    Limits: pure keyword match, no synonyms ("self-attention" won't surface a
    paper that only says "scaled dot-product attention"). Scripts without
    whitespace word breaks (CJK) are indexed but findable only by whole runs,
    not sub-phrases — use find_in_paper there.
    """

    # One hop off the loop, one walk: `search` refreshes the index `unindexable` reads.
    def _search_and_diagnose() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        hits = corpus.search(
            query,
            top_k=top_k,
            namespace=namespace,
            normalize=normalize,
            force_refresh=force_refresh,
        )
        return hits, corpus.unindexable(namespace, refresh=False)

    try:
        results, skipped = await asyncio.to_thread(_search_and_diagnose)
    except (sqlite3.Error, OSError) as exc:
        # Derived state, so retryable — but never "no paper mentions this".
        # `_connect` rebuilds a corrupt file; this is a lock or an unreachable cache dir.
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
    """Search the full text of Wikipedia articles. Returns a slim triage list.

    Matches article content, not just titles, so a description of a topic finds it
    without naming its article. Use it to situate a concept, method or person
    encountered in a paper.

    Returns ``{query, total_results, result_count, did_you_mean, results: [{title,
    url, snippet}, ...]}``. ``total_results`` is Wikipedia's own match count,
    ``result_count`` what this call returned. ``snippet`` is the matched text as
    plain prose. ``did_you_mean`` is Wikipedia's spelling correction, null when it
    offers none — on zero hits it is the retry to make, not evidence the topic is
    absent. Pass a hit's title to get_wikipedia_summary for the article extract.

    Errors: ``{error, suggestion}`` plus Wikipedia's verdict — ``retryable: false``
    for a query it rejected, ``retryable: true`` for a transport or parse failure,
    with ``retry_after_seconds`` when the server advertises one.
    """
    response = await wikipedia.search(query, limit=limit)
    if "error" in response:
        return enrich_error(response, _wikipedia_search_suggestion(response))
    results = response.get("results", [])
    return {
        "query": query,
        # An int on every search tool that reports it, so agents can branch on it.
        "total_results": response.get("total_results") or 0,
        "result_count": len(results),
        "did_you_mean": response.get("did_you_mean"),
        "results": results,
    }


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

    Returns ``{title, canonical_title, description, extract, url, permalink,
    revision, timestamp, type, pageid, wikibase_item}``.

    ``wikibase_item`` is the Wikidata QID — the join from an article to the
    identifiers the rest of this server takes, so a person resolves on to ORCID and
    get_author, an institution to ROR and get_institution. ``permalink`` pins the
    ``revision`` this call read and is what to cite: ``url`` tracks the live page,
    which is whatever the article later becomes. ``canonical_title`` is what the
    request resolved to, so it differs from the title you asked for when that title
    is a redirect. ``extract`` is empty unless ``type`` is ``"standard"`` — a
    disambiguation page's extract describes one candidate meaning and would be
    miscited as the article's.

    Errors: page not found → ``{error, not_found: true, suggestion}``; an outage →
    ``{error, retryable: true, suggestion}``. Use search_wikipedia first if you
    don't know the canonical title.
    """
    result = await wikipedia.get_summary(title, force_refresh=force_refresh)
    if "error" in result:
        return enrich_error(
            result,
            "Try search_wikipedia to find the correct title, or retry if "
            "Wikipedia is temporarily unavailable.",
        )
    return result
