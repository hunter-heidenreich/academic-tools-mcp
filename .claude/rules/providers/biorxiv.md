---
paths:
  - "src/academic_tools_mcp/providers/biorxiv.py"
---

# biorxiv.py

This module documents itself at length — the two-server fallback, the
version-is-not-identity rule, the two request-side checks and the
both-servers-must-answer rule are all in its own comments. Two things they don't
say:

**`published_doi` appears asynchronously once a preprint is published.** That lag
sets the positive TTL and is what `follow_published` consumes.

**Content hosts share one concurrency budget with the API.** Cloudflare
rate-limits `www.biorxiv.org` / `www.medrxiv.org` at the API's pace, so
`_content_gap` is a `SubGap` rather than a second `Throttle`.
