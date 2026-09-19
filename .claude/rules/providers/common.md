---
paths:
  - "src/academic_tools_mcp/providers/*.py"
---

# API providers — the shared contract

Each module's docstrings carry its own mechanics. This file holds only what binds
every client. Per-provider quirks are in the sibling file named for the module.

**Mirror `providers/biorxiv.py`, not `crossref.py`** — the build checklist and the
reasons are the `add-provider` skill's.

**Each search bound is named for the upstream parameter it caps**, not for a spelling
shared across the package: `MAX_SEARCH_RESULTS` over `max_results`, `MAX_SEARCH_ROWS`
over `rows`, `MAX_SEARCH_LIMIT` over `srlimit`, `MAX_PAGE_SIZE` over `page_size`.
Renaming them alike would cost the one thing each name carries.

## Parsing and encoding

- **A wrong-shaped 200 is transient, never an empty result set.** Reported as "no
  matches" it ends the agent's search instead of prompting a retry. Neither a
  malformed body nor an anomalous shape is ever positive-cached.
- **Guard down to the elements, and to every field a key is built from.** These
  values come from untyped JSON, where an annotation buys nothing: `AttributeError`
  is in neither `_PARSE_ERRORS` nor `HTTPX_ERRORS`, so an unguarded body escapes
  the provider instead of surfacing as `{error, retryable}`. Name each guard
  (`crossref._message_of`, `wikipedia._summary_of`, `opencitations._edges_of`,
  `biorxiv._collection_of`) so the check and the comprehension it protects stay
  one thought.
- **`safe=` is per-provider policy, not a default.** A DOI's own slash is
  structure (`openalex.get_work` keeps `safe='/'`); every other identifier here
  has none, so `safe=""` and a stray `/` is an escape. Scheme prefixes (`doi:`,
  `orcid:`) are added outside `quote`.
- **`http.addresses_a_record` tests only the *trailing* segment**, so a provider
  interpolating its identifier anywhere else owns a second check of its own.
- **Every negative-cached error carries `not_found: True`, built by
  `http.not_found`** — never a hand-spelled dict. A definitive "not mine" that
  arrives unflagged reads to every classifier as "unknown".
- **Send `useragent.headers` always** — `tests/test_politeness.py` fails a
  respelled header dict, but only once the module exists.

## Don't generalize the `_fetch` bodies

**Refuse a shared "JSON singleton GET" helper** over `openalex._fetch_singleton`,
`crossref.get_work`, `wikipedia.get_summary` and `opencitations._fetch_direction`.
The skeleton they share is about five lines and already lives in `http` and
`cache`; the rest is each provider's own shape-guard policy, and `arxiv` (XML,
three not-found shapes) and `biorxiv` (two servers) cannot use it at all.
Revisit only if a *fifth* JSON-singleton provider lands.

The shared mechanism is single-homed; per-provider policy (arxiv's single-connection
`_MAX_CONCURRENT`, biorxiv's late `published_doi`) passes in as constructor args or
`fetch` closures and stays in its own module.

## Politeness: two limits that are real and deliberately unfixed

State them rather than implying the caps are stronger than they are.

- **Caps are per-process, not per-machine.** Each `Throttle` is one module-level
  instance, so two server instances on one host (Claude Desktop *and* the CLI)
  each get their own allowance. arXiv's "single connection" rule is a per-process
  claim until a file-lock or shared token bucket exists.
- **`max_pending` bounds queued *plus* in-flight callers**, so a declared burst
  cap buys less headroom than the number suggests.
