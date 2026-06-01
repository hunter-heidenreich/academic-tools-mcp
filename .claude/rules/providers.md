---
paths:
  - "src/academic_tools_mcp/providers/*.py"
---

# API providers

## Common shape

Every per-provider client uses the same pattern. The two cross-cutting pieces —
*throttling* and the *cached-getter protocol* — are **shared infrastructure**, not
per-provider code: see `_throttle.Throttle` and `cache.cached_lookup` in
`.claude/rules/infrastructure.md`. A provider supplies only its *policy* and its
*quirks*.

- Persistent `httpx.AsyncClient` from `_clients.get_client(NAMESPACE, ...)`.
- A module-level `_throttle = Throttle(namespace=NAMESPACE, label=..., max_concurrent=_MAX_CONCURRENT, min_gap_seconds=_MIN_REQUEST_GAP, max_pending=_MAX_PENDING)`, exposed via thin `_throttled_get` (→ `_throttle.get`) and, in the PDF-downloading modules, `_request_slot` (→ `_throttle.slot`) wrappers. The `Throttle` does the three-layer gating + `_http.get_with_retry`; don't re-implement it.
- `_MAX_CONCURRENT` per-provider (the policy constant passed to `Throttle`): arxiv=1 (single-connection rule), openalex=4, acl_anthology=4, crossref=3 (polite-pool concurrency budget), biorxiv=2, opencitations=2, wikipedia=2.
- Module-level `_single_flight` instance, passed to `cache.cached_lookup`.
- Each getter is `canonical = canonical_*(id)` → define an async `_fetch()` closure (the HTTP + parse + `cache.put`/`put_negative` body, holding the provider's quirks) → `return await cache.cached_lookup(single_flight=_single_flight, namespace=NAMESPACE, entity="<entity>", canonical=canonical, positive_ttl=_POSITIVE_TTL_SECONDS, fetch=_fetch, force_refresh=force_refresh, sf_key=...)`. `cached_lookup` owns the force_refresh-invalidate, the outer + in-slot cache re-checks, the single-flight coalescing, and the per-caller deep copy — the getter no longer hand-rolls any of it.
- Pass a tuple `sf_key` when one canonical id has multiple sub-fetches (`("work", canonical)` / `("author", canonical)` for openalex; `("references", canonical)` / `("citations", canonical)` for opencitations); omit it to key on `canonical`.
- Inside `_fetch`: on definitive 404, write the error dict to negative cache before returning; on a transient parse failure return `_parse_error_dict()` and cache nothing.

To let an agent bypass the cache, accept `force_refresh: bool = False` and thread it into `cached_lookup` (which invalidates both halves before fetching) — see `arxiv.get_paper` / `openalex.get_work` / `biorxiv.get_paper` / `wikipedia.get_summary`.

## openalex.py

Singleton endpoints (`/works/{id}`, `/authors/{id}`). ID normalization for DOI formats, OpenAlex URLs, ORCIDs. Each entity has `_normalize_*` + `_canonical_*` pair. Rate limit ~10 req/sec (100ms gap). `_get_client()` bakes in the polite-pool `User-Agent` from `OPENALEX_MAILTO`. Cache namespaces: `openalex/works`, `openalex/authors`. Single-flight keys tuple-prefixed (`("work", canonical)`, `("author", canonical)`) — parallel work-and-author fetch on the same paper runs as two slots.

**DOI handling & hardening (parity with crossref/biorxiv).** `_normalize_doi` strips surrounding whitespace and both `https://`/`http://doi.org/` (and `doi:`) prefixes; the bare DOI is then percent-encoded into the `/works/doi:{doi}` path via `quote(..., safe="/")` so reserved chars (`#`, `?`) can't truncate the request to the wrong record. A malformed/truncated 200 body raises `json.JSONDecodeError` — caught via `_PARSE_ERRORS` → `_parse_error_dict()` (`{error, retryable: True}`, a fresh dict each call) and **not** negative-cached, so a retry re-fetches. An anomalous 200 that is non-dict or missing the entity `id` key is treated identically (never positive-cached for the TTL). Both `get_work` and `get_author` take `force_refresh`; `get_author`'s 404 carries `not_found: True` to match `get_work`. The `force_refresh` in-slot re-check can still be repopulated by a concurrent non-refresh caller (narrow race, shared with `get_work`) — accepted, not fixed.

**Limits:** singleton lookups (ID/DOI/ORCID) are free and unlimited. Search (1000/day), List+filter (10000/day), Content download (100/day) are not currently used.

**Batch fetch:** `get_works_batch(dois, *, force_refresh=False)` collapses N cache-miss DOIs into ⌈N/50⌉ HTTP calls via `/works?filter=doi:DOI1|DOI2|...`. Cached entries (positive or negative) are served without a network call. Each fetched work is written to the singleton cache, so a follow-up `get_work(doi)` is a free hit. DOIs requested but missing from the response are negative-cached the same way singleton 404s are. A DOI containing OpenAlex filter metacharacters (`|` = OR, `,` = AND) would corrupt the OR-joined filter, so it is split out and resolved individually through `get_work` (the encoded singleton path) instead. A parse failure or non-dict body on the batch GET maps every DOI in that chunk to a retryable `_parse_error_dict()` (not negative-cached); non-dict items inside `results` are skipped. Used by `server.get_papers_metadata` for reference-graph enrichment.

## arxiv.py

arXiv Atom API (`export.arxiv.org/api/query`). ID normalization (bare IDs, URLs, version suffixes; URL query strings / fragments are stripped before keying), XML→dict parsing. Rate limit: 1 req/3s, single connection (per arXiv policy). Cache namespace: `arxiv/papers`. No API key or env vars. `get_paper`'s "not found" path covers THREE definitive shapes — a 200 with no entries, arXiv's 200-with-`api/errors` entry, and a genuine HTTP 404 — all negative-cached. Transient failures (5xx / timeout / 429 / backpressure) are returned as retryable errors and **not** cached.

**XML is parsed with `defusedxml`** (`fromstring`), so an entity-expansion ("billion laughs") payload is refused rather than expanded. A parse failure — malformed/truncated body or a refused entity payload — is caught alongside the HTTP errors (`_PARSE_ERRORS = (ET.ParseError, DefusedXmlException)`) and surfaced as a retryable `{error, retryable: True}` dict; it is treated as transient (not "not found"), so it is **not** negative-cached. Both `get_paper` and `search_papers` share this contract.

Search supported with `max_results` capped at 50 in the tool layer.

## biorxiv.py

bioRxiv/medRxiv API (`api.biorxiv.org`). DOI normalization (bare DOIs, URLs, site content URLs with version suffixes). Tries bioRxiv first, falls back to medRxiv. Selects latest version from multi-version responses. Parses semicolon-separated author strings. Builds PDF URLs from DOI + version + server. Rate limit ~2 req/sec (500ms gap, conservative — no documented limit). Cache namespace: `biorxiv/papers`. The `published_doi` field links to the journal DOI when available — `server.get_paper_metadata(..., follow_published=True)` auto-chains to OpenAlex. No auth.

DOI prefix `10.1101/` identifies all bioRxiv and medRxiv papers.

## crossref.py

Crossref REST API (`api.crossref.org/works/{doi}`). DOI normalization. `_get_client()` bakes in polite-pool `User-Agent` with `mailto` (from `CROSSREF_MAILTO`). Rate limit ~10 req/sec (100ms gap). Cache namespace: `crossref/works`. Full work object cached; tool layer slices out reference list with pagination.

**Search opportunistically warms the works cache** — each `search_works` hit with a DOI is written to `crossref/works/<canonical>` (only if not already present, so a richer pre-existing entry isn't clobbered). A subsequent `get_work(doi)` is a free cache hit.

`get_work` takes `force_refresh` (mirrors `openalex.get_work`: invalidate both cache halves up front, skip the pre-slot checks) so the reference-graph tools can refresh both sources — the reference list grows as publishers re-deposit metadata.

**Limits:** polite pool (with `CROSSREF_MAILTO`) — 10 req/sec singles, 3 req/sec search, 3 concurrent. Public pool (no mailto) — 5 req/sec singles, 1 req/sec search, 1 concurrent. Search uses `query.bibliographic` on `/works`, capped at 20 rows.

## opencitations.py

OpenCitations Index API v2 (`api.opencitations.net/index/v2`). Outgoing references (`/references/doi:...`) and incoming citations (`/citations/doi:...`). Rate limit ~3 req/sec (334ms gap, 180/min) per OpenCitations policy. Parses space-delimited multi-ID strings (`omid:... doi:... openalex:... pmid:...`) via `_parse_ids()`. Cache namespaces: `opencitations/references`, `opencitations/citations`. Single-flight tuple-prefixed (`("references", canonical)` vs `("citations", canonical)`) — fetching both directions for one paper runs as two slots.

**Parsing/encoding hardening (parity with crossref/openalex).** The bare DOI is percent-encoded into the `.../doi:{doi}` path via `quote(..., safe="/")` so reserved chars (`#`, `?`) can't truncate the request to the wrong record (the `doi:` scheme prefix and the DOI's own slash stay literal). A malformed/truncated 200 body raises `json.JSONDecodeError` — caught via `_PARSE_ERRORS` → `_parse_error_dict()` (`{error, retryable: True}`, a fresh dict each call) and **not** negative-cached, so a retry re-fetches. An anomalous 200 that isn't the expected list of records (dict / null / string) is treated identically rather than crashing the `_format_record` comprehension; non-dict items inside the list are skipped. `get_references` / `get_citations` are one-line wrappers over a shared `_fetch_direction(doi, *, kind, id_field, force_refresh)` — the two directions differ only by `kind` (the API path segment, cache entity, and result key: `"references"`/`"citations"`) and `id_field` (`"cited"`/`"citing"`). Both take `force_refresh` (threaded into `cached_lookup`) — the citation graph grows continuously, so an agent may want fresher coverage than the 7-day TTL.

## wikipedia.py

MediaWiki OpenSearch (`/w/api.php?action=opensearch`) for title search; Wikimedia REST (`/api/rest_v1/page/summary/{title}`) for summaries and existence verification. Detects disambiguation pages via the `type` field. Rate limit ~1 req/sec (1000ms gap). `_get_client()` bakes in `User-Agent` (mailto from `WIKIPEDIA_MAILTO`) — requests without a `User-Agent` may be blocked. Cache namespace: `wikipedia/summaries`.

**Parsing/encoding hardening (parity with crossref/openalex/opencitations).** A malformed/truncated 200 body raises `json.JSONDecodeError` — caught via `_PARSE_ERRORS` → `_parse_error_dict()` (`{error, retryable: True}`, a fresh dict each call) and **not** negative-cached, so a retry re-fetches; `get_summary` treats a non-dict 200 body the same way rather than crashing the `data.get(...)` calls. The page title is percent-encoded into the summary path via `quote(url_title, safe="")` (the whole segment — a slash in a title like `AC/DC` is part of the title, not a path separator) so reserved chars can't split the path or truncate the request to the wrong record. **The cache key is case-sensitive beyond the first character** — Wikipedia titles only auto-capitalize the leading letter, so the canonical form is `url_title[:1].upper() + url_title[1:]`, not a full lowercase (which would collide case-distinct articles like `PET` vs `Pet`). The 404 error dict carries `not_found: True` (mirrors `openalex.get_work`); `page_exists` reports `exists: False` **only** on that definitive 404, and propagates transient errors as-is instead of treating a network blip as a confident non-existence.

**Limit:** 1000 req/hour for identified clients.

## acl_anthology.py

PDF source for ACL Anthology papers. Resolves DOIs with prefix `10.18653/v1/` to Anthology IDs by stripping the prefix. Downloads camera-ready PDFs from `https://aclanthology.org/{id}.pdf`. No API, no auth, no documented rate limit — but routes through the same canonical pooled-client + retry + burst-cap shape as every other provider (`_MIN_REQUEST_GAP=0.0`, `_MAX_PENDING=5`, single-flight on canonical DOI). Cache namespace: `acl_anthology/pdfs`. PDF download timeout 60s. Feeds into `papers.py`.

Coverage: all ACL-affiliated venues — ACL, EMNLP, NAACL, EACL, AACL, CoNLL, TACL, CL journal, *SEM, Findings, workshops.
