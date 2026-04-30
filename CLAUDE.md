# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A FastMCP-based MCP server that wraps the OpenAlex, arXiv, bioRxiv/medRxiv, Crossref, OpenCitations, and Wikipedia APIs to provide lean, focused tools for LLM agents working with academic papers. Designed for verifying paper metadata, authors, institutions, generating BibTeX citations, and exploring reference/citation graphs — primarily in support of a Hugo-based academic notes/blog workflow.

## Commands

```bash
uv sync                          # Install dependencies
uv run pytest -v                 # Run all tests
uv run pytest tests/test_bibtex.py -v                    # Run one test file
uv run pytest tests/test_bibtex.py::TestGenerateKey -v   # Run one test class
uv run pytest -k "test_particle" -v                      # Run tests matching a pattern
uv run python -m academic_tools_mcp.server               # Run the MCP server
```

## Architecture

**Layered design — tools never hit the API directly. Every API client uses every shared module.**

```
server.py (MCP tools, FastMCP lifespan)
  │
  ├── API clients (six, all share the same shape — see below)
  │     openalex.py / arxiv.py / biorxiv.py
  │     crossref.py / opencitations.py / wikipedia.py
  │
  ├── PDF + content modules
  │     acl_anthology.py    (static PDF URLs, no API)
  │     manual.py           (local file import + identifier dispatchers)
  │     papers.py           (PDF → markdown → sections, global convert lock)
  │     bibtex.py           (BibTeX generation)
  │     cache_search.py     (BM25 keyword search across cached markdown)
  │
  └── Shared infrastructure (every API client routes through these)
        _http.py            (retry helper, error normalization, backpressure error)
        _clients.py         (pooled httpx.AsyncClient per provider)
        _singleflight.py    (request coalescing)
        cache.py            (positive + negative file cache, atomic writes, TTL eviction)
        _stats.py           (per-provider counters + DEBUG_REQUESTS logging)
        config.py           (env vars)
```

**Shared infrastructure (no upstream API of its own):**

- **`cache.py`** — Generic file-based JSON cache under `.cache/<provider>/<entity>/`. Files are SHA-256 hashed by identifier. **Atomic writes** via `_atomic_write_json` (mkstemp + `os.replace`) — a crashed/killed process can leak a stray `.tmp` file but cannot leave a half-written canonical entry. **Self-healing reads**: corrupt JSON / OS errors / Unicode errors are caught, the bad file is unlinked, and `get` returns `None`. **Negative cache** (`get_negative` / `put_negative`) lives in a sibling `_neg/` subdirectory under each entity. Default 24h TTL on negatives, but arXiv and bioRxiv override to 1h via per-module `_NEG_TTL_SECONDS` because preprint identifiers go live mid-session and a stale 404 there is more harmful than on a stable journal record. Expired/corrupt/missing-`_expires_at` entries self-heal on read. **Positive cache TTL**: `cache.get(..., max_age_seconds=N)` evicts entries older than N seconds (by file mtime) so an agent re-reading a paper after a long session sees fresh data. Per-provider TTLs are constants in each module: arxiv=14d, biorxiv=7d, openalex=30d, crossref=30d, opencitations=7d, wikipedia=30d. **`cache.invalidate(namespace, entity, identifier)`** drops both halves at once — used by `force_refresh=True` on the unified paper tools to drop a stale entry and re-fetch on demand. **Orphan `.tmp` sweep**: `cache.gc_orphan_tmp_files()` walks `.cache/` for `*.tmp` files older than 1h and unlinks them — these accumulate when `_atomic_write_json` is interrupted between mkstemp and os.replace. Called from the FastMCP lifespan startup so each restart cleans up after the previous run's untimely deaths; never touches files newer than the cutoff so it can't race a live writer. Cache contents are agnostic to provider: scales to new providers by namespace.
- **`config.py`** — Loads `.env` from project root. All API credentials come from environment variables, never from tool parameters.
- **`_clients.py`** — Per-provider lazy-singleton `httpx.AsyncClient` pool. Each provider gets one long-lived client (with `httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=30s)`) so TCP+TLS handshakes get reused across all calls in that namespace. Headers (e.g. polite-pool User-Agent) are baked in at construction. `aclose_all()` is wired to FastMCP's lifespan in `server.py` so sockets close on shutdown; each per-client `aclose` is bounded by `_ACLOSE_TIMEOUT_SECONDS=5.0` so a wedged socket on one provider can't pin the lifespan or block the others from closing.
- **`_singleflight.py`** — `SingleFlight` class. `do(key, factory)` collapses N concurrent calls for the same key into one execution; followers `await` the same future and share the leader's result (success or failure). After resolution the slot is dropped, so the next call re-runs the factory — failure is not cached. Each provider holds its own `_single_flight` instance.
- **`_http.py`** — Shared HTTP utilities used by every API client. Exposes:
  - `HTTPX_ERRORS` — tuple of `httpx.HTTPStatusError`, `TimeoutException`, `RequestError`, plus our own `LocalBackpressureError`. Every client wraps its request block in `try/except _http.HTTPX_ERRORS` so all transient failures route through the same error path.
  - `error_dict(provider, exc)` — converts those exceptions into structured `{error, retry_after_seconds?, retryable?, backpressure?}` dicts with provider-aware messages.
  - `LocalBackpressureError(provider, pending, max_pending, min_gap_seconds=0.0)` — raised when the per-provider `_throttled_get` sees `_pending >= _MAX_PENDING` (default 5). Surfaces to agents as `{error, retryable: True, backpressure: True, max_concurrency, retry_after_seconds?}` with a concrete remediation in the message ("wait ≥Xs before retrying or reduce concurrency to ≤N parallel calls"). `retry_after_seconds` only appears when the provider has a documented gap (omitted for ACL Anthology where `_MIN_REQUEST_GAP=0`). Structured fields exist so agents can branch programmatically without parsing the message string.
  - `get_with_retry(client, url, *, max_attempts=2, backoff_seconds=1.0, provider=None, **kwargs)` — issues a GET with one transparent retry on transient failure (timeouts, network errors, 408/425/429, 5xx). On 429/503 honours `Retry-After` capped at `backoff_seconds * 30`. The actual sleep is `min(max(retry_after, backoff_seconds), cap)` so the provider's own throttle gap is the floor. When `provider` is passed, retries are recorded in `_stats` under that namespace.

