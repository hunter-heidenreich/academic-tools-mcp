---
paths:
  - "src/academic_tools_mcp/providers/*.py"
---

# API providers

## Common shape

Every per-provider client uses the same pattern. The two cross-cutting pieces — *throttling* and the *cached-getter protocol* — are **shared infrastructure**, not per-provider code: see `_throttle.Throttle` (`.claude/rules/http.md`) and `cache.cached_lookup` (`.claude/rules/cache.md`). A provider supplies only its *policy* and its *quirks*. This section is the *client* shape; registration, tools and tests for a brand-new provider are the `add-provider` skill's checklist.

- Persistent client from `_clients.get_client(NAMESPACE, headers=..., timeout=...)`, **always with headers**, and DOI normalization from the shared `_doi` module — never a local copy of either. Rationale in `.claude/rules/utils.md`. Going out as `python-httpx/x.y` is the generic agent several upstreams throttle hardest, and the one that leaves an operator no way to reach us.
- `_MAX_CONCURRENT` / `_MIN_REQUEST_GAP` / `_MAX_PENDING` are declared per provider with the reason in a comment beside them and passed to one module-level `Throttle`, reached through thin wrappers: `_throttled_get` (→ `_throttle.get`) wherever the provider parses a body, `_request_slot` (→ `_throttle.slot`) wherever it streams a PDF. `acl` is PDF-only and has only the slot wrapper. Crossref's `_MAX_CONCURRENT` / `_MIN_REQUEST_GAP` / `_SEARCH_REQUEST_GAP` are **`_resolve_policy()` output, resolved at import from config** — never read them as fixed numbers; its `_MAX_PENDING` is a literal like every other provider's.
- Module-level `_single_flight` instance, passed to `cache.cached_lookup`.
- Each getter is `canonical = canonical_*(id)` → an async `_fetch()` closure holding the HTTP + parse + caching decisions → `return await cache.cached_lookup(...)`. Accept `force_refresh: bool = False` and thread it in; don't hand-roll any of the rest (`.claude/rules/cache.md` for what the protocol owns). Only `wikipedia.get_summary` builds its canonical inline, having no DOI-like id to normalize.
- Pass a tuple `sf_key` when one canonical id has multiple sub-fetches (openalex `("work"|"author", canonical)`, opencitations `("references"|"citations", canonical)`, and arxiv/biorxiv `download_pdf`'s `("pdf", canonical)`, whose `fetch` awaits their own `get_paper` on the same `SingleFlight`); omit it to key on `canonical`, as `acl.download_pdf` does — PDF-only, so nothing can collide.
- Inside `_fetch`: on definitive 404, write the error dict to negative cache before returning; on a transient parse failure return `_parse_error_dict()` and cache nothing.
- **The PDF-downloading providers route `download_pdf` through `_pdf_download.cached_download`** the way getters route through `cache.cached_lookup` — see `.claude/rules/pdf-download.md`. Definitive download failures are negative-cached under entity `downloads` in the provider's own namespace; `_NEG_TTL_SECONDS` is the per-provider policy. arxiv and biorxiv reuse their short metadata negative TTL (arXiv renders PDFs lazily, so a just-announced paper's PDF can 404 for minutes; bioRxiv is the same "live in an hour" case), acl declares a long one (static camera-ready CDN — a 404 means a wrong ID or a paper not yet posted).

**Parsing and encoding hardening — the shared contract.** Every client that parses a body holds these; `acl` is PDF-only and has none.

- **A malformed or truncated 200 body is transient, not definitive.** The `json.JSONDecodeError` (or `ET.ParseError`) is caught via the module's `_PARSE_ERRORS` and returned as `_parse_error_dict()` — `{error, retryable: True}`, a fresh dict each call — and is **not** negative-cached, so a retry re-fetches.
- **An anomalous 200 of the wrong shape is treated identically**, rather than crashing the parse. Each provider knows what "wrong shape" means for its endpoint (non-dict, missing the entity `id`, not a list of records), and none of them is ever positive-cached for the TTL.
- **Identifiers reaching a request path are percent-encoded** via `quote(...)`, so reserved characters (`#`, `?`, a stray `/`) can't split the path or truncate the request to the wrong record. `safe=` differs per provider — see below. **Two request paths interpolate an identifier with no `quote` — quote them if you touch them**: `openalex.get_author`'s `/authors/{api_id}` and `biorxiv._fetch`'s two details URLs. (`acl.pdf_url` uses `safe=""`: an Anthology ID has no path structure, so a stray `/` is an escape.)

