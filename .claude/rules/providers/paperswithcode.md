---
paths:
  - "src/academic_tools_mcp/providers/paperswithcode.py"
---

# paperswithcode.py

**Two per-IP allowances, both shared with the operator's browser**: one for the
catalog, a stricter one for the collection endpoints. Every collection call passes
`search_gate=True` — today `search` and `_evaluation_page`; a new one owes the same.
The gaps derive from the documented rates and `_BUDGET_FRACTION`; **change the
inputs, not the gaps.**

**Never paginate for the caller, and never warm from search.** Upstream is
explicitly not a bulk-export service, so refuse any helper that walks `next_page`.

**Accepted limitation:** pacing cannot see the operator's browser traffic, which
can still trigger a 429. `net/stats.py`'s lockout then stops the server adding to it.
