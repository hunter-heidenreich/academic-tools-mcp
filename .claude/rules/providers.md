---
paths:
  - "src/academic_tools_mcp/providers/*.py"
---

# API providers

## Common shape

Every per-provider client uses the same pattern. The two cross-cutting pieces — *throttling* and the *cached-getter protocol* — are **shared infrastructure**, not per-provider code: see `_throttle.Throttle` (`.claude/rules/http.md`) and `cache.cached_lookup` (`.claude/rules/cache.md`). A provider supplies only its *policy* and its *quirks*.

- Persistent `httpx.AsyncClient` from `_clients.get_client(NAMESPACE, headers=_useragent.headers(<mailto>), ...)`. **Every provider passes headers**, or it goes out as `python-httpx/x.y` — the generic agent several upstreams throttle hardest, and the one that leaves an operator no way to reach us. `_useragent` is the single home for the string (`academic-tools-mcp/<version> (+<repo URL>[; mailto:...])`); never hand-roll one.
- DOI normalization comes from the shared `_doi` module (`normalize` / `canonical` / `looks_like_doi`), never a local copy — a second copy that misses `dx.doi.org` or a case-insensitive `doi:` prefix lands one paper under several cache keys.
- A module-level `_throttle = Throttle(namespace=NAMESPACE, label=..., max_concurrent=_MAX_CONCURRENT, min_gap_seconds=_MIN_REQUEST_GAP, max_pending=_MAX_PENDING)`, exposed via thin `_throttled_get` (→ `_throttle.get`) and, in the PDF-downloading modules, `_request_slot` (→ `_throttle.slot`) wrappers. The `Throttle` does the three-layer gating + `_http.get_with_retry`; don't re-implement it.
- `_MAX_CONCURRENT` per-provider (the policy constant passed to `Throttle`): arxiv=1 (single-connection rule), openalex=4, acl_anthology=4, biorxiv=2, opencitations=2, wikipedia=2. crossref is **resolved from config** (3 polite / 1 public) rather than fixed — see below.
- Module-level `_single_flight` instance, passed to `cache.cached_lookup`.
- Each getter is `canonical = canonical_*(id)` → define an async `_fetch()` closure (the HTTP + parse + `cache.put`/`put_negative` body, holding the provider's quirks) → `return await cache.cached_lookup(single_flight=_single_flight, namespace=NAMESPACE, entity="<entity>", canonical=canonical, positive_ttl=_POSITIVE_TTL_SECONDS, fetch=_fetch, force_refresh=force_refresh, sf_key=...)`. `cached_lookup` owns the force_refresh-invalidate, the outer + in-slot cache re-checks, the single-flight coalescing, and the per-caller deep copy — the getter no longer hand-rolls any of it.
- Pass a tuple `sf_key` when one canonical id has multiple sub-fetches (`("work", canonical)` / `("author", canonical)` for openalex; `("references", canonical)` / `("citations", canonical)` for opencitations); omit it to key on `canonical`.
- Inside `_fetch`: on definitive 404, write the error dict to negative cache before returning; on a transient parse failure return `_parse_error_dict()` and cache nothing.
- **The PDF-downloading providers route `download_pdf` through `_pdf_download.cached_download`** the way getters route through `cache.cached_lookup` — see `.claude/rules/pdf-download.md`. Definitive download failures are negative-cached under entity `downloads` in the provider's own namespace; `_NEG_TTL_SECONDS` is the per-provider policy. arxiv and biorxiv reuse their 1h metadata negative TTL (arXiv renders PDFs lazily, so a just-announced paper's PDF can 404 for minutes; bioRxiv is the same "live in an hour" case), acl_anthology declares 24h (static camera-ready CDN — a 404 means a wrong ID or a paper not yet posted).

**Parsing and encoding hardening — the shared contract.** Every client holds these; the per-provider sections below note only where one differs.

