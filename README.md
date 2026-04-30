# academic-tools-mcp

An [MCP](https://modelcontextprotocol.io/) server that gives LLM agents lean, focused tools for working with academic papers. Built on [FastMCP](https://github.com/jlowin/fastmcp).

Look up paper metadata, authors, abstracts, citations, and BibTeX entries. Download and read full paper PDFs section-by-section. Explore reference and citation graphs. Cross-reference with Wikipedia.

## Data Sources

| Provider | What it provides | Auth required |
|----------|-----------------|---------------|
| [OpenAlex](https://openalex.org/) | Paper metadata, authors, abstracts, topics, citations, BibTeX | Optional API key (free) |
| [arXiv](https://arxiv.org/) | Preprint metadata, authors, abstracts, BibTeX, PDF download | None |
| [bioRxiv/medRxiv](https://www.biorxiv.org/) | Preprint metadata, authors, abstracts, BibTeX, PDF download | None |
| [ACL Anthology](https://aclanthology.org/) | PDF download for ACL venue papers (ACL, EMNLP, NAACL, etc.) | None |
| [Crossref](https://www.crossref.org/) | Reference lists, title search / DOI discovery | Optional email (for polite pool) |
| [OpenCitations](https://opencitations.net/) | Reference and citation links with cross-referenced IDs | None |
| [Wikipedia](https://www.wikipedia.org/) | Article search, summaries, page existence checks | Optional email (for User-Agent) |

All API responses are cached locally. Multiple tool calls for the same paper = one API hit. Concurrent calls for the same paper are coalesced into a single fetch (request single-flight), transient failures (5xx, 429, timeouts) get one transparent retry, and definitive 404s are negative-cached for 24 hours so retry-happy agents don't burn rate budget on guaranteed misses.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/hunter-heidenreich/academic-tools-mcp.git
cd academic-tools-mcp
uv sync
cp .env.example .env   # then edit .env with your values
```

## Configuration

All configuration is via environment variables in `.env`. Nothing is required to get started, but some variables unlock higher rate limits.

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENALEX_API_KEY` | No | Free API key from [openalex.org](https://openalex.org/settings/api) |
| `OPENALEX_MAILTO` | No | Your email — gets you into the [polite pool](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication#the-polite-pool) (faster) |
| `CROSSREF_MAILTO` | No | Your email — gets you into the Crossref polite pool (10 req/sec vs 5) |
| `WIKIPEDIA_MAILTO` | No | Your email — required by [Wikimedia policy](https://meta.wikimedia.org/wiki/User-Agent_policy) for the User-Agent header |
| `PDF_CONVERTER` | No | PDF-to-markdown backend: `mineru` (default), `marker`, or a custom command (see [PDF Pipeline](#pdf-pipeline)) |
| `PDF_CONVERTER_VENV` | No | Path to a virtualenv to activate before running the converter (e.g. `~/.venvs/mineru`) |
| `PDF_CONVERT_TIMEOUT` | No | Hard timeout for a single PDF→markdown conversion in seconds (default `1800` = 30 min). Set to `none` / `off` / `disabled` to disable. |

## Usage

### With Claude Code

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

## Tools

### Papers (unified, auto-routed)

| Tool | Description |
|------|-------------|
| `get_paper_metadata` | Title, dates, venue / categories, identifiers — shape varies by `_source`. Optional `follow_published=True` auto-chains a bioRxiv preprint to its journal version on OpenAlex when one exists. |
| `get_paper_authors` | Author list with source-appropriate detail (affiliations, corresponding author, OpenAlex IDs) |
| `get_paper_abstract` | Plain text abstract |
| `get_paper_bibtex` | Ready-to-paste BibTeX entry |

Pass an arXiv ID (`2301.00001`, `hep-th/9901001`) or any DOI — including bioRxiv/medRxiv (`10.1101/...`), ACL Anthology (`10.18653/v1/...`), or generic publisher DOIs. Each response carries a `_source` field (`"arxiv"` / `"biorxiv"` / `"openalex"`) so you know which provider answered and which fields to expect. arXiv IDs always route to arXiv; bioRxiv DOIs route to bioRxiv; everything else (including ACL) routes to OpenAlex.

| Tool | Description |
|------|-------------|
| `search_arxiv` | Search arXiv with field prefixes (`ti:`, `au:`, `abs:`, `cat:`) and boolean operators |

### Authors

| Tool | Description |
|------|-------------|
| `get_author` | Name, ORCID, institutions (current + historical with years), h-index, i10-index, works/citation counts, top topics |

Accepts OpenAlex author IDs (from `get_paper_authors`) or ORCIDs.

### PDF pipeline (unified)

| Tool | Description |
|------|-------------|
| `download_pdf` | Download and cache the PDF — auto-detects arXiv, ACL Anthology, bioRxiv/medRxiv |
| `convert_paper` | Convert PDF to markdown, parse into sections (slow: tens of minutes; `PDF_CONVERT_TIMEOUT` caps it at 30 min by default). The server runs at most one conversion at a time across all callers — a second concurrent caller gets `{busy: True, retryable: True, in_progress: {...}}` immediately rather than queueing |
| `get_paper_sections` | Section index with titles, sub-heading previews, token counts |
| `get_paper_section` | Markdown of a section (by index or title substring); truncated by default (16000 chars) |

All four tools accept any identifier (arXiv ID, DOI, or freeform label) and auto-route to the correct provider's cache namespace. For papers not hosted on arXiv/ACL/bioRxiv, fetch the PDF yourself and hand it to `import_paper` — see [Manual import](#manual-import) below.

### References and citations (DOI required)

| Tool | Description |
|------|-------------|
| `get_paper_references_count` | Survey outgoing-reference coverage across both Crossref and OpenCitations in one call — returns per-source counts so you can pick which to page through |
| `get_paper_references` | Paginated outgoing references. Default `source="auto"` surveys both Crossref and OpenCitations in parallel and pages from whichever has more; pass `source="crossref"` for structured metadata or `source="opencitations"` for broader DOI coverage to skip the survey |
| `get_paper_citations_count` | Number of incoming citations (OpenCitations) |
| `get_paper_citations` | Paginated incoming citations with DOIs, dates, self-citation flags, and cross-referenced IDs (OpenCitations) |
| `search_crossref_by_title` | DOI discovery by bibliographic query (also works for bioRxiv papers); each hit warms the works cache so a follow-up `get_paper_metadata(doi)` is free |

For citations, follow the **count-then-page** pattern: call `get_paper_citations_count` first to see the total, then page through with `page` and `page_size`. For references the `source="auto"` default does the survey for you on the first call. Paginated responses include `_source` (on references) and `has_more` so agents know which shape to expect and when to stop. This prevents token blowouts on papers with long bibliographies or many citations.

**Source trade-off for references**: Crossref returns structured reference metadata (author, title, year, journal, DOI) when publishers deposit it; quality varies. OpenCitations aggregates from Crossref, PubMed, DataCite, OpenAIRE, and JaLC — it may have entries Crossref lacks, but returns DOI-to-DOI links only (no bibliographic metadata).

### Manual import

| Tool | Description |
|------|-------------|
| `import_paper` | Import a local `.pdf` (e.g. from Zotero or a file you downloaded) or pre-converted `.md`/`.markdown` with a user-supplied identifier. File type is detected by extension. |

For PDFs outside arXiv/bioRxiv/ACL, fetch the file yourself (browser, `curl`, publisher portal, institutional proxy) and then call `import_paper` — the server deliberately does not download arbitrary URLs.

After importing a PDF, use the unified pipeline tools (`convert_paper` → `get_paper_sections` → `get_paper_section`) with the same identifier. Markdown imports skip the conversion step and go straight to `get_paper_sections` / `get_paper_section`.

**Provider-aware routing**: if the identifier is an arXiv ID, bioRxiv DOI, or ACL DOI, the file is stored in that provider's cache namespace automatically. A subsequent `download_pdf("2301.00001")` will find an already-imported PDF — no duplicates.

### Wikipedia

| Tool | Description |
|------|-------------|
| `search_wikipedia` | Search for articles matching a query |
| `get_wikipedia_summary` | Title, description, extract, URL, and page type (`standard` / `disambiguation`); errors if the page doesn't exist |

## PDF Pipeline

The PDF-to-markdown pipeline converts downloaded PDFs into section-level markdown that agents can read piece by piece, avoiding token blowouts from dumping entire papers into context.

The pipeline is **converter-agnostic**. Set `PDF_CONVERTER` in `.env` to choose your backend:

```bash
# Named backends
PDF_CONVERTER=mineru          # default — https://github.com/opendatalab/MinerU
PDF_CONVERTER=marker          # https://github.com/datalab-to/marker

# Custom command template — use {input} and {output_dir} placeholders
PDF_CONVERTER=my-tool --in "{input}" --out "{output_dir}"
```

If your converter lives in a virtualenv, set `PDF_CONVERTER_VENV`:

```bash
PDF_CONVERTER_VENV=~/.venvs/mineru
```

The converter must accept a PDF input path and an output directory, and produce one or more `.md` files in that directory. The pipeline finds the markdown file automatically.

**Note:** PDF converters are external tools with their own licenses. [MinerU](https://github.com/opendatalab/MinerU) is AGPL-3.0; [Marker](https://github.com/datalab-to/marker) is GPL. This project invokes them as CLI subprocesses and does not link or import their code. The PDF pipeline is entirely optional — all metadata, BibTeX, and citation tools work without it.

### Installing MinerU (example setup)

```bash
python -m venv ~/.venvs/mineru
source ~/.venvs/mineru/bin/activate
pip install mineru
```

Then in `.env`:

```bash
PDF_CONVERTER=mineru
PDF_CONVERTER_VENV=~/.venvs/mineru
```

## Caching

API responses and downloaded files are cached under `.cache/`:

```
.cache/
  openalex/works/          # OpenAlex work objects (JSON)
  openalex/authors/        # OpenAlex author objects (JSON)
  arxiv/papers/            # arXiv paper entries (JSON)
  arxiv/pdfs/              # Downloaded PDFs
  arxiv/markdown/          # Converted markdown
  arxiv/sections/          # Section indices (JSON)
  biorxiv/papers/          # bioRxiv paper entries (JSON)
  biorxiv/pdfs/            # Downloaded PDFs
  biorxiv/markdown/        # Converted markdown
  biorxiv/sections/        # Section indices (JSON)
  acl_anthology/pdfs/      # Downloaded PDFs
  acl_anthology/markdown/  # Converted markdown
  acl_anthology/sections/  # Section indices (JSON)
  crossref/works/          # Crossref work objects (JSON)
  opencitations/references/# OpenCitations reference lists (JSON)
  opencitations/citations/ # OpenCitations citation lists (JSON)
  wikipedia/summaries/     # Wikipedia page summaries (JSON)
  manual/pdfs/             # Manually imported PDFs
  manual/markdown/         # Converted markdown
  manual/sections/         # Section indices (JSON)
```

Cache keys are SHA-256 hashes of canonical identifiers. Writes are atomic (temp file + `os.replace`) so a crash mid-write can't leave a corrupt entry; corrupt entries from earlier versions self-heal on read. Positive entries have no expiration — delete `.cache/` to start fresh. **Negative entries** (definitive 404s) live in a sibling `_neg/` subdirectory under each entity with a 24-hour TTL, so retry-happy agents don't repeatedly hit the network for known-bad identifiers but newly-registered DOIs still surface within a day.

## Development

```bash
uv sync                          # Install dependencies
uv run pytest -v                 # Run all tests (363 tests)
uv run pytest tests/test_bibtex.py -v   # Run one test file
uv run pytest -k "test_particle" -v     # Run tests matching a pattern
```

## Architecture

```
server.py (18 MCP tools; FastMCP lifespan closes pooled clients on shutdown)
  │
  ├── API clients          openalex.py, arxiv.py, biorxiv.py,
  │                        crossref.py, opencitations.py, wikipedia.py
  │
  ├── PDF + content        acl_anthology.py (static PDF URLs)
  │                        manual.py        (local-file import)
  │                        papers.py        (PDF → markdown → sections;
  │                                          global single-conversion lock)
  │                        bibtex.py        (BibTeX generation)
  │
  └── Shared infrastructure (every API client routes through these)
        _http.py           one-shot retry, structured errors, backpressure
        _clients.py        per-provider pooled httpx.AsyncClient
        _singleflight.py   concurrent same-key callers coalesce to one fetch
        cache.py           atomic file cache with negative-cache (24h TTL)
```

**Key design decisions:**

- **Lean responses.** Tools return only what's needed — not the full API response. An agent calling `get_paper_authors` doesn't get flooded with unrelated metadata.
- **One tool per job, auto-routed.** The four core paper tools (`get_paper_metadata`, `get_paper_authors`, `get_paper_abstract`, `get_paper_bibtex`) dispatch on identifier shape rather than forcing the agent to pick between arXiv/bioRxiv/OpenAlex families. Provider-native fields are preserved and tagged with `_source`.
- **One API hit per entity.** All tools for a given DOI share one cached response. Concurrent same-key callers are coalesced by single-flight to one fetch.
- **Persistent connections, transparent retries.** Each provider holds one pooled `httpx.AsyncClient` so TCP+TLS handshakes are reused. Transient failures (5xx, 429, timeouts, network errors) get one in-process retry that honours `Retry-After` (capped) before surfacing to the agent.
- **Burst caps with structured backpressure.** Each provider refuses to stack more than 5 concurrent callers behind its rate-limit gap. The 6th gets `{error, retryable: True, backpressure: True}` immediately so the agent learns to slow down rather than waiting silently.
- **Negative caching for definitive 404s.** Known-bad identifiers are cached for 24h so retries don't burn rate budget; transient errors are NOT cached.
- **Single-conversion lock for PDFs.** At most one PDF→markdown subprocess runs at a time across the whole server; concurrent callers get a `busy` error with what's running and how long it's been going.
- **Count-then-page for large data.** Citation and reference tools expose a `_count` tool so agents can check sizes before fetching. `get_paper_references(source="auto")` does the survey for you.
- **Provider-aware routing.** Manual imports auto-detect identifier types and store in the correct provider's cache, preventing duplicates.
- **Subprocess isolation for PDF converters.** The PDF pipeline shells out to external tools rather than importing them, keeping the dependency tree light and avoiding license entanglement.
- **Pre-computed aggregates.** List responses include counts (`author_count`, `topic_count`, `total_sections`, etc.) so agents don't need follow-up calls to check sizes.
- **Structured error hints.** Error responses include a `suggestion` field with recovery guidance (e.g. which search tool to try).

## License

MIT — see [LICENSE](LICENSE).
