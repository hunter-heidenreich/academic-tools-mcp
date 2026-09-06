---
paths:
  - "src/academic_tools_mcp/providers/*.py"
---

# API providers

## Common shape

Every per-provider client uses the same pattern. The two cross-cutting pieces — *throttling* and the *cached-getter protocol* — are **shared infrastructure**, not per-provider code: see `_throttle.Throttle` (`.claude/rules/http.md`) and `cache.cached_lookup` (`.claude/rules/cache.md`). A provider supplies only its *policy* and its *quirks*. This section is the *client* shape; registration, tools and tests for a brand-new provider are the `add-provider` skill's checklist.

- Persistent client from `_clients.get_client(NAMESPACE, headers=..., timeout=...)`, **always with headers**, and DOI normalization from the shared `_doi` module — never a local copy of either. Rationale in `.claude/rules/utils.md`. Going out as `python-httpx/x.y` is the generic agent several upstreams throttle hardest, and the one that leaves an operator no way to reach us.
- `_MAX_CONCURRENT` / `_MIN_REQUEST_GAP` / `_MAX_PENDING` are declared per provider with the reason in a comment beside them and passed to one module-level `Throttle`, reached through thin wrappers: `_throttled_get` (→ `_throttle.get`) wherever the provider parses a body, `_request_slot` (→ `_throttle.slot`) wherever it streams a PDF. `acl_anthology` is PDF-only and has only the slot wrapper. Crossref's `_MAX_CONCURRENT` / `_MIN_REQUEST_GAP` / `_SEARCH_REQUEST_GAP` are **`_resolve_policy()` output, resolved at import from config** — never read them as fixed numbers; its `_MAX_PENDING` is a literal like every other provider's.
- Module-level `_single_flight` instance, passed to `cache.cached_lookup`.
- Each getter is `canonical = canonical_*(id)` → an async `_fetch()` closure holding the HTTP + parse + caching decisions → `return await cache.cached_lookup(...)`. Accept `force_refresh: bool = False` and thread it in; don't hand-roll any of the rest (`.claude/rules/cache.md` for what the protocol owns). Only `wikipedia.get_summary` builds its canonical inline, having no DOI-like id to normalize.
- Pass a tuple `sf_key` when one canonical id has multiple sub-fetches (openalex `("work"|"author", canonical)`, opencitations `("references"|"citations", canonical)`, and arxiv/biorxiv `download_pdf`'s `("pdf", canonical)`, whose `fetch` awaits their own `get_paper` on the same `SingleFlight`); omit it to key on `canonical`, as `acl_anthology.download_pdf` does — PDF-only, so nothing can collide.
- Inside `_fetch`: on definitive 404, write the error dict to negative cache before returning; on a transient parse failure return `_parse_error_dict()` and cache nothing.
- **The PDF-downloading providers route `download_pdf` through `_pdf_download.cached_download`** the way getters route through `cache.cached_lookup` — see `.claude/rules/pdf-download.md`. Definitive download failures are negative-cached under entity `downloads` in the provider's own namespace; `_NEG_TTL_SECONDS` is the per-provider policy. arxiv and biorxiv reuse their short metadata negative TTL (arXiv renders PDFs lazily, so a just-announced paper's PDF can 404 for minutes; bioRxiv is the same "live in an hour" case), acl_anthology declares a long one (static camera-ready CDN — a 404 means a wrong ID or a paper not yet posted).

**Parsing and encoding hardening — the shared contract.** Every client that parses a body holds these; `acl_anthology` is PDF-only and has none.

- **A malformed or truncated 200 body is transient, not definitive.** The `json.JSONDecodeError` (or `ET.ParseError`) is caught via the module's `_PARSE_ERRORS` and returned as `_parse_error_dict()` — `{error, retryable: True}`, a fresh dict each call — and is **not** negative-cached, so a retry re-fetches.
- **An anomalous 200 of the wrong shape is treated identically**, rather than crashing the parse. Each provider knows what "wrong shape" means for its endpoint (non-dict, missing the entity `id`, not a list of records), and none of them is ever positive-cached for the TTL.
- **Identifiers reaching a request path are percent-encoded** via `quote(...)`, so reserved characters (`#`, `?`, a stray `/`) can't split the path or truncate the request to the wrong record. `safe=` differs per provider — see below. **Three request paths interpolate an identifier with no `quote` — quote them if you touch them**: `openalex.get_author`'s `/authors/{api_id}`, `biorxiv._fetch`'s two details URLs, and `acl_anthology.pdf_url`.

## openalex.py

Singleton endpoints (`/works/{id}`, `/authors/{id}`). Each entity has a private `_normalize_*` and a **public** `canonical_*` (`canonical_doi`, `canonical_author_id`) — `manual` and the tool layer import the canonical form. `_normalize_author_id` strips only an `https://openalex.org/` prefix: an ORCID URL is handed to the API verbatim, not rewritten.