- **A malformed or truncated 200 body is transient, not definitive.** The `json.JSONDecodeError` (or `ET.ParseError`) is caught via the module's `_PARSE_ERRORS` and returned as `_parse_error_dict()` — `{error, retryable: True}`, a fresh dict each call — and is **not** negative-cached, so a retry re-fetches.
- **An anomalous 200 of the wrong shape is treated identically**, rather than crashing the parse. Each provider knows what "wrong shape" means for its endpoint (non-dict, missing the entity `id`, not a list of records), and none of them is ever positive-cached for the TTL.
- **Identifiers are percent-encoded into the request path** via `quote(...)` so reserved characters (`#`, `?`, and a stray `/`) can't split the path or silently truncate the request to the wrong record. What stays literal differs per provider — see below.

To let an agent bypass the cache, accept `force_refresh: bool = False` and thread it into `cached_lookup` (which invalidates both halves before fetching) — see `arxiv.get_paper` / `openalex.get_work` / `biorxiv.get_paper` / `wikipedia.get_summary`.

## openalex.py

Singleton endpoints (`/works/{id}`, `/authors/{id}`). ID normalization for DOI formats, OpenAlex URLs, ORCIDs. Each entity has `_normalize_*` + `_canonical_*` pair. `_get_client()` bakes in the polite-pool `User-Agent` from `OPENALEX_MAILTO`. Single-flight keys are tuple-prefixed (`("work", canonical)`, `("author", canonical)`) — parallel work-and-author fetch on the same paper runs as two slots.

**DOI handling.** `_normalize_doi` strips surrounding whitespace and both `https://`/`http://doi.org/` (and `doi:`) prefixes; the bare DOI is encoded into `/works/doi:{doi}` with `quote(..., safe="/")`. Wrong shape here means non-dict *or* missing the entity `id` key.

Both `get_work` and `get_author` take `force_refresh`, and `get_author`'s 404 carries `not_found: True` to match `get_work`. **Known narrow race:** a `force_refresh` in-slot re-check can be repopulated by a concurrent non-refresh caller — accepted, not fixed.

**Limits:** singleton lookups (ID/DOI/ORCID) are free and unlimited. Search (1000/day), List+filter (10000/day), Content download (100/day) are not currently used.

**Batch fetch:** `get_works_batch(dois, *, force_refresh=False)` collapses N cache-miss DOIs into ⌈N/50⌉ HTTP calls via `/works?filter=doi:DOI1|DOI2|...`. Cached entries (positive or negative) are served without a network call. Each fetched work is written to the singleton cache, so a follow-up `get_work(doi)` is a free hit. DOIs requested but missing from the response are negative-cached the same way singleton 404s are. A DOI containing OpenAlex filter metacharacters (`|` = OR, `,` = AND) would corrupt the OR-joined filter, so it is split out and resolved individually through `get_work` (the encoded singleton path) instead. A parse failure or non-dict body on the batch GET maps every DOI in that chunk to a retryable `_parse_error_dict()` (not negative-cached); non-dict items inside `results` are skipped. Those per-chunk errors are built **one fresh dict per key**: `dict.fromkeys` would alias a single object across up to 50 keys, so a caller mutating its own error would corrupt the other 49. `_fetch_chunk` deliberately does **not** deep-copy its single-flight result the way `cached_lookup` does: per-key freshness removes the aliasing hazard, the works themselves are read-only to every consumer, and copying fifty full work objects per batch to guard a mutation that doesn't happen is the wrong trade. Used by `server.get_papers_metadata` for reference-graph enrichment.

## arxiv.py

arXiv Atom API (`export.arxiv.org/api/query`). ID normalization (bare IDs, URLs, query strings and fragments stripped before keying), XML→dict parsing.

**The version suffix is part of the cache key, not stripped from it.** `2301.00001` keys on the bare form and means "whatever is current"; `2301.00001v2` keys on `v2` and means that revision. Stripping it — so v1, v2 and latest share one entry — silently serves the wrong paper, because the fetch keeps the version even when the key doesn't: whichever version was requested first wins the shared key, and every later one is a cache hit returning the earlier paper's title, abstract, and authors. `get_paper`'s "not found" path covers THREE definitive shapes — a 200 with no entries, arXiv's 200-with-`api/errors` entry, and a genuine HTTP 404 — all negative-cached. Transient failures (5xx / timeout / 429 / backpressure) are returned as retryable errors and **not** cached.

