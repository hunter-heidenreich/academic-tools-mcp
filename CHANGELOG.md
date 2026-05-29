# Changelog

All notable changes to **academic-tools-mcp** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses **calendar versioning**: each release is named for the day
it was cut — `YYYY.MM.DD`, tagged `vYYYY.MM.DD` in git. The PEP 440 form in
`pyproject.toml` drops leading zeros (tag `v2026.05.29` ↔ version `2026.5.29`).
A rare second release on the same day takes a `.postN` suffix.

Releases from `2026.04.30` onward are tagged in git. Entries are **reconstructed
from git history** up to that first tag — the project carried no tags before
then, so each earlier date marks the day that batch of work landed on `main`,
grouped by milestone rather than per commit.

## [Unreleased]

### Changed

- `find_in_paper` now returns a `truncated` boolean in its response. It is
  `true` when more matches exist than `max_results` returned, so an agent doing
  exhaustive evidence-gathering knows the result set was capped rather than
  silently mistaking the first N hits for all of them. ([#8])

### Fixed

- `get_with_retry` now honors a server's `Retry-After` up to a 10-minute ceiling
  instead of clamping it to ~30s, so a provider asking for a genuine multi-minute
  cooldown (e.g. a sustained arXiv 429) is respected rather than retried
  aggressively. A misconfigured huge `Retry-After` is still bounded. ([#10])
- `download_pdf(force_refresh=True)` no longer deletes the cached PDF *before*
  attempting the re-download. A failed refetch (404, transport error,
  `MAX_PDF_BYTES` abort) now leaves the existing file intact; the new bytes are
  streamed to a temp file and atomically swapped in only on success. Affects all
  three PDF providers (arXiv, bioRxiv/medRxiv, ACL Anthology). ([#6])
- `convert_paper` no longer leaks its `/tmp/pdf-convert-*` extraction directory
  when a conversion fails (spawn error, timeout, non-zero exit, or no markdown
  produced). Cleanup now runs on every exit path, so a long-running server
  doesn't accumulate orphaned extraction dirs from failed conversions. ([#8])
- `import_paper` for pre-converted markdown now stores a `markdown_checksum`
  alongside the cached section index, matching the PDF-conversion path. A later
  `convert_paper` / section read on an imported paper now trusts the cache
  instead of re-parsing the markdown on every call. ([#8])
- `download_pdf` now normalizes old-style ACL Anthology paper IDs (e.g.
  `P16-1160`) to the case-sensitive form `aclanthology.org` expects, so a
  Crossref-lowercased DOI like `10.18653/v1/p16-1160` no longer 404s. New-format
  IDs (`2023.acl-long.1`) are left untouched. ([#9])

## [2026.04.30] — 2026-04-30

### Added

- `search_cached_papers` — BM25 keyword search across all locally converted
  paper markdown.
- `find_in_paper` — substring / whole-word search inside a single converted
  paper, returning the section and character offset of every hit so an agent can
  chain straight into `get_paper_section`.
- `get_papers_metadata` — batch metadata that collapses N identifiers into
  ⌈N/50⌉ OpenAlex calls (`/works?filter=doi:…|…`) plus concurrent arXiv/bioRxiv
  fan-out, for reference-graph enrichment.
- Streaming PDF downloads: chunked write (64 KiB) to a sibling temp file with
  atomic rename, plus a `MAX_PDF_BYTES` cap (default 200 MB) that aborts oversize
  streams mid-download.

### Changed

- Replaced the single global serial request lock with **per-provider
  concurrency caps**, so reference-graph traversals run in parallel up to each
  provider's limit instead of fully serialising.
- Robustness audit across all seven API clients: pooled `httpx.AsyncClient`,
  one transparent retry honouring `Retry-After`, request single-flight, negative
  caching, positive-cache TTL eviction, and per-provider stats counters.
- Split the dense CLAUDE.md guidance into path-scoped `.claude/rules/` files.

## [2026.04.22] — 2026-04-22

### Changed

- Consolidated the per-provider metadata tools into one identifier-dispatched
  family (`get_paper_metadata` / `_authors` / `_abstract` / `_bibtex`).
- Unified the reference/citation tools (6 → 4) with count-as-survey across
  Crossref and OpenCitations.
- Paginated `get_paper_authors` to bound responses on large-collaboration
  papers; replaced `get_paper_section` truncation with offset pagination.
- Normalised HTTP errors across all seven API clients; tightened tool
  docstrings; slimmed search hits to triage-only fields; stripped cache
  filesystem paths from PDF pipeline tool responses.

### Removed

- Trimmed the exposed tool surface: disabled topics/citations by default,
  removed arbitrary-URL download, merged redundant author/import tools, and
  dropped the Wikipedia existence check.

### Fixed

- `convert_pdf` no longer re-runs conversion when the sections cache is merely
  stale; a missing section-cache checksum is now treated as stale; subprocess
  failure paths are hardened.

## [2026.04.16] — 2026-04-16

### Added

- Server instructions, section truncation, and `anthropic/maxResultSizeChars`
  annotations on tool responses. ([#2])
- Response-quality improvements: error `suggestion` fields, pre-computed
  aggregates, empty-state handling, and retry hints. ([#4])
- Non-retryable signalling for PDF conversion failures. ([#5])

### Changed

- Consolidated 15 PDF-pipeline tools into 4 unified tools. ([#3])

### Fixed

- Duplicated "to markdown" phrasing in the convert tool docstrings. ([#1])

## [2026.04.05] — 2026-04-05

### Added

- Initial public release. A FastMCP server wrapping OpenAlex, arXiv,
  bioRxiv/medRxiv, Crossref, OpenCitations, ACL Anthology, and Wikipedia.
- Paper metadata / authors / abstract / BibTeX tools; reference and citation
  graph tools; the PDF download → markdown conversion → section-reading
  pipeline; manual PDF/markdown import.
- Configurable external PDF converter, env-based API configuration
  (mailto / keys), MIT license, and a public-facing README.

[Unreleased]: https://github.com/hunter-heidenreich/academic-tools-mcp/compare/v2026.04.30...HEAD
[2026.04.30]: https://github.com/hunter-heidenreich/academic-tools-mcp/compare/v2026.04.22...v2026.04.30
[2026.04.22]: https://github.com/hunter-heidenreich/academic-tools-mcp/compare/v2026.04.16...v2026.04.22
[2026.04.16]: https://github.com/hunter-heidenreich/academic-tools-mcp/compare/v2026.04.05...v2026.04.16
[2026.04.05]: https://github.com/hunter-heidenreich/academic-tools-mcp/releases/tag/v2026.04.05
[#1]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/1
[#2]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/2
[#3]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/3
[#4]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/4
[#5]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/5
[#6]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/6
[#8]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/8
[#9]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/9
[#10]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/10