**DOI handling.** `_normalize_doi` is a thin wrapper over `_doi.normalize` (`.claude/rules/utils.md`); the bare DOI is encoded into `/works/doi:{doi}` with `quote(..., safe="/")`. Wrong shape here means non-dict *or* missing the entity `id` key. `get_author`'s 404 carries `not_found: True` to match `get_work`.

**Batch fetch:** `get_works_batch` collapses N cache-miss DOIs into ⌈N/`_BATCH_CHUNK_SIZE`⌉ HTTP calls via `/works?filter=doi:DOI1|DOI2|...`. Cached entries (positive or negative) are served without a network call. Each fetched work is written to the singleton cache, so a follow-up `get_work(doi)` is a free hit. A DOI containing OpenAlex filter metacharacters (`|` = OR, `,` = AND) would corrupt the OR-joined filter, so it is split out and resolved individually through `get_work`. A parse failure or non-dict body on the batch GET maps every DOI in that chunk to a retryable `_parse_error_dict()`; non-dict items inside `results` are skipped.

A requested DOI missing from the response is negative-cached **only when the response accounted for itself**: no returned record carried an unrequested DOI string, and `meta.count` did not exceed `len(results)`. Otherwise the miss is inconclusive and returns `{error, retryable: True}` uncached — negative-caching a truncated page would poison a live DOI for the full negative TTL.

Per-chunk errors are one fresh dict per key — never `dict.fromkeys`, which would alias one object across the whole chunk. That per-key freshness is why `_fetch_chunk` is the one deliberate `SingleFlight.do` caller that skips `cached_lookup`'s deep copy; its docstring carries the argument.

## arxiv.py

arXiv Atom API (`export.arxiv.org/api/query`).

**The version suffix is part of the cache key.** `2301.00001` means "whatever is current"; `2301.00001v2` means that revision. Stripping it serves the wrong paper — the fetch keeps the version even when the key doesn't. Do not "normalize" it away. `get_paper`'s "not found" path covers three definitive shapes — a 200 with no entries, arXiv's 200-with-`api/errors` entry, and a genuine HTTP 404 — all negative-cached. Transient failures (5xx / timeout / 429 / backpressure) are returned as retryable errors and **not** cached.

**`_normalize_arxiv_id` is the single home for the ID's spellings, and it must stay idempotent.** It accepts a bare id, an `arXiv:` prefix in any case (arXiv's own "Cite as" form), and an `abs`/`pdf` URL. **Invariant: the prefix is stripped before the URL handling, in a loop, with whitespace re-stripped after** — the ordering `_doi.normalize` holds for `doi:`, and for the same reasons: `arXiv:https://arxiv.org/abs/…` occurs in pasted citations, and a single pass leaves `arXiv:arXiv:…` keying separately from its own output. A spelling this rejects does not merely fail to fetch: it is not an arXiv *shape* either, so `manual.resolve_target` files the paper under `manual` with a canonical key that is already arXiv's, and the same paper caches, downloads and converts twice.

**`search_papers` warms both keys.** Each hit is written under `canonical_arxiv_id` *and* `base_arxiv_id`: warming only the versioned one would leave every bare lookup a miss.

**arXiv raises `retry_attempts` above the shared default.** arXiv's Fastly edge returns 429/503 with no `Retry-After` when an IP is briefly penalty-boxed, and one retry tends to land in the same cooldown; two ride `get_with_retry`'s backoff out of it.

**XML is parsed with `defusedxml`**, so an entity-expansion payload is refused rather than expanded; the refusal joins `ET.ParseError` in `_PARSE_ERRORS` and is transient, not not-found. `get_paper` and `search_papers` share it.

## biorxiv.py

bioRxiv/medRxiv API (`api.biorxiv.org`). Nothing in the shared `10.1101/` prefix tells the two servers apart, so `get_paper` tries bioRxiv and falls back to medRxiv — including when the *first* response was malformed, since medRxiv may still answer cleanly.

`_collection_of` is what makes that safe: it returns `None` for a wrong-shape body and a list (possibly empty) for a well-formed one. Only a well-formed **empty** collection is "not found" and negative-cached; a wrong-shape body from *both* servers is `_parse_error_dict()` and is never cached. Its shape guard is load-bearing, not padding — the docstring says why.

`published_doi` appears asynchronously once a preprint is published; that lag is what sets the positive TTL and what `follow_published` consumes (`.claude/rules/server.md`).

## crossref.py

Crossref REST API (`api.crossref.org/works/{doi}`). Full work object cached; the tool layer slices out the reference list with pagination.

**Search opportunistically warms the works cache** — each `search_works` hit with a DOI is written to `crossref/works/<canonical>` unless a **within-TTL** entry already exists (a TTL-aware `cache.get`, not a presence test), so fresher search data replaces a stale entry but never clobbers a live one. A subsequent `get_work(doi)` is a free cache hit.