**Descriptive `User-Agent`, unconditionally.** Unlike the polite-pool opt-in providers (crossref/openalex set a `User-Agent` only when a mailto is configured), `_build_headers()` / `_get_client()` send a descriptive UA on *every* call — metadata, search, and PDF download — because arXiv's Fastly edge throttles generic library UAs (`python-httpx/x.y`) far harder, returning 429/503 on modest bursts. The optional `ARXIV_MAILTO` env var is appended as a contact; the UA is sent even when it's blank. **Retries twice** (`Throttle(retry_attempts=3)`) rather than the default once, because that same edge returns 429/503 with no `Retry-After` when an IP is briefly penalty-boxed and a single retry tends to land in the same cooldown window — the two retries ride `get_with_retry`'s exponential backoff (≈3s, ≈6s).

**XML is parsed with `defusedxml`** (`fromstring`), so an entity-expansion ("billion laughs") payload is refused rather than expanded. A parse failure — malformed/truncated body or a refused entity payload — is caught alongside the HTTP errors (`_PARSE_ERRORS = (ET.ParseError, DefusedXmlException)`) and surfaced as a retryable `{error, retryable: True}` dict; it is treated as transient (not "not found"), so it is **not** negative-cached. Both `get_paper` and `search_papers` share this contract.

Search supported with `max_results` capped at 50 in the tool layer.

## biorxiv.py

bioRxiv/medRxiv API (`api.biorxiv.org`). DOI normalization (bare DOIs, URLs, site content URLs with version suffixes). Tries bioRxiv first, falls back to medRxiv. Selects latest version from multi-version responses. Parses semicolon-separated author strings. Builds PDF URLs from DOI + version + server. No documented upstream limit, so the pacing is deliberately conservative. The `published_doi` field links to the journal DOI when available — `server.get_paper_metadata(..., follow_published=True)` auto-chains to OpenAlex. No auth.

DOI prefix `10.1101/` identifies all bioRxiv and medRxiv papers.

## crossref.py

Crossref REST API (`api.crossref.org/works/{doi}`). DOI normalization via the shared `_doi` module. `_get_client()` bakes in the shared `_useragent` header, always — with `mailto` (from `CROSSREF_MAILTO`) appended when configured. Full work object cached; tool layer slices out reference list with pagination.

