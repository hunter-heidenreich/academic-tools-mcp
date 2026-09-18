---
paths:
  - "src/academic_tools_mcp/providers/wikipedia.py"
---

# wikipedia.py

**Two endpoint families, documented and rate-limited separately** — `get_summary`
on the Wikimedia REST API, `search` on the MediaWiki Action API. A fact about one
is not a fact about the other: `addresses_a_record` guards the summary path, where
the title *is* a path segment, and does not apply to search, whose input rides in
a query parameter on a static path.

**`list=search` is full text, and that is the whole point.** A title-prefix
suggester answers `[]` to a query phrased as a description rather than a title,
and a structurally valid empty list fires no guard — it reads to the agent as
"Wikipedia has no article on this", the failure `common.md` forbids, arriving
without a wrong shape.

**`canonical_title` is built from free-form user text rather than an identifier
grammar** — the only canonicalizer here that is, and it is the cache key *and* the
URL path segment, so the two cannot drift.