**The tier is chosen from config, not assumed.** `_resolve_policy()` picks the rate constants at import from `in_polite_pool()`, so `_MAX_CONCURRENT` / `_MIN_REQUEST_GAP` / `_SEARCH_REQUEST_GAP` are its output rather than literals — don't read any one of them as a fixed number. Limits per Crossref's REST API docs; this table is the one `providers/crossref.py` and `tests/test_politeness.py` point at:

|        | singles    | search    | concurrent |
|--------|------------|-----------|------------|
| polite | 10 req/sec | 3 req/sec | 3          |
| public | 5 req/sec  | 1 req/sec | 1          |

If you touch these, keep the two halves in lockstep: **the rate we take must follow the identity we send.** Hardcoding the polite tier while the mailto stays unconfigured makes the documented default (an empty `.env`) request at the polite rate anonymously.

`_resolve_policy()` runs at **import** but `_build_headers()` reads config per request, so changing `CROSSREF_MAILTO` in a live process moves the identity without moving the rate — a tier mismatch, not a no-op. Restart, same as `ENABLE_DEBUG_TOOLS`.

Search is paced separately (`_throttled_search_get`) because Crossref limits it far more tightly than singleton lookups — sharing the singles throttle leaves the search limit unenforced in either tier. The search gate rides *on top of* the shared `Throttle` rather than owning a second one: Crossref's concurrency budget covers all requests, so a separate semaphore would let searches and singles together exceed it. Search uses `query.bibliographic` on `/works`.

## opencitations.py

OpenCitations Index API v2. Outgoing references (`/references/doi:...`) and incoming citations (`/citations/doi:...`).

**Encoding and shape.** The bare DOI is encoded into `.../doi:{doi}` with `quote(..., safe="/")` — the `doi:` scheme prefix and the DOI's own slash stay literal. Wrong shape here means anything that isn't a list of records (dict / null / string), which would otherwise crash the `_format_record` comprehension; non-dict items inside a valid list are skipped.

`get_references` / `get_citations` are one-line wrappers over a shared `_fetch_direction(doi, *, kind, id_field, force_refresh)`; the two directions differ only by `kind` (API path segment, cache entity, result key) and `id_field` (`"cited"` / `"citing"`).

## wikipedia.py

MediaWiki OpenSearch (`/w/api.php?action=opensearch`) for title search; Wikimedia REST (`/api/rest_v1/page/summary/{title}`) for summaries and existence verification. **Requests without a `User-Agent` may be blocked outright.**

**Encoding — `safe=""`, unlike the DOI providers.** The page title is encoded with `quote(url_title, safe="")`, escaping the *whole* segment: a slash in a title like `AC/DC` is part of the title, not a path separator. Wrong shape here means a non-dict body, which would crash `get_summary`'s `data.get(...)` calls.

**The cache key is case-sensitive beyond the first character.** Wikipedia auto-capitalizes only the leading letter, so the canonical form is `url_title[:1].upper() + url_title[1:]`. A full lowercase would collide case-distinct articles like `PET` and `Pet`.

The 404 error dict carries `not_found: True` (mirroring `openalex.get_work`). `page_exists` reports `exists: False` **only** on that definitive 404 and propagates transient errors as-is, rather than reporting a network blip as confident non-existence.

## acl_anthology.py

PDF source for ACL Anthology papers. Resolves `10.18653/v1/` DOIs to Anthology IDs by stripping the prefix (case-insensitively) and then **uppercasing old-format IDs** (`_OLD_FORMAT_ID_RE`, e.g. `P16-1160`): the CDN path is case-sensitive and Crossref hands these DOIs back lowercased. New-format IDs (`2023.acl-long.1`) must stay untouched.

Downloads camera-ready PDFs from `aclanthology.org`. No API, no auth, no documented rate limit — but routes through the same canonical pooled-client + retry + burst-cap shape as every other provider, with no inter-start gap and single-flight on the canonical DOI. `manual.resolve_target()` is what routes an ACL DOI here, and it must be checked before the generic-DOI branch.

---

## Politeness: what is enforced, and what is not

Enforced per provider: the inter-start gap, the concurrency cap, the burst cap, `Retry-After` (both the delay-seconds **and** the HTTP-date form RFC 9110 permits — Wikimedia- and Cloudflare-fronted endpoints emit dates), and a descriptive `User-Agent`.

Two limits are real and deliberately **not** solved. State them rather than implying the caps are stronger than they are:

- **Caps are per-process, not per-machine.** Each provider's `Throttle` is one module-level instance holding its own `asyncio.Semaphore`, so the cap is scoped to the interpreter. Two server instances on one machine (Claude Desktop *and* the CLI, a common setup) each get their own allowance, so arXiv's documented "single connection" rule is honoured *per process* and the host as a whole can double it. Fixing this needs a file-lock or a shared token bucket; until then, arxiv's `_MAX_CONCURRENT` is a per-process claim.
- **`max_pending` bounds queued *plus* in-flight callers**, not queued alone, so a provider's declared burst cap buys less headroom than the number suggests. The mechanism is in `.claude/rules/http.md` § `_throttle.py`.
