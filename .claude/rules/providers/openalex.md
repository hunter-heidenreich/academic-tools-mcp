---
paths:
  - "src/academic_tools_mcp/providers/openalex.py"
---

# openalex.py

**The budget is credits, not requests, and the classes are priced far apart** —
the constants and their measurement are at `_SEARCH_CREDITS`, the operator-facing
ceilings in `README.md` § Configuration. The consequence no comment there carries:
**both metered classes run through the quota gate.** `_throttled_get` defaults to
`metered=True`, so the `filter=` batch is gated too; `_search_gap` paces only the
`search=` class, which is the expensive one. Don't conflate the two.

**`autocomplete` and search warm nothing but their own shape.** An autocomplete
record and a search hit are projections, so one written under a singleton key
answers a later `get_work` / `get_author` / `get_institution` with a fraction of
the object. This is why **`search_works` must never send `select=`** — its hits
land where the metadata dispatcher looks.

**OpenAlex emits `"author": null` rather than dropping the key**, so `.get(k,
default)` alone is never enough on one of its objects: the default never fires.
That upstream shape is what the guards below exist for, and what `bibtex` and
`tools/paper` guard against when they read the same objects.

**`pmids` and `work_ids` are views of a work, never second copies of one.** A
second entity holding the work itself would double every citation count's
staleness window.

**A batch miss is negative-cached only when the response accounted for itself**:
every returned record attributable to a DOI we asked for, **and `meta.count` not
exceeding `len(results)`**. The second conjunct is the one that is easy to drop —
without it a truncated page poisons a live DOI for the full negative TTL.

**Element-level guards are load-bearing**, each consumed where nothing above
catches an `AttributeError`/`TypeError`: `best_pdf_url` (the OA download trust
boundary — `streaming.cached_download` does not wrap its `fetch`),
`_canonical_from_response_doi` (runs after the batch `try` closes),
`reconstruct_abstract`, and `search_authors`' warm key. Each type-checks and
degrades rather than raising.
