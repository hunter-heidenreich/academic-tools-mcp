---
paths:
  - "src/academic_tools_mcp/providers/*.py"
---

# API providers

**Per-provider mechanics live in each module's own docstrings and comments** —
they are written for this, and they are the first thing you will read anyway.
This file covers only what no single module can say: the contract every client
holds, and the per-provider quirks that are upstream facts rather than code.

## Common shape

Throttling (`throttle.Throttle`, `.claude/rules/net.md`) and the cached-getter
protocol (`cache.cached_lookup`, `.claude/rules/store.md`) are shared
infrastructure. A provider supplies only its policy and its quirks. **Mirror
`providers/biorxiv.py`** — the fullest instance — and read `crossref.py` as a
counter-example, not a template. The build checklist for a new provider is the
`add-provider` skill's, not this file's.

Two things the shape does not make obvious:

- **Going out as `python-httpx/x.y`** is the generic agent several upstreams
  throttle hardest, and the one that leaves an operator no way to reach us.
  Hence headers always, from the shared `useragent` module.
- **`acl` is PDF-only**, so it has the slot wrapper but no `_throttled_get`, no
  metadata getter, and none of the parsing contract below.

### Parsing and encoding — the shared contract

- **A malformed or truncated 200 body is transient; an anomalous 200 of the
  *wrong shape* is treated identically.** Neither is ever positive-cached.
- **The guard goes all the way down to the elements, and to the fields a key is
  built from.** A list-shaped payload is checked for being a list, its entries
  for being dicts, and any field handed to `doinorm.normalize` / `.split()` for
  being a `str` — each raises `AttributeError` on the wrong type, and
  `AttributeError` is in neither `_PARSE_ERRORS` nor `HTTPX_ERRORS`, so an
  unguarded body escapes the provider entirely instead of surfacing as the
  uniform `{error, retryable}` contract. **An annotation is not a guard here**:
  these values come from untyped JSON, so `raw: str | None` buys nothing at
  runtime. Each provider names its guard — `crossref._message_of`,
  `wikipedia._summary_of`, `opencitations._edges_of`, `biorxiv._collection_of` —
  so the check and the comprehension it protects stay one thought.
- **A wrong shape is an error, never an empty result set.** Reported as "no
  matches", it ends the agent's search instead of prompting a retry.
- **`safe=` is per-provider policy, not a default.** `openalex.get_work` keeps
  `safe='/'` because a DOI's own slash is structure; every other identifier here
  (`get_author`, `acl.pdf_url`, `wikipedia`) has none, so `safe=""` and a stray
  `/` is an escape. A scheme prefix — `doi:`, `orcid:` — is added outside `quote`. The request-side guard that
  `quote` cannot be, and the second check each provider owes on top of it, are in
  `.claude/rules/net.md` § `addresses_a_record` — this file does not restate it.
- **Every negative-cached error carries `not_found: True`, built by
  `http.not_found`** — never a hand-spelled dict. `acl.download_pdf`'s
  non-ACL-DOI rejection carries it too: a definitive "not mine" that arrives
  unflagged reads to every classifier as "unknown".

## openalex.py

**Guards reach the elements, not just the top-level object.** Four values come
from untyped JSON and are consumed where nothing above catches an
`AttributeError`/`TypeError`: `best_pdf_url`'s sub-objects and URLs (the OA
download trust boundary — `streaming.cached_download` does not wrap its `fetch`),
`_canonical_from_response_doi`'s argument (it runs *after* the batch request's
`try` has closed), `reconstruct_abstract`'s index (`get_paper_abstract` has no
`try`), and `search_authors`' warm key (its loop runs after the `try` too). Each
type-checks and degrades — to `None` / `""`, or to skipping that record's warm.

**An ORCID is a key and a path, and they differ.** `canonical_author_id` folds
every spelling to the bare form; the request rebuilds `orcid:<bare>`, which is
what OpenAlex resolves — a bare ORCID 404s. Widening the fold was a silent
re-key: old entries are unreachable, not wrong, and the `authors` TTL reaps them.

**`get_author` refuses a separator itself.** `quote(safe="")` escapes a `/`,
which hides a traversal from `addresses_a_record`; neither shape contains one, so
the bare id is checked before the URL is built.

**A batch miss is negative-cached only when the response accounted for itself**:
every returned record attributable to a DOI we asked for, **and `meta.count` not
exceeding `len(results)`**. That second conjunct is the one that is easy to drop
— without it a truncated page poisons a live DOI for the full negative TTL.

## arxiv.py

**The version suffix is part of the cache key**, deliberately the opposite of
bioRxiv. `2301.00001` means "whatever is current"; `2301.00001v2` means that
revision. Do not "normalize" it away.

**A spelling `normalize_arxiv_id` rejects does not merely fail to fetch.** It is
not an arXiv *shape* either, so `manual.resolve_target` files the paper under
`manual` with a canonical key that is already arXiv's — and the same paper
caches, downloads and converts twice. That cost is what buys the permissiveness:
`_ARXIV_URL_RE` carries `re.IGNORECASE`, an optional scheme and an optional
`www.`/`export.` label, matching `doinorm._DOI_URL_RE`'s latitude, because those
are the forms pasted citations and plain-text notes carry.

**`ARXIV_DOI_PREFIX` is stripped only when the tail is itself arXiv-shaped.** The
prefix names arXiv's DOI registrant, not a promise about what follows; stripping
it unconditionally mangles an unrelated DataCite record *and* costs the pass its
idempotence for a nested spelling. It is public for the reason
`acl.ACL_DOI_PREFIX` is — one spelling of a registrant, not two.

