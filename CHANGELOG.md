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

- `search_cached_papers` results are now deterministically ordered: equal-scoring
  hits break ties by `(namespace, canonical_id)` instead of the index's internal
  entry order, so the same query returns the same ordering across sessions even as
  the incremental index grows. `top_k=0` (or negative) now returns `[]` rather than
  silently yielding one hit. Internally, the parsed search index is memoised by
  `index.json`'s stat signature, so a repeat search over an unchanged corpus skips
  the JSON re-parse. ([#24])
- `download_pdf` for arXiv and bioRxiv now coalesces concurrent calls for the
  same identifier into a single streaming download via single-flight (ACL
  Anthology already did). Previously two parallel calls for one id could both
  miss the `dest.exists()` guard and stream the file twice — the atomic rename
  kept the result correct, but doubled bandwidth and throttle cost. The slot is
  keyed `("pdf", canonical)` so the inner metadata lookup doesn't deadlock on
  the download's own slot. ([#22])

### Fixed

- `search_cached_papers` now reports the correct `snippet` and `section` for hits
  in documents containing characters whose lowercase form changes length (e.g.
  U+0130 'İ' → two chars). The match position was located in the lowercased text
  but applied to the original markdown, drifting the snippet window and section
  attribution past the real match. Snippet location now maps every offset back to
  the original text via a position-tracking transform. ([#24])
- `search_cached_papers` now restores the canonical ID for old-style arXiv hits in
  every archive (`cs/`, `math/`, `stat/`, `math.GT/…`, …), not just the eight
  hyphenated physics archives that were previously hardcoded — so the returned
  `canonical_id` round-trips back through `get_paper_metadata`. ([#24])
- BibTeX generation now emits valid, compilable entries for inputs that
  previously produced broken output. Citation keys are sanitised to ASCII
  `[a-z0-9]` — non-decomposable characters (`ø`, `ł`, `ß`, …) are transliterated
  and apostrophes/hyphens/periods dropped, so keys from authors like `O'Brien`
  or `Wałęsa` no longer leak illegal characters. Title/venue escaping now
  neutralises the full LaTeX special set (`$ \ { } ~ ^` in addition to
  `& % _ #`), and DOI fields escape their BibTeX-fatal characters.
  Organisational authors (e.g. "The ATLAS Collaboration") are brace-wrapped so
  BibTeX treats them atomically instead of inventing a surname. ([#23])

- Single-flight no longer logs a spurious `Future exception was never
  retrieved` warning to stderr when a coalesced fetch with no concurrent
  followers raises. The leader now marks its own future's exception retrieved
  before re-raising; failure propagation to waiters and the "failure is not
  cached" semantics are unchanged. ([#22])

## [2026.05.29] — 2026-05-29

### Added

- `get_paper_metadata(biorxiv_doi, follow_published=True)` now reports a
  `followed_published` signal so the bioRxiv→journal chain is no longer silent
  when it falls back. On a successful chain the `openalex_via_biorxiv` response
  carries `followed_published=True`; when the preprint has a `published_doi` but
  OpenAlex hasn't indexed the journal version yet, the response falls back to the
  preprint record (`_source="biorxiv"`) with `followed_published=False` — so a
  consumer can tell it's looking at preprint-era metadata for a paper that *is*
  published, rather than one that simply isn't published yet. The field stays
  absent when no chain was attempted (`follow_published=False` or no
  `published_doi`), so the default response shape is unchanged. ([#16])
- `find_in_paper(identifier, query, normalize=True)` and
  `search_cached_papers(query, normalize=True)` opt into diacritic-insensitive
  search: both NFKD-fold the query (and the document text) and strip combining
  marks before matching, so `cafe` matches `café` and `Gutierrez` matches
  `Gutiérrez` (and vice versa). For `find_in_paper`, the reported `char_offset`,
  `match`, and `snippet` are still sliced from the original (un-folded) text — a
  fold-with-position-map translates each match back to original offsets — so
  chaining into `get_paper_section(identifier, section_index, offset=char_offset)`
  still lands on the match. Folding turns diacritic Latin words into ASCII, so
  `whole_words` boundaries work for them; non-Latin scripts (CJK, Arabic) remain
  ASCII-word-boundary-limited and are documented as such. Default stays `False`,
  so literal-match behaviour is unchanged. ([#14])
- `get_paper_metadata(doi, fallback_crossref=True)` opts into a Crossref fallback
  when OpenAlex returns a definitive "not found" (HTTP 404) for a DOI — Crossref's
  indexing of new and niche-venue DOIs is often ahead of OpenAlex's. The fallback
  fires *only* on a true 404, never on a transient OpenAlex error (5xx/429/timeout),
  which should be retried instead. The response carries `_source="crossref"` with a
  reduced field set: no open-access info (`is_oa`/`oa_status`/`oa_url`/`pdf_url` are
  null) and no abstract path. Default stays `False`, so the hard "not found" error is
  unchanged. ([#13])
- `convert_paper(identifier, mode="fast")` adds an opt-in lightweight extraction
  fallback. It shells out to a text-only extractor (`PDF_FAST_CONVERTER`, named
  backends `pdftotext` — default — and `pymupdf` via the new `[fast]` optional
  dependency, or any custom command emitting text to stdout) and runs *outside*
  the global single-conversion lock, so it takes seconds, never returns `busy`,
  and never serialises behind a heavy MinerU run. The output is deliberately
  degraded (plain text, no tables/equations/figures/headings) and lands in the
  same cache slot as a full conversion, so a later `convert_paper(force_refresh=True)`
  upgrades it. Tunable timeout via `PDF_FAST_CONVERT_TIMEOUT` (default 120s). The
  full-mode timeout error now suggests retrying with `mode="fast"`, and every
  successful `convert_paper` response carries a `conversion_mode` field. ([#12])
- `download_pdf(identifier, allow_oa_url=True)` opts into downloading a generic
  publisher DOI from the open-access PDF URL OpenAlex reports for it
  (`best_oa_location.pdf_url` → `primary_location.pdf_url` → `open_access.oa_url`).
  Only the OpenAlex-surfaced URL is fetched — never a caller-supplied one — so the
  server stays metadata-gated rather than a general scraper. The fetch validates the
  response is actually a PDF (`%PDF-` magic bytes, rejecting HTML landing/paywall
  pages) and caches it in the `manual` namespace so `convert_paper` and the rest of
  the pipeline find it. Default stays `False`: the strict refusal (with an
  `import_paper` fallback hint) is unchanged for non-arXiv/bioRxiv/ACL identifiers.
  ([#11])

### Changed

- `search_cached_papers` is now backed by a persistent incremental index
  (`.cache/__search_index__/index.json`) instead of re-reading and re-tokenising
  every cached markdown file on every call. Each document's term frequencies are
  cached and keyed by a cheap `os.stat` staleness signal (`mtime_ns` + `size`), so
  a search only re-tokenises papers that actually changed since the last call and
  re-reads only the top-`k` winners to extract snippets. Results are byte-identical
  to the old full-scan path; the change is purely a scaling fix (the previous
  O(corpus) tokenise-per-call approached tool timeouts at thousands of cached
  papers). Both diacritic-folded and un-folded frequencies are stored, so toggling
  `normalize` never forces a re-tokenise. A new opt-in `force_refresh=True` rebuilds
  every index entry for the rare case a file changed without its mtime/size
  changing; a corrupt index or a version bump self-heals by rebuilding. ([#15])
- `get_paper_metadata` now surfaces a `pdf_url` field on OpenAlex-sourced responses,
  carrying the best open-access PDF link OpenAlex knows (preferring a direct PDF over
  a landing page). ([#11])
- `find_in_paper` now returns a `truncated` boolean in its response. It is
  `true` when more matches exist than `max_results` returned, so an agent doing
  exhaustive evidence-gathering knows the result set was capped rather than
  silently mistaking the first N hits for all of them. ([#8])

### Fixed

- Section-lock eviction (the per-paper LRU map that serialises section-cache
  re-parses) is now bounded to O(N) per pass when many locks are held, instead
  of re-scanning the whole map with `all(...)` on every iteration. The same
  pathological all-held path also no longer crashes by evicting the
  just-inserted lock and then `KeyError`-ing — the inserting key is now skipped
  during eviction. No behaviour change in the normal (few-held) case.
  (`KNOWN_ISSUES` 2.4) ([#17])
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

[Unreleased]: https://github.com/hunter-heidenreich/academic-tools-mcp/compare/v2026.05.29...HEAD
[2026.05.29]: https://github.com/hunter-heidenreich/academic-tools-mcp/compare/v2026.04.30...v2026.05.29
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
[#11]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/11
[#12]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/12
[#13]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/13
[#14]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/14
[#15]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/15
[#16]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/16
[#17]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/17
[#22]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/22
[#23]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/23
[#24]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/24