- **`_stats.py`** — Per-provider counters (`cache_hits`, `cache_misses`, `negative_hits`, `http_calls`, `http_retries`, `backpressure_refusals`) plus a live `in_flight` sample drawn from each provider module's `_pending` global. `_stats.snapshot()` returns the whole picture as `{providers: {arxiv: {...}, openalex: {...}, ...}}`; `_stats.reset()` zeroes the cumulative counters (used by the test fixture). **Not exposed as an MCP tool** — operational data is for the operator, not the agent. Wired into `cache.get`/`get_negative` and every provider's `_throttled_get`. **DEBUG_REQUESTS**: setting the env var to `1` / `true` / `yes` / `on` makes each throttled GET log `[academic-tools] {provider} GET {url} (throttle wait Xs)` to **stderr** (not stdout — MCP servers speak JSON-RPC there). Re-read on every call so an operator can flip the flag without restarting.

**Per-provider clients (all share the same shape):** persistent `httpx.AsyncClient` from `_clients.get_client(NAMESPACE, ...)`; `_throttled_get` enforcing `_MIN_REQUEST_GAP`, `_MAX_PENDING=5` burst cap, and routing through `_http.get_with_retry` with `backoff_seconds=max(_MIN_REQUEST_GAP, 1.0)` so per-provider rate-limit policies apply to retries too; module-level `_single_flight` keyed by canonical identifier (sometimes tuple-keyed, e.g. `("references", canonical)` so different sub-fetches for the same DOI run independently); cache lookup re-checked inside the single-flight slot to catch a leader's just-written cache entry; negative cache check both before and inside the slot; on definitive 404 the error dict is written to the negative cache before being returned.