## openalex.py

Singleton endpoints (`/works/{id}`, `/authors/{id}`). Each entity has a private `_normalize_*` and a **public** `canonical_*` (`canonical_doi`, `canonical_author_id`) — `manual` and the tool layer import the canonical form. `_normalize_author_id` strips only an `https://openalex.org/` prefix: an ORCID URL is handed to the API verbatim, not rewritten.

**DOI handling.** `_normalize_doi` is a thin wrapper over `_doi.normalize` (`.claude/rules/utils.md`); the bare DOI is encoded into `/works/doi:{doi}` with `quote(..., safe="/")`. Wrong shape here means non-dict *or* missing the entity `id` key. `get_author`'s 404 carries `not_found: True` to match `get_work`.

**Batch fetch:** `get_works_batch` collapses N cache-miss DOIs into ⌈N/`_BATCH_CHUNK_SIZE`⌉ HTTP calls via `/works?filter=doi:DOI1|DOI2|...`. Cached entries (positive or negative) are served without a network call. Each fetched work is written to the singleton cache, so a follow-up `get_work(doi)` is a free hit. A DOI containing OpenAlex filter metacharacters (`|` = OR, `,` = AND) would corrupt the OR-joined filter, so it is split out and resolved individually through `get_work`. A parse failure or non-dict body on the batch GET maps every DOI in that chunk to a retryable `_parse_error_dict()`; non-dict items inside `results` are skipped.

A requested DOI missing from the response is negative-cached **only when the response accounted for itself**: no returned record carried an unrequested DOI string, and `meta.count` did not exceed `len(results)`. Otherwise the miss is inconclusive and returns `{error, retryable: True}` uncached — negative-caching a truncated page would poison a live DOI for the full negative TTL.

Per-chunk errors are one fresh dict per key — never `dict.fromkeys`, which would alias one object across the whole chunk. That per-key freshness is why `_fetch_chunk` is the one deliberate `SingleFlight.do` caller that skips `cached_lookup`'s deep copy; its docstring carries the argument.

## arxiv.py

arXiv Atom API (`export.arxiv.org/api/query`).

**The version suffix is part of the cache key.** `2301.00001` means "whatever is current"; `2301.00001v2` means that revision. Stripping it serves the wrong paper — the fetch keeps the version even when the key doesn't. Do not "normalize" it away. `get_paper`'s "not found" path covers three definitive shapes — a 200 with no entries, arXiv's 200-with-`api/errors` entry, and a genuine HTTP 404 — collapsed onto **one** `_not_found()` closure so all three cache one payload carrying `not_found: True`. The 404 is tested on `response.status_code` *before* `raise_for_status`, as every sibling does: routed through `_http.error_dict` instead it negative-caches a raw body snippet for the full TTL. Transient failures (5xx / timeout / 429 / backpressure) are returned as retryable errors and **not** cached.

**`normalize_arxiv_id` is the single home for the ID's spellings, and it must stay idempotent.** It accepts a bare id, an `arXiv:` prefix in any case (arXiv's own "Cite as" form), an `abs`/`pdf` URL, and arXiv's own DataCite DOI. **Invariant: the prefix is stripped before the URL and DOI handling, in a loop, with whitespace re-stripped after** — the ordering `_doi.normalize` holds for `doi:`, and for the same reasons: `arXiv:https://arxiv.org/abs/…` occurs in pasted citations, and a single pass leaves `arXiv:arXiv:…` keying separately from its own output. A spelling this rejects does not merely fail to fetch: it is not an arXiv *shape* either, so `manual.resolve_target` files the paper under `manual` with a canonical key that is already arXiv's, and the same paper caches, downloads and converts twice.

Two consequences of that cost, both load-bearing:

