from typing import Any

import httpx

from . import cache

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


async def get_work(doi: str, mailto: str | None = None) -> dict[str, Any]:
    """Fetch a work by DOI, using cache when available.

    Returns the full OpenAlex work object.
    """
    canonical = _canonical_doi(doi)

    cached = cache.get(NAMESPACE, "works", canonical)
    if cached is not None:
        return cached

    api_doi = f"doi:{_normalize_doi(doi)}"
    params = {}
    if mailto:
        params["mailto"] = mailto

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
