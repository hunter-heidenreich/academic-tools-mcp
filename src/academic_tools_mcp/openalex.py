from typing import Any

import httpx

from . import _http, cache, config

OPENALEX_BASE_URL = "https://api.openalex.org"
NAMESPACE = "openalex"


def _normalize_doi(doi: str) -> str:
    """Normalize a DOI to the format OpenAlex expects in the URL path.

    Accepts:
      - bare DOI: 10.1234/example
      - prefixed: doi:10.1234/example
      - full URL: https://doi.org/10.1234/example
    Returns the doi: prefixed form for the API path.
    """
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    elif doi.startswith("doi:"):
        doi = doi[len("doi:"):]
    return doi


def _canonical_doi(doi: str) -> str:
    """Return a canonical lowercase DOI string for cache keying."""
    return _normalize_doi(doi).lower()


def _build_params() -> dict[str, str]:
    """Build query params from environment config."""
    params: dict[str, str] = {}
    api_key = config.get("OPENALEX_API_KEY")
    if api_key:
        params["api_key"] = api_key
    mailto = config.get("OPENALEX_MAILTO")
    if mailto:
        params["mailto"] = mailto
    return params


def _normalize_author_id(author_id: str) -> str:
    """Normalize an author identifier for the API path.

    Accepts:
      - OpenAlex ID: A5023888391
      - Full OpenAlex URL: https://openalex.org/A5023888391
      - ORCID URL: https://orcid.org/0000-0001-6187-6610
    """
    if author_id.startswith("https://openalex.org/"):
        author_id = author_id[len("https://openalex.org/"):]
    return author_id


def _canonical_author_id(author_id: str) -> str:
    """Return a canonical author ID for cache keying."""
    return _normalize_author_id(author_id).lower()


async def get_author(author_id: str) -> dict[str, Any]:
    """Fetch an author by OpenAlex ID or ORCID, using cache when available.

    Returns the full OpenAlex author object.
    """
    canonical = _canonical_author_id(author_id)

    cached = cache.get(NAMESPACE, "authors", canonical)
    if cached is not None:
        return cached

    api_id = _normalize_author_id(author_id)
    params = _build_params()

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{OPENALEX_BASE_URL}/authors/{api_id}",
                params=params,
                timeout=30.0,
            )

        if response.status_code == 404:
            return {"error": f"No author found for ID: {author_id}"}

        response.raise_for_status()
        data = response.json()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("OpenAlex", e)

    cache.put(NAMESPACE, "authors", canonical, data)
    return data


async def get_work(doi: str) -> dict[str, Any]:
    """Fetch a work by DOI, using cache when available.

    Returns the full OpenAlex work object.
    """
    canonical = _canonical_doi(doi)

    cached = cache.get(NAMESPACE, "works", canonical)
    if cached is not None:
        return cached

    api_doi = f"doi:{_normalize_doi(doi)}"
    params = _build_params()

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{OPENALEX_BASE_URL}/works/{api_doi}",
                params=params,
                timeout=30.0,
            )

        if response.status_code == 404:
            return {"error": f"No work found for DOI: {doi}"}

        response.raise_for_status()
        data = response.json()
    except _http.HTTPX_ERRORS as e:
        return _http.error_dict("OpenAlex", e)

    cache.put(NAMESPACE, "works", canonical, data)
    return data


def reconstruct_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    """Reconstruct plain text from OpenAlex's inverted index abstract format."""
    if not inverted_index:
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(word for _, word in word_positions)