- **`_ARXIV_URL_RE` is `re.IGNORECASE` with an optional scheme and an optional `www.`/`export.` host label**, matching `_doi._DOI_URL_RE`'s latitude for the same reason — those are the forms pasted citations and plain-text notes carry.
- **`ARXIV_DOI_PREFIX` (`10.48550/arXiv.`) is stripped only when the tail is itself arXiv-shaped.** The prefix names arXiv's DOI registrant, not a promise about what follows; stripping it unconditionally mangles an unrelated DataCite record *and* costs the pass its idempotence for a nested spelling. The prefix is exported for the reason `acl.ACL_DOI_PREFIX` is: `bibtex` builds the `eprint` field from it, and a second spelling would let the router and the BibTeX writer disagree about which papers are arXiv's.

The *shape* is `is_arxiv_id`, public beside it: `manual`'s two dispatchers route on it exactly as they route on `biorxiv.is_biorxiv_doi` and `acl.is_acl_doi`, so no caller re-derives what an arXiv id looks like. `OLD_ARCHIVE_PATTERN` / `OLD_NUMBER_PATTERN` are exported for the reason `_doi` exports `REGISTRANT_PATTERN` — `cache_search` matches the old-style form over `safe_stem`'s `_` rather than `/`, and a second spelling would let it invert a stem that never routes here. The new-style grammar crosses no module boundary and stays private.

**Version stripping has one body, in two spellings of one rule.** `strip_version` preserves case (BibTeX's `eprint` keeps `math.GT/0309136`'s archive class); `base_arxiv_id` is `strip_version` over the folded canonical, and is the cache key a bare request uses. `id_from_entry` inverts the `id` URL in a parsed Atom entry — the tool layer's `arxiv_id` field and `search_papers`' warm keys both come from it, so a local `split("/abs/")` anywhere is a fork.

**`search_papers` warms both keys, through `cache.warm`.** Each hit is written under `canonical_arxiv_id` *and* `base_arxiv_id`: warming only the versioned one would leave every bare lookup a miss. `max_results` is clamped to the exported `MAX_SEARCH_RESULTS`, which is also the `search_arxiv` tool's validation bound.

**arXiv answers with HTTP 200 and a synthetic `api/errors` entry for both an invalid id *and* a malformed `search_query`.** `_is_error_entry` is shared so `get_paper` and `search_papers` classify it identically — as not-found and as a non-retryable query rejection respectively. Parsed as a normal entry it becomes a "hit" whose id is an errors URL, which the agent then chains the next tool call onto.

**arXiv raises `retry_attempts` above the shared default.** arXiv's Fastly edge returns 429/503 with no `Retry-After` when an IP is briefly penalty-boxed, and one retry tends to land in the same cooldown; two ride `get_with_retry`'s backoff out of it.

**XML is parsed with `defusedxml`**, so an entity-expansion payload is refused rather than expanded; the refusal joins `ET.ParseError` in `_PARSE_ERRORS` and is transient, not not-found. `get_paper` and `search_papers` share it.

## biorxiv.py

bioRxiv/medRxiv API (`api.biorxiv.org`). Nothing in the shared `10.1101/` prefix tells the two servers apart, so `get_paper` tries bioRxiv and falls back to medRxiv — including when the *first* response was malformed, since medRxiv may still answer cleanly.

`_collection_of` is what makes that safe: it returns `None` for a wrong-shape body and a list (possibly empty) for a well-formed one. Only a well-formed **empty** collection is "not found" and negative-cached; a wrong-shape body from *both* servers is `_parse_error_dict()` and is never cached. Its shape guard is load-bearing, not padding — the docstring says why.

`published_doi` appears asynchronously once a preprint is published; that lag is what sets the positive TTL and what `follow_published` consumes (`.claude/rules/server.md`).

## crossref.py

Crossref REST API (`api.crossref.org/works/{doi}`). Full work object cached; the tool layer slices out the reference list with pagination.

**Search opportunistically warms the works cache** — each `search_works` hit with a DOI goes through `cache.warm` (`.claude/rules/cache.md`), which owns the TTL-aware probe. A subsequent `get_work(doi)` is a free cache hit.

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

