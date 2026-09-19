---
paths:
  - "src/academic_tools_mcp/manual.py"
---

# Paper identity and manual imports

- **`resolve_target` and `resolve_metadata_source` route on three predicates they
  do not own** (`arxiv.is_arxiv_id`, `biorxiv.is_biorxiv_doi`, `acl.is_anthology_id`),
  because storage and metadata must not disagree about which ids are whose.
- **The `migrate_*` sweeps ask the router** rather than matching a shape of their
  own, so they cannot drift from the routing they exist to catch up with. A
  spelling the router learns and a sweep doesn't strands that paper's artifacts for
  good.
- **Every pipeline entry point trades a non-DOI identifier before routing,
  `import_paper` included** — it is the one that *writes*, so a spelling it files
  under and the readers resolve away is an import nothing reads back. The three
  `refile_*_stems` functions catch up with what an older build stranded, and **a
  new traded shape owes a re-filer**, or every import already made under it orphans
  the day it starts resolving.
- **An identifier changes identity only in `app.resolve_paper_identifier`.** Graph
  tools skip the Anthology trade: they are DOI-keyed.
- **Both import functions stay synchronous; the async boundary is the tool layer.**
  `tools/pipeline.import_paper` wraps each in `asyncio.to_thread` and holds
  `papers.sections_lock` across **both** branches.
- **This module deliberately does not download arbitrary URLs.** Agents fetch
  non-native PDFs themselves and hand the local file to `import_paper`.
- **Manual imports intentionally have no BibTeX generation** — there is no
  structured metadata. When the identifier is a DOI, chain into `get_paper_bibtex`.