- **`openalex.py`** — Thin async client for OpenAlex singleton endpoints (`/works/{id}`, `/authors/{id}`). Handles ID normalization (DOI formats, OpenAlex URLs, ORCIDs) and cache read/write. Each entity type has a `_normalize_*` and `_canonical_*` pair. Rate-limited at ~10 req/sec (100ms gap) — previously had no rate limiter at all. `_get_client()` bakes in the polite-pool `User-Agent` from `OPENALEX_MAILTO`. Cache namespaces: `openalex/works`, `openalex/authors`. Single-flight keys are tuple-prefixed (`("work", canonical)`, `("author", canonical)`) so a parallel work-and-author fetch on the same paper runs as two slots.
- **`arxiv.py`** — Thin async client for arXiv's Atom API (`export.arxiv.org/api/query`). Handles ID normalization (bare IDs, URLs, version suffixes) and XML→dict parsing. Enforces arXiv's rate limit (1 request per 3 seconds, single connection) via the standard pattern. Cache namespace: `arxiv/papers`. No API key or env vars required. `get_paper`'s 404 path covers BOTH HTTP 404 and arXiv's 200-with-`api/errors` shape — both are negative-cached.
- **`biorxiv.py`** — Thin async client for the bioRxiv/medRxiv API (`api.biorxiv.org`). Handles DOI normalization (bare DOIs, URLs, site content URLs with version suffixes). Tries bioRxiv first, falls back to medRxiv. Selects the latest version from multi-version responses. Parses semicolon-separated author strings into structured dicts. Builds PDF URLs from DOI + version + server (biorxiv.org vs medrxiv.org). Rate-limited to ~2 req/sec (500ms gap) as a courtesy (no documented limit). Cache namespace: `biorxiv/papers`. The `published_doi` field links to the journal DOI when available — `server.get_paper_metadata(..., follow_published=True)` auto-chains to OpenAlex. No auth required.
- **`manual.py`** — Manual PDF/markdown import for local files, plus the two identifier dispatchers. **Provider-aware routing for PDF storage**: `_resolve_target()` detects the identifier type (arXiv ID, bioRxiv DOI, ACL DOI) and stores PDFs/markdown directly in that provider's cache namespace, so native pipeline tools find them with no duplicates. Unrecognised identifiers fall back to the `manual` namespace. **Metadata dispatch**: `_resolve_metadata_source()` returns `"arxiv" | "biorxiv" | "openalex" | None` — ACL DOIs and generic DOIs route to OpenAlex (ACL has no metadata API); unknown identifiers return `None` so tools can surface a clear error. Supports `~/` expansion for local paths. The module deliberately does **not** download arbitrary URLs — agents fetch non-native PDFs themselves (browser, curl, institutional proxy) and hand the local file to `import_paper`. No API, no auth, no rate limits.
- **`wikipedia.py`** — Thin async client for the Wikipedia API. Uses MediaWiki OpenSearch (`/w/api.php?action=opensearch`) for title search and the Wikimedia REST API (`/api/rest_v1/page/summary/{title}`) for page summaries and existence verification. Detects disambiguation pages via the `type` field. Rate-limited to ~1 req/sec (1,000ms gap) per Wikimedia's reader tier guidance. `_get_client()` bakes in the `User-Agent` header (with mailto from `WIKIPEDIA_MAILTO` env var). Cache namespace: `wikipedia/summaries`. No auth required.
- **`papers.py`** — Converter-agnostic PDF-to-markdown pipeline and section-level access. `_build_converter_command()` reads `PDF_CONVERTER` (named backend or custom command template) and `PDF_CONVERTER_VENV` (optional venv to activate) from env. Built-in backends: `mineru` (default), `marker`. `_resolve_convert_timeout()` reads `PDF_CONVERT_TIMEOUT` (default 1800s = 30 min; `none`/`off`/`disabled`/`0` disables; garbage falls back to default). `convert_pdf()` shells out to the configured converter under a **global single-conversion lock** (`_global_convert_lock`): at most one PDF→markdown subprocess runs across the whole server at a time, and a second concurrent caller gets `{busy: True, retryable: True, in_progress: {namespace, canonical, elapsed_seconds}, pdf_size_mb}` immediately rather than queueing. Spawned with `start_new_session=True` so a timeout can `os.killpg(SIGKILL)` the whole process tree (the converter, not just the bash wrapper). Stores markdown under `.cache/<namespace>/markdown/`. `parse_sections()` splits by H2 headings with H3 previews (adaptive — detects H1 vs H2 documents). `get_section_content()` retrieves individual sections by index or title substring. Section indices cached under `.cache/<namespace>/sections/`. **Per-paper sections lock** (`_section_locks` keyed by `(namespace, canonical)`) serialises concurrent re-parse attempts on the same paper so they don't race or both burn CPU. The lock dict is an `OrderedDict` capped at `_SECTION_LOCKS_MAX=1024` with FIFO eviction — long-running sessions touching thousands of papers don't accumulate Locks forever, and currently-held locks are skipped on eviction so mutual exclusion can't be silently dropped out from under a writer.
- **`cache_search.py`** — BM25 keyword search across every cached markdown file (`.cache/<namespace>/markdown/*.md`). Standard Robertson BM25 (k1=1.5, b=0.75); tokeniser preserves intra-word hyphens / dots so `self-attention` and `BM25` survive intact, drops a tiny stopword list (the classic 50, minus content-bearing terms like "all", "no", "not", "very" that matter in academic prose). For each top hit, returns the document title (first H1/H2), a ~200-char snippet centred on the position with the most distinct query terms cooccurring nearby, and the H2 section the snippet falls under so an agent can chain into `get_paper_section(canonical_id, section)`. Filename → canonical-ID inversion is per-namespace (`10.1101_X` → `10.1101/X`, `10.18653_v1_X` → `10.18653/v1/X`, `hep-th_9901001` → `hep-th/9901001`); arXiv new-style IDs and manual identifiers pass through unchanged. No persistent index — the corpus is small enough (tens to hundreds of papers for a personal MCP) that a fresh scan + tokenise + score on every call runs in well under 100ms and avoids any index-staleness concerns. Wrapped in `asyncio.to_thread` at the tool layer so a large-corpus search doesn't pin the event loop on concurrent HTTP fetches. Exposes one MCP tool: `search_cached_papers(query, top_k=10, namespace=None)`. Pure keyword recall — won't surface "scaled dot-product attention" for a "self-attention" query — so the natural follow-up if recall ever bites is sentence-transformer embedding rerank, but BM25 is the right starting primitive for now.
- **`crossref.py`** — Thin async client for the Crossref REST API (`api.crossref.org/works/{doi}`). Handles DOI normalization and cache read/write. `_get_client()` bakes in the polite-pool `User-Agent` header with `mailto` (from `CROSSREF_MAILTO` env var). Rate-limited to ~10 req/sec (100ms gap). Cache namespace: `crossref/works`. The full work object is cached; the tool layer slices out just the `reference` list with pagination. **Search opportunistically warms the works cache**: each `search_works` hit with a DOI is written to `crossref/works/<canonical>` (only if not already present, so a richer pre-existing entry isn't clobbered). A subsequent `get_work(doi)` is a free cache hit.
- **`opencitations.py`** — Thin async client for the OpenCitations Index API v2 (`api.opencitations.net/index/v2`). Fetches outgoing references (`/references/doi:...`) and incoming citations (`/citations/doi:...`). Rate-limited to ~3 req/sec (334ms gap, 180 req/min) per OpenCitations policy. Parses space-delimited multi-ID strings (`omid:... doi:... openalex:... pmid:...`) into structured dicts via `_parse_ids()`. Cache namespaces: `opencitations/references`, `opencitations/citations`. Single-flight keys are tuple-prefixed (`("references", canonical)` vs `("citations", canonical)`) so simultaneously fetching both directions of the citation graph for one paper runs as two slots, not one. No auth required.
- **`acl_anthology.py`** — PDF source for ACL Anthology papers. Resolves DOIs with the ACL prefix (`10.18653/v1/`) to Anthology IDs by stripping the prefix. Downloads camera-ready PDFs from `https://aclanthology.org/{id}.pdf`. No API, no auth, no documented rate limit — but routes through the same canonical pooled-client + retry + burst-cap shape as every other provider (`_MIN_REQUEST_GAP=0.0`, `_MAX_PENDING=5`, single-flight on the canonical DOI so racing callers for the same PDF coalesce). Cache namespace: `acl_anthology/pdfs`. PDF download timeout is 60s. Tools feed into the same `papers.py` pipeline as arXiv PDFs.
- **`bibtex.py`** — Generates BibTeX entries for three provider shapes. `generate_bibtex()` takes a raw OpenAlex work object and maps `type` → BibTeX entry type via `_TYPE_MAP`. `generate_arxiv_bibtex()` produces `@misc` (preprint) or `@article` (published) with `eprint`/`archiveprefix`/`primaryclass` fields. `generate_biorxiv_bibtex()` produces `@article` when `published_doi` is set, else `@misc` with the preprint DOI, server name, and `howpublished` URL. All three share helpers for surname particles (`van`, `de la`, `von`, etc.) in citation keys and author formatting.
- **`server.py`** — FastMCP tool definitions (19 live tools) plus the `_lifespan` async context manager that closes pooled clients via `_clients.aclose_all()` on shutdown. Each tool fetches the full cached object then returns only the relevant slice. Tools use `Annotated` types (`DOI`, `AUTHOR_ID`, `PAPER_ID`) for parameter descriptions. **Unified paper family** (`get_paper_metadata`, `get_paper_authors`, `get_paper_abstract`, `get_paper_bibtex`) accepts any `PAPER_ID` and dispatches via `manual._resolve_metadata_source()` to arXiv, bioRxiv, or OpenAlex. Every successful response carries `"_source"` (which provider served it) and `"_canonical_id"` (the provider's normalized form of the input — version-stripped lowercased arXiv ID, lowercased bare DOI, etc.) so agents can branch on provider-specific fields and reuse the canonical form across subsequent tool calls without re-normalising whatever the user typed. There is no lowest-common-denominator normalisation. All four also accept **`force_refresh: bool = False`** — when True, drops both positive and negative cache entries for the canonical identifier and re-fetches; useful for stale citation counts, a bioRxiv preprint that just got published, or retrying an identifier that previously 404'd. `get_paper_metadata` additionally accepts `follow_published: bool = False` — when `True` and a bioRxiv paper has a `published_doi`, the tool auto-chains to `openalex.get_work(published_doi, force_refresh=force_refresh)` and returns the journal record with `_source: "openalex_via_biorxiv"`, `_canonical_id` set to the journal DOI, plus a `preprint_doi` field; falls back to the preprint record if OpenAlex misses. **Unified PDF pipeline** (`download_pdf` → `convert_paper` → `get_paper_sections` → `get_paper_section`) auto-detects the provider via `manual._resolve_target()` and routes to the correct cache namespace — works for arXiv IDs, ACL DOIs, bioRxiv DOIs, and manually imported papers. The first three also accept **`force_refresh: bool = False`** with stage-specific semantics: on `download_pdf` it unlinks the cached PDF and re-downloads (use when the cached file is corrupt or the provider replaced it under the same canonical key); on `convert_paper` it drops both the cached markdown and the section index so the converter subprocess re-runs (use after replacing the source PDF or upgrading the converter); on `get_paper_sections` it drops just the section index so the next read re-parses the markdown. `get_paper_section` reads the markdown file directly (no derived cache) so it has no `force_refresh` parameter. `convert_paper` surfaces three error shapes: `{error, retryable: False}` for permanent failures (missing PDF, converter crash); `{error, retryable: False, timed_out: True, timeout_seconds, pdf_size_mb}` on the `PDF_CONVERT_TIMEOUT`; `{error, retryable: True, busy: True, in_progress: {...}}` when another conversion is already in flight. `get_paper_section` is paginated by character offset: `offset` (default 0) + `max_chars` (default 16000, hard cap 200000). Every response carries `total_chars`, `chars_returned`, `has_more`, and `next_offset` so agents read long sections by re-calling with `offset=next_offset` rather than asking for an unbounded slice. The tool also carries `anthropic/maxResultSizeChars=200000` meta so Claude Code doesn't persist large results to disk. The PDF pipeline tools (`download_pdf`, `convert_paper`, `import_paper`) deliberately strip cache filesystem paths from their responses at the MCP boundary so agents drive the pipeline by identifier through the tools rather than reading files directly. Manual import is a single tool `import_paper(file_path, identifier)` that auto-detects `.pdf` vs `.md`/`.markdown` by extension and routes accordingly. Wikipedia tools (`search_wikipedia`, `get_wikipedia_summary`) support cross-referencing workflows. **Reference/citation graph tools**: `get_paper_references_count` surveys both Crossref and OpenCitations in parallel and returns per-source counts; `get_paper_references(doi, source, page, page_size)` defaults `source="auto"` — auto fires both providers in parallel via `asyncio.gather`, picks whichever has more references (tie goes to Crossref for richer per-entry metadata), and falls back to the surviving source if one errors; both errors → response carries both error messages. Explicit `source="crossref"` or `source="opencitations"` skips the survey (important for paginating page=2..N). `get_paper_citations_count` / `get_paper_citations` cover incoming citations (OpenCitations-only, no source param).

**Key design decisions:**
- Tool responses are intentionally small — an LLM agent should not receive the full OpenAlex response. Each tool returns only what's needed for its purpose.
- All tools for a given DOI or arXiv ID share one cached API response. Multiple tool calls = one API hit. Concurrent same-key callers are coalesced by single-flight to one outbound fetch.
- **Robustness primitives apply uniformly across providers.** Every API client (arxiv, openalex, biorxiv, crossref, opencitations, wikipedia, acl_anthology) has the same shape: persistent `httpx.AsyncClient`, throttle gap + 5-deep burst cap (`LocalBackpressureError` past that), single-flight by canonical identifier, one transparent retry on transient failure honouring `Retry-After`, negative caching on definitive 404s (default 24h TTL; arxiv/biorxiv override to 1h because preprint identifiers go live mid-session), positive cache TTL eviction (per-provider), and `_stats` counters for observability. New providers should follow this pattern (see "Adding a New API Provider").
- **One paper tool per job, not one per provider.** Rather than `get_paper_*` / `get_arxiv_paper_*` / `get_biorxiv_paper_*` families, the four core paper tools take any identifier and dispatch internally. Responses are tagged with `_source` ("arxiv" / "biorxiv" / "openalex") so agents can handle provider-specific fields without pre-guessing which family to call. Dispatch is by identifier shape, not by which provider has more data — `get_paper_metadata("2301.00001")` returns arXiv's native response even if the paper is also in OpenAlex. Agents that want OpenAlex-specific data (topics, citations, venue) call the dedicated OpenAlex-only tools with the paper's DOI.
- OpenAlex singleton lookups (works, authors) are free and unlimited. Search/filter endpoints have daily limits and are not currently implemented.
- arXiv lookups are free but rate-limited (3-second gap enforced in code, plus the burst cap). Search is supported with a 50-result cap per call. `search_arxiv` returns `{total_results, results: [...]}` (matching `search_crossref_by_title` so an agent can branch by source without learning per-tool field names); each hit is the slim triage shape `{arxiv_id, title, first_author, author_count, published_year}` — full-author lists balloon to tens of KB on HEP/biology papers, so the search tool drops everything beyond what's needed for triage. `author_count` lets the agent decide whether to call `get_paper_authors` directly or paginate. Each entry is opportunistically cached, so a follow-up `get_paper_metadata(arxiv_id)` is free.
- The OpenAlex-shaped `get_paper_authors` response includes `openalex_id` per author so agents can chain into `get_author`. arXiv and bioRxiv responses do not carry this because those APIs don't expose author IDs.
- `get_paper_authors` is paginated (`page`, `page_size`, default 25, cap 25) to bound response size on large-collaboration papers (HEP, biology consortia) that can carry thousands of authors. Every response includes `author_count` (global total), `has_more`, and the current page. Since the upstream paper response is cached per canonical identifier, paging is pure in-memory slicing — zero extra API cost. The institution roll-up (`page_institutions` / `page_institution_count`) appears on every branch — populated on OpenAlex (derived from the current page only so the cap holds; agents needing a global list dedupe across pages), empty on arxiv/biorxiv (those upstream APIs don't carry per-author institution rollups). The shape stays symmetric so paginating agents don't have to feature-detect.
- **Reference/citation graph tools.** `get_paper_references` defaults to `source="auto"`, which fires both Crossref and OpenCitations in parallel (`asyncio.gather`), picks whichever has more references (tie → Crossref for richer per-entry metadata), and serves from the surviving source if one errors. Both errors → returns combined error dict. Explicit `source="crossref"` or `source="opencitations"` skips the survey — important so paginating page=2..N doesn't re-survey. `get_paper_references_count` remains for agents that want to compare coverage explicitly. `get_paper_citations_count` / `get_paper_citations` cover incoming citations (OpenCitations only today — `get_paper_citations` accepts `source: Literal["auto", "opencitations"] = "auto"` so a future second source can ship without a breaking change; both values dispatch identically today, the response carries `_source: "opencitations"` either way).
- Crossref provides structured reference metadata (author, title, year, journal, DOI) when publishers deposit it; quality varies. OpenCitations aggregates from Crossref/PubMed/DataCite/OpenAIRE/JaLC and returns DOI-to-DOI links with cross-referenced IDs (OMID, OpenAlex, PMID) and self-citation flags — broader coverage, no bibliographic metadata.
- `search_crossref_by_title` enables DOI discovery by bibliographic query — useful when you only have a title or arXiv ID and need the published DOI (e.g., to find the ACL Anthology DOI for a paper known only by its arXiv ID). Year filtering is optional but note that Crossref publication dates may differ from arXiv preprint dates. This also serves as the de facto search for bioRxiv papers, since the bioRxiv API has no title search endpoint — Crossref indexes all bioRxiv DOIs. Hits return a slim triage shape `{doi, title, first_author, author_count, year}` (parallel to `search_arxiv`). **Each hit also opportunistically warms `crossref/works`** (without clobbering an existing richer entry), so chaining to `get_paper_metadata(doi)` is a free cache hit.
- **`search_cached_papers`** is the third search primitive — distinct from the two upstream search tools above. It runs BM25 over every markdown file already converted by the PDF pipeline (across all namespaces, optionally filtered to one) and ranks them against a free-text query. The use case is "I read this paper a few weeks ago, what was its identifier?" or "which of my imported PDFs talked about X?" — neither of which the upstream search APIs can answer because they don't know what's in your local cache. Especially useful for manually imported papers where the identifier is a freeform label and the only handle on the paper is its content. Returns `{namespace, canonical_id, score, title, snippet, section, char_count}` per hit; chain `get_paper_section(canonical_id, section)` to read the full section the snippet came from. Pure keyword match — won't bridge synonyms, doesn't see un-converted PDFs.
- **bioRxiv → journal chaining**: When a bioRxiv/medRxiv paper has been formally published, `get_paper_metadata(biorxiv_doi)` returns a `published_doi` field containing the journal DOI. Pass `follow_published=True` to auto-chain — the response comes back as `_source: "openalex_via_biorxiv"` with both `doi` (journal) and `preprint_doi` (the original bioRxiv DOI) so the chain stays visible. If OpenAlex doesn't have the journal record (paper too new to index), the tool falls back to the preprint metadata rather than erroring. Manual chain still works: calling `get_paper_metadata(published_doi)` routes through OpenAlex.
- **PDF subprocess gating.** `convert_paper` enforces a global single-conversion lock — at most one PDF→markdown subprocess runs across the whole server at a time. Concurrent callers get `{busy: True, retryable: True, in_progress: {namespace, canonical, elapsed_seconds}}` immediately rather than queueing. The conversion subprocess is spawned with `start_new_session=True` and bounded by `PDF_CONVERT_TIMEOUT` (default 1800s = 30 min); on timeout, `os.killpg(SIGKILL)` takes down the whole process tree (the converter, not just the bash wrapper). Already-converted papers' cached-path early-return is NOT subject to the global lock — agents can keep reading sections of converted papers while a different one is converting.
- **Manual import deduplication**: `import_paper(file_path, identifier)` auto-detects the identifier type and stores the file in the matching provider's cache namespace. For example, `import_paper("paper.pdf", "2301.00001")` writes to `.cache/arxiv/pdfs/`, and `import_paper("paper.pdf", "10.1101/2024.01.01.573838")` writes to `.cache/biorxiv/pdfs/`. This means a subsequent `download_pdf("2301.00001")` will find the cached PDF — no duplicate downloads or conversions. The unified pipeline tools (`convert_paper`, `get_paper_sections`, `get_paper_section`) also route to the correct namespace automatically. PDFs are validated by their `%PDF-` magic bytes (rejects mis-extension files before they reach the converter); markdown is read as UTF-8 with a clean error on decode failure. The MCP-layer response slims the markdown branch to `section_count` only — the agent calls `get_paper_sections` if it wants the full index.
- Manual imports intentionally have no BibTeX generation — the manual pipeline has no structured metadata. When the identifier is a DOI, chain into `get_paper_bibtex` (which dispatches to OpenAlex for arbitrary DOIs) for BibTeX instead.

## Adding a New OpenAlex Entity

1. Add `_normalize_*` and `_canonical_*` functions in `openalex.py`
2. Add an async `get_*` function that checks cache, fetches, and stores
3. Add focused tool(s) in `server.py` that extract lean slices from the cached object
4. Add unit tests for normalization logic in `tests/test_openalex.py`

## Adding a New API Provider

Follow the shape every existing client uses (see `arxiv.py` or `crossref.py` as canonical examples). Concretely:

1. Create a new module (e.g. `myprovider.py`) and import the shared infrastructure: `from . import _clients, _http, _singleflight, _stats, cache, config`.
2. Pick a distinct cache namespace string and use `cache.get(NAMESPACE, "<entity>", canonical, max_age_seconds=_POSITIVE_TTL_SECONDS)` / `cache.put(...)` / `cache.get_negative(...)` / `cache.put_negative(..., ttl_seconds=_NEG_TTL_SECONDS)` (only override the negative TTL if the data moves faster than 24h, e.g. preprint identifiers — see arxiv.py / biorxiv.py).
3. Module-level state for the throttle and TTLs:
   ```python
   _request_lock = asyncio.Lock()
   _last_request_time: float = 0.0
   _MIN_REQUEST_GAP = 0.5  # provider-appropriate
   _MAX_PENDING = 5
   _pending: int = 0
   _single_flight = _singleflight.SingleFlight()
   _POSITIVE_TTL_SECONDS = 30 * 86400.0  # provider-appropriate; pick by how fast the data drifts
   ```
4. Implement `_throttled_get` following the exact pattern in the existing clients: backpressure check (incl. `_stats.incr(NAMESPACE, "backpressure_refusals")` before raising) + increment, throttle gap inside the lock with `wait_seconds` measured for `_stats.log_request(NAMESPACE, url, wait_seconds)`, `_stats.incr(NAMESPACE, "http_calls")`, then dispatch to `_http.get_with_retry(client, url, backoff_seconds=max(_MIN_REQUEST_GAP, 1.0), provider=NAMESPACE, **kwargs)`, decrement on the way out.
5. Public `get_*` functions: cache → negative cache → `_single_flight.do(canonical, _fetch)`. Pass `max_age_seconds=_POSITIVE_TTL_SECONDS` to every `cache.get` call (both outer and inner inside `_fetch`). Inside `_fetch`, re-check both caches (a follower's coroutine resumed after the leader populated them) before going to network. On a definitive 404, build the error dict and call `cache.put_negative(...)` before returning it. If the data is mutable enough that an agent might want to bypass the cache, accept `force_refresh: bool = False` and call `cache.invalidate(NAMESPACE, "<entity>", canonical)` at the top of the function before the cache check (see `arxiv.get_paper` / `openalex.get_work` / `biorxiv.get_paper` for the pattern).
6. Use `_clients.get_client(NAMESPACE, headers=..., timeout=30.0)` for the HTTP client. Headers (e.g. polite-pool User-Agent) baked in once at client construction; per-call kwarg overrides still work via `client.get(url, timeout=...)`.
7. Add the module name to the `_reset_pooled_state` autouse fixture in `tests/conftest.py` so tests get a clean `_pending` / `_single_flight` between runs, and to the `_PROVIDER_MODULES` tuple in `_stats.py` so its in-flight count appears in `snapshot()`.
8. Add env vars to `.env.example` and load them via `config.get()`.
9. Add tools in `server.py` that call the new module.
10. Add unit tests covering normalization, parsing, the throttle's backpressure behaviour, any negative-cache 404 paths, and TTL eviction / `force_refresh` if relevant.

## Burst cap (applies to every provider)

Every provider's `_throttled_get` enforces a 5-deep burst cap behind its rate-limit gap. The 6th concurrent caller raises `LocalBackpressureError` (caught by the existing `try/except _http.HTTPX_ERRORS` and surfaced as `{error, retryable: True, backpressure: True}`) instead of silently queueing. The cap is a constant (`_MAX_PENDING = 5`) in each module — bump it per-provider if needed.

## Cache TTLs

Positive cache entries have per-provider TTLs (`_POSITIVE_TTL_SECONDS` in each module) so an agent re-reading a paper after a long session sees fresh data without a manual cache wipe:

| Provider       | Positive TTL | Negative TTL | Why |
|----------------|--------------|--------------|-----|
| arxiv          | 14d          | 1h           | New versions land under the same canonical key (version stripped); preprint IDs go live mid-session. |
| biorxiv        | 7d           | 1h           | `published_doi` appears asynchronously when the preprint becomes a journal article. |
| openalex/works | 30d          | 24h          | Citation count and topic classifications drift; DOIs are stable. |
| openalex/authors | 30d        | 24h          | h_index / works_count drift on the same timescale. |
| crossref       | 30d          | 24h          | Reference list grows as publishers re-deposit metadata. |
| opencitations  | 7d           | 24h          | Citation graph (especially incoming) grows continuously. |
| wikipedia      | 30d          | 24h          | Articles change as edits are made. |

Eviction is mtime-based and self-healing — `cache.get(..., max_age_seconds=N)` unlinks an over-age entry and returns `None`. **`force_refresh=True`** on the four unified paper tools (`get_paper_metadata`, `get_paper_authors`, `get_paper_abstract`, `get_paper_bibtex`) drops both halves via `cache.invalidate(...)` and re-fetches — useful for stale citation counts, retrying a previously-404'd identifier, or chasing a freshly-published preprint.

## Observability

`_stats.py` collects per-provider counters in-process. `_stats.snapshot()` returns:

```python
{
  "providers": {
    "arxiv": {
      "cache_hits": 42,
      "cache_misses": 5,
      "negative_hits": 1,
      "http_calls": 5,
      "http_retries": 0,
      "backpressure_refusals": 0,
      "in_flight": 0,
    },
    ...
  }
}
```

Counters are cumulative since process start (or the last `_stats.reset()`). `in_flight` is sampled live from each provider module's `_pending` global. **Not exposed as an MCP tool** — operational data is for the operator, not the agent. Use it from a debug script, a future internal endpoint, or a custom Python REPL session.

**`DEBUG_REQUESTS=1`** (also `true` / `yes` / `on`) makes each throttled GET log `[academic-tools] {provider} GET {url} (throttle wait Xs)` to stderr. Re-read on every call so an operator can flip the flag without restarting. Output goes to **stderr only** because MCP servers speak JSON-RPC on stdout — anything written there would corrupt the protocol stream.

**`ENABLE_DEBUG_TOOLS=1`** (also `true` / `yes` / `on`) registers a `get_server_stats` MCP tool that returns `_stats.snapshot()`. Read at module import time, so flipping it requires a server restart. **Off by default** — agents would otherwise see operational data and might branch on it. Use this when you want to inspect counters from inside Claude Code without dropping into a Python REPL: launch the server with `ENABLE_DEBUG_TOOLS=1 uv run python -m academic_tools_mcp.server`, then call the tool from your client.

## OpenAlex API Limits

- **Singleton lookups** (get by ID/DOI/ORCID): Free, unlimited.
- **Local rate limit**: ~10 req/sec (100ms gap) enforced in `openalex.py` via `asyncio.Lock` + monotonic timer. Persistent client bakes in a polite-pool `User-Agent` from `OPENALEX_MAILTO`.
- **Search**: 1,000 calls/day — not currently used.
- **List+filter**: 10,000 calls/day — not currently used.
- **Content download**: 100/day — not currently used.

## arXiv API Limits

- **Rate limit**: Max 1 request every 3 seconds, single connection. Enforced by `asyncio.Lock` + `time.monotonic()` in `arxiv.py`. Persistent `httpx.AsyncClient` from `_clients.py` honours the "single connection" intent more cleanly than per-call socket churn would.
- **No authentication required** — no API key, no email, nothing in `.env`.
- **Search cap**: `max_results` capped at 50 per call in the MCP tool layer to keep responses lean.

## bioRxiv/medRxiv API Limits

- **Rate limit**: No documented limits. Enforced conservatively at ~2 req/sec (500ms gap) in `biorxiv.py`.
- **No authentication required** — no API key, no email, nothing in `.env`.
- **DOI prefix** `10.1101/` identifies all bioRxiv and medRxiv papers.
- **Multi-version responses**: The `/details` endpoint returns all versions; code selects the latest automatically.
- **medRxiv fallback**: If a DOI isn't found on bioRxiv, the code automatically tries medRxiv.

## Crossref API Limits

- **Polite pool** (with `CROSSREF_MAILTO`): 10 req/sec single records, 3 req/sec search/query, 3 concurrent. Enforced conservatively at ~10 req/sec (100ms gap) in `crossref.py`.
- **Public pool** (no mailto): 5 req/sec single records, 1 req/sec search/query, 1 concurrent.
- **No API key required** — just `mailto` in `User-Agent` for polite pool access (baked into the persistent client).
- **Search**: Uses `query.bibliographic` parameter on `/works` endpoint. Capped at 20 rows per request. Search results are NOT cached as a query, but every hit with a DOI opportunistically warms `crossref/works/<canonical>` (without clobbering pre-existing entries) so a follow-up `get_work(doi)` is free.

## OpenCitations API Limits

- **Rate limit**: 180 req/min per IP. Enforced at ~3 req/sec (334ms gap) in `opencitations.py`.
- **No authentication required** — no API key, no email, nothing in `.env`.

## Wikipedia API Limits

- **Rate limit**: 1,000 req/hour for identified clients (with `User-Agent`). Enforced conservatively at ~1 req/sec (1,000ms gap) in `wikipedia.py`.
- **No authentication required** — just a `User-Agent` header with `mailto` (from `WIKIPEDIA_MAILTO` env var). Requests without a `User-Agent` may be blocked. The persistent client bakes the header in.
- **Page summaries are cached** under `wikipedia/summaries` to avoid redundant lookups.

## ACL Anthology

- **No API** — PDFs are served directly at `https://aclanthology.org/{anthology_id}.pdf`. No authentication, no rate limits documented.
- **DOI prefix** `10.18653/v1/` identifies ACL Anthology papers. The Anthology ID is the DOI suffix (e.g., `10.18653/v1/2023.acl-long.1` → `2023.acl-long.1`).
- **Coverage**: All ACL-affiliated venues — ACL, EMNLP, NAACL, EACL, AACL, CoNLL, TACL, CL journal, *SEM, Findings, workshops.

## APIs NOT to Use

- **Semantic Scholar** — Do not suggest or integrate. Their API keys are not granted to individuals. The shared global pool is unreliable and practically unusable. Not a viable option.
- **Google Scholar** — No official API exists. Scraping is fragile and against ToS. Do not suggest.

## Future Possibilities

- **OpenReview** — Has an API (v1: `api.openreview.net`, v2: `api2.openreview.net`) that could provide venue/decision metadata, review scores, and forum data for ML/AI conference papers. However, after a November 2025 security incident (reviewer identity leak), all endpoints now return 403 without authentication. Would require storing `OPENREVIEW_USERNAME`/`OPENREVIEW_PASSWORD` in `.env` and managing token refresh. Revisit if they reopen public access or if the auth overhead becomes worthwhile. We already have 5+ papers with OpenReview forum IDs (e.g., `openreview_n8hGHUfZ3Sy`).
