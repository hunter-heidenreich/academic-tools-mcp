---
paths:
  - "src/academic_tools_mcp/openalex.py"
  - "src/academic_tools_mcp/arxiv.py"
  - "src/academic_tools_mcp/biorxiv.py"
  - "src/academic_tools_mcp/crossref.py"
  - "src/academic_tools_mcp/opencitations.py"
  - "src/academic_tools_mcp/wikipedia.py"
  - "src/academic_tools_mcp/acl_anthology.py"
---

# API providers

## Common shape

Every per-provider client uses the same pattern:

- Persistent `httpx.AsyncClient` from `_clients.get_client(NAMESPACE, ...)`.
- `_throttled_get` enforcing `_MIN_REQUEST_GAP`, `_MAX_PENDING=5` burst cap, routing through `_http.get_with_retry` with `backoff_seconds=max(_MIN_REQUEST_GAP, 1.0)` so per-provider rate-limit policies apply to retries too.
- Module-level `_single_flight` keyed by canonical identifier (sometimes tuple-keyed, e.g. `("references", canonical)` so different sub-fetches for the same DOI run independently).
- Cache lookup re-checked **inside** the single-flight slot to catch a leader's just-written entry.
- Negative cache check both **before** and **inside** the slot.
- On definitive 404, error dict is written to negative cache before being returned.

If the data is mutable enough that an agent might want to bypass the cache, accept `force_refresh: bool = False` and call `cache.invalidate(NAMESPACE, "<entity>", canonical)` at the top of the function before the cache check (see `arxiv.get_paper` / `openalex.get_work` / `biorxiv.get_paper`).

## openalex.py

Singleton endpoints (`/works/{id}`, `/authors/{id}`). ID normalization for DOI formats, OpenAlex URLs, ORCIDs. Each entity has `_normalize_*` + `_canonical_*` pair. Rate limit ~10 req/sec (100ms gap). `_get_client()` bakes in the polite-pool `User-Agent` from `OPENALEX_MAILTO`. Cache namespaces: `openalex/works`, `openalex/authors`. Single-flight keys tuple-prefixed (`("work", canonical)`, `("author", canonical)`) — parallel work-and-author fetch on the same paper runs as two slots.

**Limits:** singleton lookups (ID/DOI/ORCID) are free and unlimited. Search (1000/day), List+filter (10000/day), Content download (100/day) are not currently used.

## arxiv.py

arXiv Atom API (`export.arxiv.org/api/query`). ID normalization (bare IDs, URLs, version suffixes), XML→dict parsing. Rate limit: 1 req/3s, single connection (per arXiv policy). Cache namespace: `arxiv/papers`. No API key or env vars. `get_paper`'s 404 path covers BOTH HTTP 404 and arXiv's 200-with-`api/errors` shape — both negative-cached.

Search supported with `max_results` capped at 50 in the tool layer.

## biorxiv.py

bioRxiv/medRxiv API (`api.biorxiv.org`). DOI normalization (bare DOIs, URLs, site content URLs with version suffixes). Tries bioRxiv first, falls back to medRxiv. Selects latest version from multi-version responses. Parses semicolon-separated author strings. Builds PDF URLs from DOI + version + server. Rate limit ~2 req/sec (500ms gap, conservative — no documented limit). Cache namespace: `biorxiv/papers`. The `published_doi` field links to the journal DOI when available — `server.get_paper_metadata(..., follow_published=True)` auto-chains to OpenAlex. No auth.

DOI prefix `10.1101/` identifies all bioRxiv and medRxiv papers.

## crossref.py

Crossref REST API (`api.crossref.org/works/{doi}`). DOI normalization. `_get_client()` bakes in polite-pool `User-Agent` with `mailto` (from `CROSSREF_MAILTO`). Rate limit ~10 req/sec (100ms gap). Cache namespace: `crossref/works`. Full work object cached; tool layer slices out reference list with pagination.

**Search opportunistically warms the works cache** — each `search_works` hit with a DOI is written to `crossref/works/<canonical>` (only if not already present, so a richer pre-existing entry isn't clobbered). A subsequent `get_work(doi)` is a free cache hit.

**Limits:** polite pool (with `CROSSREF_MAILTO`) — 10 req/sec singles, 3 req/sec search, 3 concurrent. Public pool (no mailto) — 5 req/sec singles, 1 req/sec search, 1 concurrent. Search uses `query.bibliographic` on `/works`, capped at 20 rows.

## opencitations.py

OpenCitations Index API v2 (`api.opencitations.net/index/v2`). Outgoing references (`/references/doi:...`) and incoming citations (`/citations/doi:...`). Rate limit ~3 req/sec (334ms gap, 180/min) per OpenCitations policy. Parses space-delimited multi-ID strings (`omid:... doi:... openalex:... pmid:...`) via `_parse_ids()`. Cache namespaces: `opencitations/references`, `opencitations/citations`. Single-flight tuple-prefixed (`("references", canonical)` vs `("citations", canonical)`) — fetching both directions for one paper runs as two slots.

## wikipedia.py

MediaWiki OpenSearch (`/w/api.php?action=opensearch`) for title search; Wikimedia REST (`/api/rest_v1/page/summary/{title}`) for summaries and existence verification. Detects disambiguation pages via the `type` field. Rate limit ~1 req/sec (1000ms gap). `_get_client()` bakes in `User-Agent` (mailto from `WIKIPEDIA_MAILTO`) — requests without a `User-Agent` may be blocked. Cache namespace: `wikipedia/summaries`.

**Limit:** 1000 req/hour for identified clients.

## acl_anthology.py

PDF source for ACL Anthology papers. Resolves DOIs with prefix `10.18653/v1/` to Anthology IDs by stripping the prefix. Downloads camera-ready PDFs from `https://aclanthology.org/{id}.pdf`. No API, no auth, no documented rate limit — but routes through the same canonical pooled-client + retry + burst-cap shape as every other provider (`_MIN_REQUEST_GAP=0.0`, `_MAX_PENDING=5`, single-flight on canonical DOI). Cache namespace: `acl_anthology/pdfs`. PDF download timeout 60s. Feeds into `papers.py`.

Coverage: all ACL-affiliated venues — ACL, EMNLP, NAACL, EACL, AACL, CoNLL, TACL, CL journal, *SEM, Findings, workshops.