## acl.py

**The module is `acl`; the namespace is `acl_anthology`.** The one place the two diverge — every other provider's module name is its namespace. `NAMESPACE` is the cache *directory* name, so folding it to `acl` orphans every cached ACL artifact; rename it only behind a sweep like `migrate_legacy_pdf_stems`. It is also the value an agent passes to `search_cached_papers(namespace=...)`.

PDF source for ACL Anthology papers. Downloads camera-ready PDFs from `aclanthology.org`. No API, no auth, no documented rate limit — but routes through the same canonical pooled-client + retry + burst-cap shape as every other provider, with no inter-start gap and single-flight on the canonical DOI. `manual.resolve_target()` is what routes an ACL DOI here, and it must be checked before the generic-DOI branch.

**Invariant: the Anthology ID addresses the CDN and names nothing on disk.** `pdf_path` keys on `canonical_key`, so the PDF, the markdown and the section index share one stem — the identity `cache_search._restore_slashes` inverts an ACL filename with, and the reason `tools/pipeline`'s force_refresh cascade (which drops artifacts keyed on `target["canonical"]`) reaches the file the agent just replaced. Key the PDF on the Anthology ID instead and this becomes the one namespace whose three artifacts disagree.

`doi_to_anthology_id` therefore serves `pdf_url` and the `anthology_id` provenance field only: it strips the prefix (case-insensitively) and **uppercases old-format IDs** (`_OLD_FORMAT_ID_RE`, e.g. `P16-1160`), because the CDN path is case-sensitive and Crossref hands these DOIs back lowercased. New-format IDs (`2023.acl-long.1`) must stay untouched. `canonical_key` is `_doi.canonical` — ACL layers no URL form of its own, so unlike `biorxiv.canonical_key` there is no second normalization step to hold here.

**An empty suffix is not an ACL DOI.** `_strip_acl_prefix` rejects `10.18653/v1/`, so it falls through to the generic-DOI route: it names no paper, and `safe_stem("")` is the empty stem, which would cache every such identifier as the same `.pdf` — the hole `manual._identifier_error` closes for a blank import.

**`migrate_legacy_pdf_stems()`** re-files PDFs written under the old Anthology-ID stem, at startup beside `papers.migrate_legacy_stems` and `manual.migrate_misrouted_arxiv`. Same discipline as those (`.claude/rules/pipeline.md`): `.pdf` only, materialised listing via `_stems.list_dir`, skip on collision, idempotent because a migrated stem now carries the `safe_stem(ACL_DOI_PREFIX)` prefix the sweep gates on. Only `pdfs/` moves — markdown and sections were always canonical-keyed.

`ACL_DOI_PREFIX` is exported (as `biorxiv.DOI_PREFIX` is) for the reason `_doi` exports `REGISTRANT_PATTERN`: `cache_search._NAMESPACE_DOI_PREFIXES` needs the prefix rather than the function, and a second spelling would let the router and the stem inverter disagree.

---

## Politeness: what is enforced, and what is not

Enforced per provider: the inter-start gap, the concurrency cap, the burst cap, `Retry-After` (both the delay-seconds **and** the HTTP-date form RFC 9110 permits — Wikimedia- and Cloudflare-fronted endpoints emit dates), and a descriptive `User-Agent`.

Two limits are real and deliberately **not** solved. State them rather than implying the caps are stronger than they are:

- **Caps are per-process, not per-machine.** Each provider's `Throttle` is one module-level instance holding its own `asyncio.Semaphore`, so the cap is scoped to the interpreter. Two server instances on one machine (Claude Desktop *and* the CLI, a common setup) each get their own allowance, so arXiv's documented "single connection" rule is honoured *per process* and the host as a whole can double it. Fixing this needs a file-lock or a shared token bucket; until then, arxiv's `_MAX_CONCURRENT` is a per-process claim.
- **`max_pending` bounds queued *plus* in-flight callers**, not queued alone, so a provider's declared burst cap buys less headroom than the number suggests. The mechanism is in `.claude/rules/http.md` § `_throttle.py`.