**arXiv answers HTTP 200 with a synthetic `api/errors` entry for both an invalid
id and a malformed `search_query`.** Parsed as a normal entry it becomes a "hit"
whose id is an errors URL, which the agent then chains the next tool call onto.
`_is_error_entry` is shared so `get_paper` and `search_papers` classify it
identically — as not-found and as a non-retryable query rejection respectively.

**arXiv raises `retry_attempts` above the shared default.** Its Fastly edge
returns 429/503 with no `Retry-After` when an IP is briefly penalty-boxed, and
one retry tends to land in the same cooldown; two ride `get_with_retry`'s backoff
out of it.

**XML is parsed with `defusedxml`**, and the refusal joins `ET.ParseError` in
`_PARSE_ERRORS` — transient, not not-found.

## biorxiv.py

`published_doi` appears asynchronously once a preprint is published; that lag is
what sets the positive TTL and what `follow_published` consumes
(`.claude/rules/server.md`). Everything else about this module — the two-server
fallback, the version-is-not-identity rule, the two request-side checks, the
both-servers-must-answer rule — is stated at length in its own comments.

## crossref.py

**The tier is chosen from config, not assumed.** `_resolve_policy()` picks the
rate constants at import from `in_polite_pool()`, so `_MAX_CONCURRENT` /
`_MIN_REQUEST_GAP` / `_SEARCH_REQUEST_GAP` are its *output* — don't read any one
of them as a fixed number. Limits per Crossref's REST API docs:

|        | singles    | search    | concurrent |
|--------|------------|-----------|------------|
| polite | 10 req/sec | 3 req/sec | 3          |
| public | 5 req/sec  | 1 req/sec | 1          |

**The rate we take must follow the identity we send.** Hardcoding the polite tier
while the mailto stays unconfigured makes the documented default (an empty
`.env`) request at the polite rate anonymously.

**`_resolve_policy()` runs at import but `_build_headers()` reads config per
request**, so changing `CROSSREF_MAILTO` in a live process moves the identity
without moving the rate — a tier mismatch, not a no-op. Restart, same as
`ENABLE_DEBUG_TOOLS`.

**The year filter is deliberately year-only.** Crossref does not document how it
pads a partial date, and CrossRef/rest-api-doc#7 reports the fully-specified form
dropping works whose deposited date is itself year-only — so spelling out
`-01-01` / `-12-31` is a regression, not a hardening.

**Accepted limitation:** the search gate stamps `_last_search_time` *before*
handing off to the singles slot, so under mixed load a queued search can start
later than its stamp and two searches land closer together than the gap.
Reserving the instant the way `Throttle.slot` does needs a second `Throttle`,
whose `pending` `stats.throttles()` would then sum into this namespace's
`in_flight` row.

## opencitations.py

**An empty list is a real answer, positive-cached.** An unknown-but-well-formed
DOI answers 200 with `[]`, never 404 — OpenCitations cannot tell "never indexed"
from "indexed with zero edges", so neither can this module. **Don't "fix" it into
a definitive miss the way `biorxiv._collection_of` does**: bioRxiv's empty
collection means the DOI is absent, OpenCitations' does not.

**`_parse_ids`' pairs are forwarded verbatim to the agent** by `_format_record`
and `tools/graph.py`, so a blank `doi` would read as a real identifier to chain
the next tool call onto — hence the drops. A repeated prefix takes the last
token: not a decision anyone made, and pinned by a test rather than relied on.

## wikipedia.py

**Requests without a `User-Agent` may be blocked outright.**

**`canonical_title` is built from free-form user text rather than an identifier
grammar** — the only canonicalizer here that is. It is the cache key *and* the
URL path segment, so the two cannot drift; its three folding rules and the
`"ß".upper()` length trap are in its own docstring.

## acl.py

**The module is `acl`; the namespace is `acl_anthology`** — the one place the two
diverge. It is also the value an agent passes to
`search_cached_papers(namespace=...)`.

**Invariant: the Anthology ID addresses the CDN and names nothing on disk.**
`pdf_path` keys on `canonical_key`, so the PDF, the markdown and the section
index share one stem — the identity `corpus._restore_slashes` inverts an ACL
filename with, and the reason `tools/pipeline`'s force_refresh cascade (which
drops artifacts keyed on `target["canonical"]`) reaches the file the agent just
replaced. Key the PDF on the Anthology ID instead and this becomes the one
namespace whose three artifacts disagree.

---

## Politeness: what is enforced, and what is not

Enforced per provider: the inter-start gap, the concurrency cap, the burst cap,
`Retry-After` in both RFC 9110 forms, and a descriptive `User-Agent`.

Two limits are real and deliberately **not** solved. State them rather than
implying the caps are stronger than they are:

- **Caps are per-process, not per-machine.** Each provider's `Throttle` is one
  module-level instance holding its own `asyncio.Semaphore`, so the cap is scoped
  to the interpreter. Two server instances on one machine (Claude Desktop *and*
  the CLI, a common setup) each get their own allowance, so arXiv's documented
  "single connection" rule is honoured *per process* and the host as a whole can
  double it. Fixing this needs a file-lock or a shared token bucket; until then,
  arxiv's `_MAX_CONCURRENT` is a per-process claim.
- **`max_pending` bounds queued *plus* in-flight callers**, so a provider's
  declared burst cap buys less headroom than the number suggests
  (`.claude/rules/net.md` § `net/throttle.py`).
