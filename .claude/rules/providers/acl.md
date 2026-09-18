---
paths:
  - "src/academic_tools_mcp/providers/acl.py"
---

# acl.py

**Invariant: the Anthology ID keys all three artifacts.** Half the Anthology has
no DOI, and the `force_refresh` cascade only reaches artifacts sharing one key.

**A metadata miss fetches a whole collection**, which is why `cached_lookup`
caches per paper — a collection entry would be deep-copied on every read.

**`acl_anthology` is also the value an agent passes to
`search_cached_papers(namespace=...)`**, so the module/namespace divergence
reaches the tool surface, not just the disk.
