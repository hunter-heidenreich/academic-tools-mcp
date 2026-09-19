---
paths:
  - "src/academic_tools_mcp/providers/arxiv.py"
---

# arxiv.py

**The version suffix is part of the cache key**, deliberately the opposite of
bioRxiv: `2301.00001` means "whatever is current", `2301.00001v2` that revision.
Do not "normalize" it away.

**`_ARXIV_URL_RE` is permissive on purpose.** A spelling `normalize_arxiv_id`
rejects is not an arXiv *shape* either, so `manual.resolve_target` files the paper
under `manual` and the same paper caches, downloads and converts twice. That cost
is what buys the latitude.

**arXiv answers an invalid id or a malformed `search_query` with a synthetic
`api/errors` entry under HTTP 400 — or 200, or with an empty-bodied 406.**
`_rejection_root` lets that 400 reach the parse and `_is_error_entry` is shared, so
`get_paper` and `search_papers` classify it identically; `_is_edge_rejection` takes
the 406.

**An id that does not exist comes back as an empty feed or as that same 406**, so the
406 cannot carry `not_found` however much it looks like absence.

**Its `retryable: True` is what lets `download_pdf` reach the PDF host**, which rides a
different edge and keeps serving while the Atom API refuses.

**Widening the shared retryable-status allowlist to 406 was rejected**: an id arXiv will
not resolve is the commonest trigger, so a transparent retry would spend every attempt on
a request that cannot succeed.

Three upstream facts the batch path is built around:

- **One id arXiv rejects fails the whole request**, so a chunk falls back to
  singleton `get_paper` — on the 406 as on the `api/errors` entry.
- **Some records always 500 with the `api/errors` entry** — `hep-th/9901001v1` is
  the standing instance. A plain 5xx stays chunk-wide; a penalty box shouldn't
  multiply requests.
- **A bare id and an older revision in one request both come back**, so the bare
  key takes the newest version returned, not whichever entry matched first.

**`retry_attempts` is above the shared default because of the Fastly edge**: it
returns 429/503 with no `Retry-After` when an IP is briefly penalty-boxed, and one
retry tends to land in the same cooldown.

**A search page past `MAX_SEARCH_WINDOW` gets HTTP 500 carrying an "internal
error" entry, not a 4xx** — so the retry path would call it transient and spend
three attempts on a request that cannot succeed. `search_papers` refuses the page
before building it.

**License and revision history come from OAI-PMH, not the Atom API.** Both
interfaces share arXiv's single-connection rule, so `get_versions` reuses
`_throttled_get` rather than adding a second throttle. Only `idDoesNotExist` among
OAI-PMH's in-body errors is a definitive miss; older papers carry no `<license>`.
