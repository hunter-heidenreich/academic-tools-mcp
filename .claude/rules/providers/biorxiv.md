---
paths:
  - "src/academic_tools_mcp/providers/biorxiv.py"
---

# biorxiv.py

Two things this module's own comments don't say:

**`published_doi` appears asynchronously once a preprint is published.** That lag
sets the positive TTL and is what `follow_published` consumes.

**Content hosts share one concurrency budget with the API.** Cloudflare
rate-limits `www.biorxiv.org` / `www.medrxiv.org` at the API's pace, so
`_content_gap` is a `SubGap` rather than a second `Throttle`.
