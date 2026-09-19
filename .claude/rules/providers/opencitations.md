---
paths:
  - "src/academic_tools_mcp/providers/opencitations.py"
---

# opencitations.py

**The access token is upstream's ask and buys nothing.** There is no tier to
widen, so no confirm-then-widen gate of the kind `crossref` needs — **don't
"optimise" the rate constants against a configured token.**

**An empty list is a real answer, positive-cached.** OpenCitations cannot tell
"never indexed" from "indexed with zero edges", so neither can this module.
**Don't "fix" it into a definitive miss the way `biorxiv._collection_of` does** —
bioRxiv's empty collection means the DOI is absent; this one does not.

**The count endpoints are a fallback and rescue nothing at the top end.** Both
time out or 504 on works with tens of thousands of citations, past any wait worth
handing an agent. So the list stays primary, **the 30s timeout is deliberate
rather than an oversight to widen**, and no tally may stand in for a list.

**OpenCitations Meta is deliberately unused.** `/meta/v1/metadata/{ids}` is
batch-capable and returns title/author/pub_date/venue — precisely the deficit that
costs OpenCitations the `auto` survey. Left out because graph rows are lean triage
output and provider-specific metadata wants its own tool. The Index likewise
accepts `pmid:` and `omid:` directly, declined so one paper keeps one cache key.
