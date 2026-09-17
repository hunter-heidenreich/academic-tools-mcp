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
- **`acl` fetches a whole collection per metadata miss** (§ acl.py).

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

**arXiv answers an invalid id and a malformed `search_query` with a synthetic
`api/errors` entry, under HTTP 400 (or 200).** Parsed as a normal entry it
becomes a "hit" whose id is an errors URL, which the agent then chains the next
tool call onto; raised as a plain 400 it becomes an unflagged `error_dict`
snippet. `_rejection_root` lets exactly that 400 reach the parse, and
`_is_error_entry` is shared so `get_paper` and `search_papers` classify it
identically — as not-found and as a non-retryable query rejection respectively.
A valid-shape id that does not exist is a 200 with an empty feed.

**An `id_list` batch answers in its own order and omits a missing id silently.**
`get_papers_batch` therefore attributes each entry by id, never by position, and
negative-caches an omission only when the feed accounted for itself — every entry
attributable and `totalResults` equal to the entries returned, the openalex rule.
More upstream facts shape it: **one id arXiv rejects fails the whole request**,
and **some records always 500 with the `api/errors` entry** (`hep-th/9901001v1`).
Either way the chunk falls back to singleton `get_paper`; a plain 5xx stays
chunk-wide, since a penalty box shouldn't multiply requests. And **a bare
id and an older revision in one request both come back**, so the bare key takes
the newest version returned rather than whichever entry matched first. The
default page is 10, so `max_results` rides every request.

**A search page past `MAX_SEARCH_WINDOW` gets HTTP 500, not a 4xx.** The limit
is on `start + max_results`, not on the match count, and the 500 carries an
`api/errors` entry reading "internal error" — so the retry path would call it
transient and spend three attempts on a request that cannot succeed.
`search_papers` refuses such a page before building the request.

**arXiv raises `retry_attempts` above the shared default.** Its Fastly edge
returns 429/503 with no `Retry-After` when an IP is briefly penalty-boxed, and
one retry tends to land in the same cooldown; two ride `get_with_retry`'s backoff
out of it.

**License and revision history come from OAI-PMH, not the Atom API**, which
carries neither. `get_versions` reads the `arXivRaw` record at `_OAI_BASE_URL`
through the same `_throttled_get`: arXiv's single-connection rule covers every
interface, so a second throttle would double the rate it allows. OAI-PMH reports
every error in a 200 body as `<error code=…>`; only `idDoesNotExist` is a
definitive miss. The record is keyed by the bare id (one record spans every
revision) and lives `_VERSIONS_TTL_SECONDS`, far shorter than a paper's: a new
revision is exactly what it reports. Older papers carry no `<license>`.

**`arxiv:doi` is the author-recorded journal DOI**, sometimes several separated by
whitespace and often absent; `follow_published` takes the first.

**`arxiv.org/html/<id>` is LaTeXML's rendering, and not every paper has one.**
arXiv answers 404 with a "No HTML for …" page when the source was not LaTeX or
conversion failed; `get_html` negative-caches that on the short arXiv TTL, since a
rendering can land after announcement. A 200 without `_HTML_MARKER` is the wrong
shape and so transient. The body is held in memory, so `MAX_PDF_BYTES` bounds it
too.

**XML is parsed with `defusedxml`**, and the refusal joins `ET.ParseError` in
`_PARSE_ERRORS` — transient, not not-found.

## biorxiv.py

`published_doi` appears asynchronously once a preprint is published; that lag is
what sets the positive TTL and what `follow_published` consumes
(`.claude/rules/server.md`). Everything else about this module — the two-server
fallback, the version-is-not-identity rule, the two request-side checks, the
both-servers-must-answer rule — is stated at length in its own comments.

**Content hosts get a stricter pace.** Cloudflare rate-limits or challenges
`www.biorxiv.org` / `www.medrxiv.org` at the API's pace, so content fetches wait
out `_content_gap` — a `SubGap`, so both request classes share one concurrency
budget.

**`/pubs` is per-server, like `/details`**, so `get_paper` asks with the record's
`server`. It merges the journal fields outside the details cache entry, or a
`/pubs` failure would stick for the record's 7 days.

**`get_jats` fetches only https `jatsxml` URLs on `_JATS_HOSTS`** — the
`download/openaccess` rule. Any other URL is "no full text", negative-cached. A
200 without `_JATS_MARKER` is a challenge or error page, so transient.

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

**Accepted limitation:** the search gate stamps its start *before*
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

## paperswithcode.py

**Two per-IP allowances, both shared with the operator's browser**: one for the
whole catalog, and a stricter one for every *collection* endpoint (`/papers/search`,
`/evaluations/`, the `/tasks/` and `/datasets/` lists). So the module has crossref's
two gates, and any list URL goes through `_throttled_search_get`. The gaps derive
from the documented rates and `_BUDGET_FRACTION`; change the inputs, not the gaps.
`retry_attempts=1` is deliberate: a retry spends the operator's allowance inside
the cooldown a 429 just announced.

**Papers are keyed by unversioned arXiv ID only**: upstream has one record per
paper and no DOI lookup. The arXiv shape test, run before the URL is built, is the
second request-side check. Task and benchmark slugs come from `canonical_slug`,
whose alphabet can shorten the path only by being empty.

**Never paginate for the caller, and never warm from search.** A search hit is a
partial record and would poison `get_paper`'s key. Upstream is explicitly not a
bulk-export service, so refuse any helper that walks `next_page`.

**Accepted limitation:** pacing cannot see the operator's browser traffic, which
can still trigger a 429. The lockout in `.claude/rules/net.md` § `net/stats.py`
then stops the server adding to it.

## acl.py

**The module is `acl`; the namespace is `acl_anthology`** — the one place the two
diverge. It is also the value an agent passes to
`search_cached_papers(namespace=...)`.

**Invariant: the Anthology ID keys all three artifacts.** Half the Anthology has
no DOI, and the force_refresh cascade only reaches artifacts sharing one key.

**The ID grammar is closed.** Claiming a label or volume ID (`P16-1`) would
negative-cache a paper that doesn't exist.

**Cache per paper, not per collection**: `cached_lookup` deep-copies a
megabytes-large collection on every read.

**The hosted-DOI index keeps its last good copy.** A failed refresh must not flip
a DOI's identity, and is recorded as a positive entry: it establishes nothing
about the DOI. A stale index answers while it refreshes in the background: the
refresh is a 12 MB download, and every generic DOI lookup would otherwise wait on it.

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