**Search opportunistically warms the works cache** — each `search_works` hit with a DOI is written to `crossref/works/<canonical>` (only if not already present, so a richer pre-existing entry isn't clobbered). A subsequent `get_work(doi)` is a free cache hit.

`get_work` takes `force_refresh` (mirrors `openalex.get_work`: invalidate both cache halves up front, skip the pre-slot checks) so the reference-graph tools can refresh both sources — the reference list grows as publishers re-deposit metadata.

**The tier is chosen from config, not assumed.** `_resolve_policy()` picks the rate constants at import from `in_polite_pool()`, so `_MAX_CONCURRENT` / `_MIN_REQUEST_GAP` / `_SEARCH_REQUEST_GAP` are its output rather than literals — don't read any one of them as a fixed number. Changing `CROSSREF_MAILTO` requires a restart (same as `ENABLE_DEBUG_TOOLS`).

If you touch these constants, keep the two halves in lockstep: **the rate we take must follow the identity we send.** Hardcoding the polite tier while the mailto-bearing `User-Agent` stays conditional means the documented default (an empty `.env`) requests at 2× the public-pool rate, 3× its concurrency and 10× its search rate, anonymously.

Search is paced separately (`_throttled_search_get`) because Crossref limits it far more tightly than singleton lookups — sharing the singles throttle leaves the search limit unenforced in either tier. The search gate rides *on top of* the shared `Throttle` rather than owning a second one — Crossref's concurrency budget covers all requests, so a separate semaphore would let searches and singles together exceed it. Search uses `query.bibliographic` on `/works`, capped at 20 rows.

## opencitations.py

OpenCitations Index API v2 (`api.opencitations.net/index/v2`). Outgoing references (`/references/doi:...`) and incoming citations (`/citations/doi:...`). Parses space-delimited multi-ID strings (`omid:... doi:... openalex:... pmid:...`) via `_parse_ids()`. Single-flight tuple-prefixed (`("references", canonical)` vs `("citations", canonical)`) — fetching both directions for one paper runs as two slots.

**Encoding and shape.** The bare DOI is encoded into `.../doi:{doi}` with `quote(..., safe="/")` — the `doi:` scheme prefix and the DOI's own slash stay literal. Wrong shape here means anything that isn't a list of records (dict / null / string), which would otherwise crash the `_format_record` comprehension; non-dict items inside a valid list are skipped.

`get_references` / `get_citations` are one-line wrappers over a shared `_fetch_direction(doi, *, kind, id_field, force_refresh)`; the two directions differ only by `kind` (API path segment, cache entity, result key) and `id_field` (`"cited"` / `"citing"`). Both take `force_refresh` — the citation graph grows continuously, so an agent may want fresher coverage than the positive TTL allows.

## wikipedia.py

MediaWiki OpenSearch (`/w/api.php?action=opensearch`) for title search; Wikimedia REST (`/api/rest_v1/page/summary/{title}`) for summaries and existence verification. Detects disambiguation pages via the `type` field. `_get_client()` bakes in the `User-Agent` (mailto from `WIKIPEDIA_MAILTO`) — **requests without one may be blocked outright.**

**Encoding — `safe=""`, unlike the DOI providers.** The page title is encoded with `quote(url_title, safe="")`, escaping the *whole* segment: a slash in a title like `AC/DC` is part of the title, not a path separator. Wrong shape here means a non-dict body, which would crash `get_summary`'s `data.get(...)` calls.

**The cache key is case-sensitive beyond the first character.** Wikipedia auto-capitalizes only the leading letter, so the canonical form is `url_title[:1].upper() + url_title[1:]`. A full lowercase would collide case-distinct articles like `PET` and `Pet`.

The 404 error dict carries `not_found: True` (mirroring `openalex.get_work`). `page_exists` reports `exists: False` **only** on that definitive 404 and propagates transient errors as-is, rather than reporting a network blip as confident non-existence.

**Limit:** 1000 req/hour for identified clients.

## acl_anthology.py

PDF source for ACL Anthology papers. Resolves DOIs with prefix `10.18653/v1/` to Anthology IDs by stripping the prefix. Downloads camera-ready PDFs from `https://aclanthology.org/{id}.pdf`. No API, no auth, no documented rate limit — but routes through the same canonical pooled-client + retry + burst-cap shape as every other provider, with no inter-start gap and single-flight on the canonical DOI. Feeds into `papers.py`.

Coverage: all ACL-affiliated venues — ACL, EMNLP, NAACL, EACL, AACL, CoNLL, TACL, CL journal, *SEM, Findings, workshops.

---

## Politeness: what is enforced, and what is not

Enforced per provider: the inter-start gap, the concurrency cap, the burst cap, `Retry-After` (both the delay-seconds **and** the HTTP-date form RFC 9110 permits — Wikimedia- and Cloudflare-fronted endpoints emit dates), and a descriptive `User-Agent`. `oa_download` additionally paces **per host** (`Throttle(per_host=True)`), because its URLs are publisher CDNs rather than one API and a reference walk through a single journal lands many of them on one domain.

Two limits are real and deliberately **not** solved. State them rather than implying the caps are stronger than they are:

- **Caps are per-process, not per-host.** `Throttle` holds a plain `asyncio.Semaphore` in module state. Two server instances on one machine (Claude Desktop *and* the CLI, a common setup) each get their own allowance, so arXiv's documented "single connection" rule is honoured *per process* and the host as a whole can double it. Fixing this needs a file-lock or a shared token bucket; until then, `_MAX_CONCURRENT = 1` for arxiv is a per-process claim.
- **`max_pending` bounds queued *plus* in-flight callers**, not queued alone. `slot()` increments `pending` before acquiring the semaphore, so with `max_concurrent=4, max_pending=5` only one caller can actually be waiting before the sixth is refused. The refusal is still correct backpressure; the name just promises more headroom than it delivers.

**Counter semantics** live with the counters: `http_calls` splits between `_http.get_with_retry` (per outbound request) and `Throttle.slot(count_request=True)` (streaming downloads that bypass the retry helper) — see `.claude/rules/http.md`. `cache.get(count=False)` likewise suppresses hit/miss counting for `cached_lookup`'s in-slot re-check and for cache-warming probes, so one lookup registers one outcome.
