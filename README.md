# academic-tools-mcp

An MCP server for academic research tools built on [OpenAlex](https://openalex.org/). Designed to give LLM agents lean, focused responses for verifying paper metadata, authors, institutions, and generating BibTeX citations.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Tools

Paper tools accept a `doi` parameter (bare, prefixed, or full URL). Author tools accept an `author_id` (OpenAlex ID or ORCID). API key and mailto are configured via environment variables (see [Configuration](#configuration)).

Responses are cached locally under `.cache/openalex/` — no repeated API calls for the same entity.

---

### `get_paper_metadata`

Core metadata: title, year, type, venue, DOI, and open access info.

```json
{
  "title": "Exposing the Limitations of Molecular Machine Learning with Activity Cliffs",
  "doi": "https://doi.org/10.1021/acs.jcim.2c01073",
  "publication_year": 2022,
  "publication_date": "2022-12-01",
  "type": "article",
  "language": "en",
  "venue": "Journal of Chemical Information and Modeling",
  "is_oa": true,
  "oa_status": "hybrid",
  "oa_url": "https://doi.org/10.1021/acs.jcim.2c01073"
}
```

### `get_paper_authors`

Author names, positions, corresponding status, and institution names. Also returns a deduplicated list of all institutions.

```json
{
  "authors": [
    {
      "name": "Derek van Tilborg",
      "openalex_id": "https://openalex.org/A5087157931",
      "position": "first",
      "is_corresponding": false,
      "institutions": ["University Medical Center Utrecht", "Eindhoven University of Technology"]
    },
    {
      "name": "Alisa Alenicheva",
      "openalex_id": "https://openalex.org/A5024632622",
      "position": "middle",
      "is_corresponding": false,
      "institutions": []
    },
    {
      "name": "Francesca Grisoni",
      "openalex_id": "https://openalex.org/A5078946433",
      "position": "last",
      "is_corresponding": true,
      "institutions": ["Eindhoven University of Technology", "University Medical Center Utrecht"]
    }
  ],
  "all_institutions": [
    "University Medical Center Utrecht",
    "Eindhoven University of Technology"
  ]
}
```

### `get_paper_abstract`

Plain text abstract reconstructed from OpenAlex's inverted index.

```json
{
  "title": "Exposing the Limitations of Molecular Machine Learning with Activity Cliffs",
  "abstract": "Machine learning has become a crucial tool in drug discovery and chemistry at large..."
}
```

### `get_paper_citations_summary`

Citation count, reference count, and retraction status.

```json
{
  "title": "Exposing the Limitations of Molecular Machine Learning with Activity Cliffs",
  "cited_by_count": 218,
  "referenced_works_count": 101,
  "is_retracted": false
}
```

### `get_paper_topics`

Topic classifications with field hierarchy, plus keywords with relevance scores.

```json
{
  "title": "Exposing the Limitations of Molecular Machine Learning with Activity Cliffs",
  "topics": [
    {
      "name": "Computational Drug Discovery Methods",
      "score": 1.0,
      "subfield": "Computational Theory and Mathematics",
      "field": "Computer Science",
      "domain": "Physical Sciences"
    }
  ],
  "keywords": [
    {"keyword": "Machine learning", "score": 0.7528},
    {"keyword": "Drug discovery", "score": 0.5626}
  ]
}
```

### `get_paper_bibtex`

Ready-to-paste BibTeX entry. Automatically selects the correct entry type based on the work type:

| Work type | BibTeX type |
|-----------|-------------|
| article, review, letter, editorial | `@article` |
| preprint, posted-content | `@misc` |
| proceedings-article | `@inproceedings` |
| book-chapter | `@incollection` |
| book, monograph | `@book` |
| dissertation | `@phdthesis` |
| report | `@techreport` |

```json
{
  "bibtex": "@article{vantilborg2022exposing,\n  title={Exposing the Limitations of Molecular Machine Learning with Activity Cliffs},\n  author={van Tilborg, Derek and Alenicheva, Alisa and Grisoni, Francesca},\n  journal={Journal of Chemical Information and Modeling},\n  volume={62},\n  number={23},\n  pages={5938--5951},\n  year={2022},\n  publisher={American Chemical Society},\n  doi={10.1021/acs.jcim.2c01073}\n}"
}
```

Which renders as:

```bibtex
@article{vantilborg2022exposing,
  title={Exposing the Limitations of Molecular Machine Learning with Activity Cliffs},
  author={van Tilborg, Derek and Alenicheva, Alisa and Grisoni, Francesca},
  journal={Journal of Chemical Information and Modeling},
  volume={62},
  number={23},
  pages={5938--5951},
  year={2022},
  publisher={American Chemical Society},
  doi={10.1021/acs.jcim.2c01073}
}
```

### `get_author_profile`

Author summary: name, ORCID, current institutions, publication/citation counts, h-index, and top research topics. Accepts an OpenAlex author ID (from `get_paper_authors`) or ORCID.

```json
{
  "name": "Derek van Tilborg",
  "openalex_id": "https://openalex.org/A5087157931",
  "orcid": "https://orcid.org/0000-0003-4473-0657",
  "works_count": 18,
  "cited_by_count": 335,
  "h_index": 7,
  "i10_index": 5,
  "current_institutions": [
    "Institute for Complex Systems",
    "Eindhoven University of Technology"
  ],
  "top_topics": [
    {"name": "Computational Drug Discovery Methods", "count": 13},
    {"name": "Machine Learning in Materials Science", "count": 11}
  ]
}
```

### `get_author_affiliations`

Affiliation history with years, useful for verifying which institution an author was at when a paper was published.

```json
{
  "name": "Derek van Tilborg",
  "affiliations": [
    {
      "institution": "Eindhoven University of Technology",
      "country_code": "NL",
      "years": [2022, 2023, 2024, 2025, 2026]
    },
    {
      "institution": "University Medical Center Utrecht",
      "country_code": "NL",
      "years": [2022, 2024]
    },
    {
      "institution": "Wageningen University & Research",
      "country_code": "NL",
      "years": [2021]
    }
  ]
}
```

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `OPENALEX_API_KEY` | API key from [openalex.org/settings/api](https://openalex.org/settings/api) (free) |
| `OPENALEX_MAILTO` | Your email for the [polite pool](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication#the-polite-pool) (faster rate limits) |

Both are optional but recommended. If set, they are sent automatically on every request — the LLM agent never needs to know about them.

## Usage

### Claude Code

Add to your MCP config (`~/.claude/claude_code_config.json`):

```json
{
  "mcpServers": {
    "academic-tools": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/academic-tools-mcp", "python", "-m", "academic_tools_mcp.server"]
    }
  }
}
```

### Standalone

```bash
uv run python -m academic_tools_mcp.server
```

### FastMCP CLI

```bash
uv run fastmcp run src/academic_tools_mcp/server.py:mcp
```

## Caching

API responses are cached as JSON files under `.cache/<provider>/<entity>/`. Currently supports:

- `.cache/openalex/works/` — full OpenAlex work objects
- `.cache/openalex/authors/` — full OpenAlex author objects

Cache has no expiration. All tools for a given entity share the same cached response, so only one API call is made regardless of how many tools you invoke.
