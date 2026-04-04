# academic-tools-mcp

An MCP server for academic research tools built on [OpenAlex](https://openalex.org/) and the [arXiv API](https://info.arxiv.org/help/api/). Designed to give LLM agents lean, focused responses for verifying paper metadata, authors, institutions, and generating BibTeX citations.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

## Tools

### OpenAlex

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

### arXiv

arXiv tools accept an `arxiv_id` parameter: bare ID (`2301.00001`), versioned (`2301.00001v2`), or URL (`https://arxiv.org/abs/2301.00001`). No API key required.

Responses are cached locally under `.cache/arxiv/papers/`. arXiv's rate limit (1 request per 3 seconds) is enforced automatically.

---

### `get_arxiv_paper_metadata`

Core metadata: title, dates, categories, links, and publication info.

```json
{
  "arxiv_id": "1706.03762v7",
  "title": "Attention Is All You Need",
  "published": "2017-06-12T17:57:34Z",
  "updated": "2023-08-02T00:52:10Z",
  "primary_category": "cs.CL",
  "categories": ["cs.CL", "cs.LG"],
  "pdf_url": "http://arxiv.org/pdf/1706.03762v7",
  "doi": "10.48550/arXiv.1706.03762",
  "journal_ref": "Advances in Neural Information Processing Systems 30 (2017)",
  "comment": "15 pages, 5 figures"
}
```

### `get_arxiv_paper_authors`

Author list with affiliations when available.

```json
{
  "authors": [
    {"name": "Ashish Vaswani", "affiliations": ["Google Brain"]},
    {"name": "Noam Shazeer", "affiliations": []},
    {"name": "Niki Parmar", "affiliations": ["Google Research"]}
  ]
}
```

### `get_arxiv_paper_abstract`

Title and abstract text.

```json
{
  "title": "Attention Is All You Need",
  "abstract": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks..."
}
```

### `get_arxiv_paper_bibtex`

BibTeX entry with `eprint`, `archiveprefix`, and `primaryclass` fields. Uses `@article` if the paper has a journal reference, otherwise `@misc`.

```json
{
  "bibtex": "@article{vaswani2017attention,\n  title={Attention Is All You Need},\n  author={Vaswani, Ashish and Shazeer, Noam and Parmar, Niki},\n  journal={Advances in Neural Information Processing Systems 30 (2017)},\n  year={2017},\n  eprint={1706.03762},\n  archiveprefix={arXiv},\n  primaryclass={cs.CL},\n  doi={10.48550/arXiv.1706.03762}\n}"
}
```

Which renders as:

```bibtex
@article{vaswani2017attention,
  title={Attention Is All You Need},
  author={Vaswani, Ashish and Shazeer, Noam and Parmar, Niki},
  journal={Advances in Neural Information Processing Systems 30 (2017)},
  year={2017},
  eprint={1706.03762},
  archiveprefix={arXiv},
  primaryclass={cs.CL},
  doi={10.48550/arXiv.1706.03762}
}
```

### `search_arxiv`

Search arXiv papers with field prefixes (`ti:`, `au:`, `abs:`, `cat:`) and boolean operators (`AND`, `OR`, `ANDNOT`). Returns up to 50 lean results.

```json
{
  "total_results": 1234,
  "papers": [
    {
      "arxiv_id": "1706.03762v7",
      "title": "Attention Is All You Need",
      "authors": ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"],
      "primary_category": "cs.CL",
      "published": "2017-06-12T17:57:34Z"
    }
  ]
}
```

### Paper PDF Pipeline

Download arXiv PDFs, convert to markdown with [MinerU](https://github.com/opendatalab/MinerU), and access content section-by-section. Requires MinerU installed in `~/.venvs/mineru`.

The pipeline is a four-step chain: `download_arxiv_pdf` → `convert_paper` → `get_paper_sections` → `get_paper_section`.

---

### `download_arxiv_pdf`

Download and cache the PDF for an arXiv paper. Skips download if already cached.

```json
{
  "path": "/path/to/.cache/arxiv/pdfs/1706.03762.pdf",
  "size_bytes": 2087448,
  "cached": false
}
```

### `convert_paper`

Convert a cached PDF to markdown using MinerU, then parse into H2-level sections. This is slow (5-10 minutes). Skips conversion if already cached.

```json
{
  "markdown_path": "/path/to/.cache/arxiv/markdown/1706.03762.md",
  "sections": [
    {"index": 0, "title": "Preamble", "h3s": [], "approx_tokens": 150},
    {"index": 1, "title": "Introduction", "h3s": [], "approx_tokens": 800},
    {"index": 2, "title": "Background", "h3s": ["Encoder-Decoder", "Attention"], "approx_tokens": 600},
    {"index": 3, "title": "Model Architecture", "h3s": ["Encoder and Decoder Stacks", "Attention", "Position-wise Feed-Forward Networks"], "approx_tokens": 2400}
  ],
  "cached": false
}
```

### `get_paper_sections`

Get the section index for a converted paper. Lightweight — returns only titles, H3 previews, and approximate token counts.

```json
{
  "sections": [
    {"index": 0, "title": "Introduction", "h3s": [], "approx_tokens": 800},
    {"index": 1, "title": "Methods", "h3s": ["Architecture", "Training"], "approx_tokens": 2400},
    {"index": 2, "title": "Results", "h3s": [], "approx_tokens": 1200}
  ]
}
```

### `get_paper_section`

Get the full markdown content of a specific section. Accepts an index number or a title substring (case-insensitive).

```json
{
  "title": "Methods",
  "content": "### Architecture\n\nThe model architecture is based on...\n\n### Training\n\nWe trained using...",
  "approx_tokens": 2400
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
- `.cache/arxiv/papers/` — parsed arXiv paper entries
- `.cache/arxiv/pdfs/` — downloaded arXiv PDFs
- `.cache/arxiv/markdown/` — MinerU-converted markdown
- `.cache/arxiv/sections/` — section index JSON

Cache has no expiration. All tools for a given entity share the same cached response, so only one API call is made regardless of how many tools you invoke.
