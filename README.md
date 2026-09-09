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
| [Wikipedia](https://www.wikipedia.org/) | Article search, summaries | Optional email (for User-Agent) |

All API responses are cached locally. Multiple tool calls for the same paper = one API hit. Concurrent calls for the same paper are coalesced into a single fetch (request single-flight), transient failures (5xx, 429, timeouts) get one transparent retry honouring `Retry-After`, and definitive 404s are negative-cached (24h; 1h for arXiv/bioRxiv, whose identifiers go live mid-session) so retry-happy agents don't burn rate budget on guaranteed misses.

## Known upstream limitations

These are properties of the upstream providers rather than of this server, which does not attempt to paper over them. Treat them as caveats when citing a result.

- **Diacritics are dropped or mangled** in OpenAlex author names (`Alan Aspuru-Guzik` for `Alán Aspuru-Guzik`). Verify spellings against the publisher's page before quoting a name.
- **Affiliations are current, not paper-time.** OpenAlex reports where an author works *now*, not where they were when the paper was published — the gap widens for older papers.
- **A zero from OpenCitations is not a claim of absence.** OpenCitations answers a DOI it has never indexed and a DOI it indexed with zero edges identically — an empty list — so `get_paper_references(source="opencitations")` and `get_paper_citations` returning `total: 0` mean "no edges in this index", not "this paper has no references or citations". Cross-check against Crossref (`get_paper_references_count` reports both).
- **Preprint and published author lists diverge.** arXiv and the published DOI can list different author sets for the same work. `get_paper_metadata(doi, follow_published=True)` chains a bioRxiv preprint to its journal version, but only once OpenAlex has indexed that version; until then the response carries `followed_published: false` so you can tell you are looking at preprint-era metadata.

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
| `CROSSREF_MAILTO` | No | Your email — joins the Crossref [polite pool](https://www.crossref.org/documentation/retrieve-metadata/rest-api/). Not just a courtesy: the client picks its rate limits from whether this is set (10 req/sec singles / 3 search / 3 concurrent with it; 5 / 1 / 1 without). |
| `ARXIV_MAILTO` | No | Your email, appended to the arXiv User-Agent. A descriptive agent is sent either way — arXiv's edge throttles generic library agents far harder. |
| `CACHE_DIR` | No | Where the on-disk cache lives (default: `.cache/` beside the project). Set it when running from an installed wheel. |
| `ACADEMIC_TOOLS_ENV_FILE` | No | Explicit path to the `.env` to load. Authoritative: when set it is the *only* candidate, so a path that isn't there means no `.env` rather than a silent fallback to another file. |
| `MAX_PDF_BYTES` | No | Cap on a single PDF download (default `200000000` ≈ 200 MB). `none` / `off` / `disabled` / `0` disables; any other unparseable value (including a negative one) falls back to the default. |
| `WIKIPEDIA_MAILTO` | No | Your email, appended to the Wikipedia User-Agent. A descriptive agent is sent either way, which is what matters: [Wikimedia](https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits) caps an unidentified client at 10 req/min against 200 with a compliant one. |
| `PDF_CONVERTER` | No | PDF-to-markdown backend: `mineru` (default), `marker`, or a custom command (see [PDF Pipeline](#pdf-pipeline)) |
| `PDF_CONVERTER_VENV` | No | Path to a virtualenv to activate before running the converter (e.g. `~/.venvs/mineru`) |
| `PDF_CONVERT_TIMEOUT` | No | Hard timeout for a single PDF→markdown conversion in seconds (default `1800` = 30 min). Set to `none` / `off` / `disabled` / `0`, or any value `<= 0`, to disable. Unparseable or non-finite falls back to the default. |
| `PDF_FAST_CONVERTER` | No | Backend for `convert_paper(mode="fast")`: `pdftotext` (default, needs poppler-utils), `pymupdf` (needs the `fast` extra), or a custom command |
| `PDF_FAST_CONVERT_TIMEOUT` | No | Hard timeout for a fast extraction in seconds (default `120`). Same disable rules as `PDF_CONVERT_TIMEOUT`. |
| `DEBUG_REQUESTS` | No | `1` logs every throttled GET to **stderr** (never stdout — that's the JSON-RPC stream). Re-read per call, so it can be flipped without a restart. |
| `ENABLE_DEBUG_TOOLS` | No | `1` registers a `get_server_stats` tool exposing per-provider counters and the `.env` that won at import. Off by default so agents don't see operational data. Read at import — needs a restart. |

Setting `ACADEMIC_TOOLS_ENV_FILE` makes that file the only candidate. Otherwise `.env` is looked for in order: the project root (source checkouts), `$PWD/.env`, then `$XDG_CONFIG_HOME/academic-tools-mcp/.env` (falling back to `~/.config`). Real environment variables always win over the file, and a file that is unreadable or isn't UTF-8 is skipped rather than fatal.

Every value has its surrounding whitespace stripped, and one that is left empty reads as unset — a trailing space in a `.env` line is a typo, not a setting. Whitespace *inside* a value (a converter command template, a path) is preserved.

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
| `get_papers_metadata` | Bulk metadata for many identifiers at once. *Uncached* OpenAlex DOIs are chunked into batched `/works?filter=doi:...` calls — a cached one costs no request at all; arXiv / bioRxiv fan out concurrently. Designed for reference-graph enrichment after `get_paper_references`. Cap 100 per call. |
| `get_paper_authors` | Author list with source-appropriate detail (affiliations, corresponding author, OpenAlex IDs) |
| `get_paper_abstract` | Plain text abstract |
| `get_paper_bibtex` | Ready-to-paste BibTeX entry |

Pass an arXiv ID, any DOI, or a PMID. DOIs include bioRxiv/medRxiv (`10.1101/...`), ACL Anthology (`10.18653/v1/...`) and generic publisher DOIs. Each response carries a `_source` field (`"arxiv"` / `"biorxiv"` / `"openalex"`) so you know which provider answered and which fields to expect; `follow_published` adds `"openalex_via_biorxiv"` when the chain reaches the journal version. arXiv IDs always route to arXiv; bioRxiv DOIs route to bioRxiv; everything else (including ACL) routes to OpenAlex.

A **PubMed ID** works anywhere a DOI does — the paper tools, the PDF pipeline, and the reference/citation graph, whose OpenCitations rows hand PMIDs back. Spell it `pmid:20079334`, as a `pubmed.ncbi.nlm.nih.gov` URL, or as a bare 7–8 digit run; shorter runs need the `pmid:` prefix, so a short freeform `import_paper` label keeps meaning what it did. A PMID is traded for the paper's DOI on first use and never becomes a cache key of its own, so `pmid:20079334` and `10.1016/j.cell.2009.11.006` are one paper with one `_canonical_id`. OpenAlex is the resolver: a paper it has not indexed does not resolve, and PMCIDs are unsupported.

An arXiv ID is accepted in every spelling that names the same paper, so one paper never caches twice: bare (`2301.00001`, `2301.00001v2`, `hep-th/9901001`), arXiv's `arXiv:` "Cite as" prefix, an `abs`/`pdf`/`html` URL (any scheme or none, with or without a `www.`/`export.` host label), and arXiv's own DataCite DOI (`10.48550/arXiv.2301.00001`). The version suffix is part of the identity: `2301.00001` means "whatever is current" and `2301.00001v2` means that revision, and the two cache separately.

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
| `download_pdf` | Download and cache the PDF — auto-detects arXiv, ACL Anthology, bioRxiv/medRxiv. Streams chunks to disk (peak memory = 64 KiB) and aborts mid-stream if the response would exceed `MAX_PDF_BYTES` (default 200 MB). Whenever it actually downloads (not on a cache hit), the cached markdown + section index are dropped automatically so the next `convert_paper` picks up the new bytes. Markdown you imported yourself survives that; pass `force_refresh=True` to replace it too. |
| `convert_paper` | Convert PDF to markdown, parse into sections (slow: tens of minutes; `PDF_CONVERT_TIMEOUT` caps it at 30 min by default). The server runs at most one conversion at a time across all callers — a second concurrent caller gets `{busy: True, retryable: True, in_progress: {...}}` immediately rather than queueing |
| `get_paper_sections` | Section index with titles, sub-heading previews, token counts, and `conversion_mode` — what produced the markdown (`full` / `fast` / `imported`) |
| `get_paper_section` | Markdown of a section (by index or title substring); truncated by default (16000 chars) |
| `find_in_paper` | Substring (or whole-word) search inside one converted paper. Returns each hit's section + char offset + ~120-char snippet, and echoes `paper_identifier` in canonical form. Char offsets align with `get_paper_section`'s stripped text so you can chain straight to the surrounding context. |
| `search_cached_papers` | BM25 keyword search across **every** converted paper in the local cache. Answers "which paper mentioned X?"; pair it with `find_in_paper` for "where in that paper?". Papers the index could never use are reported separately as `unindexable`, each with a `canonical_id` you can hand straight to `find_in_paper`. |

`convert_paper(mode="fast")` runs a lightweight text-only extractor (`PDF_FAST_CONVERTER`, default `pdftotext`) outside the global conversion lock — seconds instead of minutes, but no tables, equations, figures, or real headings. Use it for triage; re-run in full mode when you need structure.

`download_pdf` natively handles arXiv, bioRxiv/medRxiv, and ACL Anthology. For any other publisher DOI it refuses by default rather than fetching arbitrary URLs; `download_pdf(doi, allow_oa_url=True)` opts into a narrow exception that fetches **only** the open-access PDF URL OpenAlex already surfaces for that work — never a caller-supplied URL.

Every tool above except `search_cached_papers` (which takes a query, not a paper) accepts any identifier — arXiv ID, DOI, or freeform label — and auto-routes to the correct provider's cache namespace. For papers not hosted on arXiv/ACL/bioRxiv, fetch the PDF yourself and hand it to `import_paper` — see [Manual import](#manual-import) below.

### References and citations (DOI required)

| Tool | Description |
|------|-------------|
| `get_paper_references_count` | Survey outgoing-reference coverage across both Crossref and OpenCitations in one call — returns per-source counts so you can pick which to page through |
| `get_paper_references` | Paginated outgoing references. Default `source="auto"` surveys both Crossref and OpenCitations in parallel and pages from the better-covered one, biased toward Crossref for its richer per-entry metadata (OpenCitations wins only on a materially larger reference list); pass `source="crossref"` for structured metadata or `source="opencitations"` for broader DOI coverage to skip the survey |
| `get_paper_citations_count` | Number of incoming citations (OpenCitations) |
| `get_paper_citations` | Paginated incoming citations with DOIs, dates, self-citation flags, and cross-referenced IDs (OpenCitations) |
| `search_crossref_by_title` | DOI discovery by bibliographic query (also works for bioRxiv papers); `max_results` widens the triage list up to Crossref's cap. Each hit warms the Crossref works cache, so a follow-up `get_paper_references(doi, source="crossref")` is free |

For citations, follow the **count-then-page** pattern: call `get_paper_citations_count` first to see the total, then page through with `page` and `page_size`. For references the `source="auto"` default does the survey for you on the first call. Paginated responses include `_source` (on references) and `has_more` so agents know which shape to expect and when to stop, and echo `doi` in canonical form so every spelling of one paper correlates to a single value across calls. This prevents token blowouts on papers with long bibliographies or many citations.

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
| `get_wikipedia_summary` | Title, description, extract, URL, page type (`standard` / `disambiguation`), and `pageid`; errors with `not_found` if the page doesn't exist |

## PDF Pipeline

The PDF-to-markdown pipeline converts downloaded PDFs into section-level markdown that agents can read piece by piece, avoiding token blowouts from dumping entire papers into context.

The pipeline is **converter-agnostic**. Set `PDF_CONVERTER` in `.env` to choose your backend:

```bash
# Named backends
PDF_CONVERTER=mineru          # default — https://github.com/opendatalab/MinerU
PDF_CONVERTER=marker          # https://github.com/datalab-to/marker

# Custom command template. Placeholders: {input} (the PDF), {output_dir}
# (where to write markdown), {python} (this server's own interpreter).
# Use them BARE — the values are substituted already shell-quoted, so
# wrapping one in quotes yourself breaks every path with a space in it.
PDF_CONVERTER=my-tool --in {input} --out {output_dir}
PDF_CONVERTER={python} -m my_tool {input} {output_dir}
```

If your converter lives in a virtualenv, set `PDF_CONVERTER_VENV`:

```bash
PDF_CONVERTER_VENV=~/.venvs/mineru
```

The converter must accept a PDF input path and an output directory, and produce one or more `.md` files in that directory. The pipeline picks one deterministically: a file named after the PDF beats any other `.md`, and within each of those two passes the shallowest path wins, then alphabetical order. (MinerU's `<stem>/{auto,ocr,txt}/<stem>.md` layout therefore resolves to `auto`.)

`PDF_FAST_CONVERTER` takes the same `{input}` / `{python}` placeholders, under the same bare-placeholder rule. It has no `{output_dir}`: a fast backend must write the extracted text to **stdout** and its diagnostics to stderr.

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
  openalex/pmids/          # PMID -> DOI mappings (JSON)
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
  <namespace>/downloads/_neg/  # Definitive PDF-download failures (TTL below)
  oa_download/downloads/   # Open-access fetch bookkeeping (the PDF itself is
                           #   filed under the routed provider's namespace)
  manual/pdfs/             # Manually imported PDFs
  manual/markdown/         # Converted markdown
  manual/sections/         # Section indices (JSON)
  __search_index__/        # SQLite FTS5 index behind search_cached_papers
```

Cache keys are SHA-256 hashes of canonical identifiers. Writes are atomic (temp file + `os.replace`) so a crash mid-write can't leave a corrupt entry; corrupt entries from earlier versions self-heal on read.

**Positive entries expire per provider**, so a long session doesn't serve stale data:

| Provider | Positive TTL | Negative TTL | Why |
|----------|--------------|--------------|-----|
| arxiv | 14d | 1h | New versions land under a new key; preprint IDs go live mid-session. |
| biorxiv | 7d | 1h | `published_doi` appears asynchronously once a preprint is published. |
| openalex (works, authors, pmids) | 30d | 24h | Citation counts, topics, h-index all drift. |
| crossref | 30d | 24h | Reference lists grow as publishers re-deposit metadata. |
| opencitations | 7d | 24h | The citation graph grows continuously. |
| wikipedia | 30d | 24h | Articles change as they're edited. |

**PDF downloads** negative-cache definitive failures too, under a `downloads` entity in each provider's namespace: arxiv / biorxiv 1h (they render PDFs lazily, so a just-announced paper's PDF can 404 for minutes), acl_anthology and the open-access path 24h (static files — a 404 means a wrong ID or a closed-access paper). The PDF itself never expires.

Eviction is mtime-based and self-healing: an over-age entry is unlinked on read. **Negative entries** (definitive 404s) live in a sibling `_neg/` subdirectory so a known-bad identifier doesn't burn rate budget on every retry, while a newly-registered DOI still surfaces within the TTL. `force_refresh=True` drops both halves and re-fetches.

Downloaded PDFs, converted markdown, and section indices have **no** expiry — they're derived from bytes that don't change. `.cache/` therefore grows without bound; prune it by hand if it gets large.

## Development

```bash
uv sync                          # Install dependencies
uv run pytest -v                 # Run all tests
uv run pytest tests/test_bibtex.py -v   # Run one test file
uv run pytest -k "test_particle" -v     # Run tests matching a pattern

# Coverage, as CI runs it. CI gates at 90%; the flags are not in `addopts`
# because a single-file run would then fail the floor.
uv run pytest -q --cov=academic_tools_mcp --cov-report=term-missing --cov-fail-under=90
```

## Architecture

```
server.py            thin entry: re-exports mcp + tools, registers the
  │                  optional debug tool
  │
  ├── app.py           FastMCP instance, lifespan, shared Annotated param types
  ├── manual.py        local-file import + identifier dispatch
  ├── corpus.py        BM25 over cached markdown (SQLite FTS5)
  ├── bibtex.py        BibTeX generation
  ├── fast_extract.py  bundled pymupdf text extractor (a `python -m` target)
  │
  ├── tools/         21 @mcp.tool functions, split by job
  │                    paper.py     metadata / authors / abstract / bibtex
  │                    pipeline.py  download → convert → sections → section
  │                    graph.py     references and citations
  │                    search.py    arXiv / Crossref / Wikipedia / local corpus
  │
  ├── providers/     seven API clients, all the same shape
  │                    openalex.py  arxiv.py     biorxiv.py   crossref.py
  │                    opencitations.py  wikipedia.py  acl.py
  │
  ├── papers/        PDF → markdown → sections
  │                    sections.py  markdown structure + search
  │                    index.py     the section index + its lock
  │                    convert.py   converter subprocess + gate
  │
  ├── download/      getting PDF bytes onto disk
  │                    streaming.py   streaming download, size cap, cached-download
  │                    openaccess.py  gated open-access fetch for generic DOIs
  │
  ├── net/           the outbound network edge
  │                    http.py      retry honouring Retry-After, structured errors
  │                    throttle.py  burst cap → concurrency cap → inter-start gap
  │                    clients.py   per-provider pooled httpx.AsyncClient
  │                    stats.py     per-provider counters, DEBUG_REQUESTS logging
  │
  ├── store/         the on-disk cache (every API client routes through this)
  │                    cache.py         atomic file cache, TTLs, negative cache
  │                    atomic.py        temp-file + os.replace, the one write seam
  │                    singleflight.py  same-key callers coalesce to one fetch
  │                    stems.py         artifact naming — one sanitizer, every path
  │
  └── util/          leaf helpers — every module here imports nothing else
                       config.py     .env + environment resolution
                       doinorm.py    DOI normalization — one home, every caller
                       textnorm.py   diacritic folding + maps back to original offsets
                       useragent.py  the outbound User-Agent — one home, every client
```

**Key design decisions:**

- **Lean responses.** Tools return only what's needed — not the full API response. An agent calling `get_paper_authors` doesn't get flooded with unrelated metadata.
- **One tool per job, auto-routed.** The four core paper tools (`get_paper_metadata`, `get_paper_authors`, `get_paper_abstract`, `get_paper_bibtex`) dispatch on identifier shape rather than forcing the agent to pick between arXiv/bioRxiv/OpenAlex families. Provider-native fields are preserved and tagged with `_source`.
- **Batch where it matters.** `get_papers_metadata` collapses N parallel singletons into batched `/works?filter=doi:...|...` calls over the OpenAlex DOIs that actually miss the cache, plus concurrent fan-out for arXiv / bioRxiv — designed for reference-graph enrichment.
- **One API hit per entity.** All tools for a given DOI share one cached response. Concurrent same-key callers are coalesced by single-flight to one fetch.
- **Per-provider concurrency, and per-host pacing where the host isn't fixed.** Each provider has its own concurrency cap (arxiv=1, per its single-connection rule; openalex=4; crossref resolved from config, see below) — multiple GETs run in flight up to the cap while a brief gap-lock enforces inter-start spacing. The open-access download path is the one client whose URLs are publisher CDNs rather than a single API, so it paces at 1 request/second **per host**: a reference walk through one journal resolves many DOIs to the same domain, and a global gap would either under-pace that or needlessly throttle unrelated publishers.
- **Persistent connections, transparent retries.** Each provider holds one pooled `httpx.AsyncClient` so TCP+TLS handshakes are reused. Transient failures (5xx, 429, timeouts, network errors) get one in-process retry honouring `Retry-After` in either form RFC 9110 permits — delay-seconds or HTTP-date — capped at 10 minutes, before surfacing to the agent. arXiv retries twice instead of once: its edge returns 429/503 with no `Retry-After`, and a single retry tends to land in the same cooldown.
- **Burst caps with structured backpressure.** Each provider refuses to stack more than 5 concurrent callers behind its rate-limit gap. The 6th gets `{error, retryable: True, backpressure: True}` immediately so the agent learns to slow down rather than waiting silently.
- **Transient failures are flagged, not just described.** Every 429, 5xx, timeout and network error carries `retryable: True`, and `retry_after_seconds` whenever the server advertises `Retry-After` (either RFC 9110 form), so an agent can branch and pick a wait interval without parsing the message string.
- **Negative caching for definitive 404s.** Known-bad identifiers are cached (24h; 1h for arXiv/bioRxiv) so retries don't burn rate budget — for PDF downloads as well as metadata, so a paper whose PDF 404s doesn't re-hit the upstream on every call. Transient errors are **not** cached: the classifier is an allowlist keyed on an explicit `retryable: False`, because an unclassified failure (a paywalled 403, say) carries no flag either way and a "not marked retryable" test would cache it for the full TTL.
- **The rate we take follows the identity we send.** Crossref publishes two service tiers; the client picks its limits from whether `CROSSREF_MAILTO` is configured rather than assuming the polite tier. Every provider sends a descriptive `User-Agent` naming the project and its repository, with a contact address appended when one is set.
- **Streaming PDF downloads with size guard.** PDFs stream chunked to a temp file with atomic rename — peak memory = 64 KiB, not 2× the PDF — and abort mid-stream if `MAX_PDF_BYTES` (default 200 MB) is exceeded. Force-refresh cascades: re-downloading drops the cached markdown + sections so the next conversion picks up the new bytes.
- **Single-conversion lock for PDFs.** At most one PDF→markdown subprocess runs at a time across the whole server; concurrent callers get a `busy` error with what's running and how long it's been going.
- **Count-then-page for large data.** Citation and reference tools expose a `_count` tool so agents can check sizes before fetching. `get_paper_references(source="auto")` does the survey for you, biased toward Crossref's richer per-entry metadata.
- **Provider-aware routing.** Manual imports auto-detect identifier types and store in the correct provider's cache, preventing duplicates.
- **Subprocess isolation for PDF converters.** The PDF pipeline shells out to external tools rather than importing them, keeping the dependency tree light and avoiding license entanglement.
- **Pre-computed aggregates.** List responses include counts (`author_count`, `topic_count`, `total_sections`, etc.) so agents don't need follow-up calls to check sizes.
- **Structured error hints.** Error responses carry a `suggestion` field with recovery guidance (e.g. which search tool to try). One exception: an identifier no provider claims carries its guidance in the `error` string instead, since there is no provider failure to enrich.

## Versioning

This project uses **calendar versioning**: each release is named for the day it
was cut — `YYYY.MM.DD`, tagged `vYYYY.MM.DD` in git (a rare same-day re-release
takes a `.postN` suffix). See [CHANGELOG.md](CHANGELOG.md) for the release
history.

## License

MIT — see [LICENSE](LICENSE).
