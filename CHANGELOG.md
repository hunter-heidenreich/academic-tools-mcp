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

### Added

- **A PMID is an identifier this server accepts, not just one it hands out.**
  Every OpenCitations row carries a `pmid` cross-reference, so a citing row with
  a `pmid` and no `doi` was a dead end. `pmid:20079334`, a
  `pubmed.ncbi.nlm.nih.gov` URL (including the legacy `/pubmed/` path) or a bare
  7–8 digit run now works anywhere a DOI does: the paper tools, the PDF pipeline
  and the graph tools. A PMID is **traded for the paper's DOI** and is never
  itself a cache key, so both spellings share one `_canonical_id` and one cached
  work, and the lookup after the trade costs no request. OpenAlex is the
  resolver, so a paper it has not indexed does not resolve; PMCIDs are
  unsupported. ([#115])
- **`get_paper_metadata` reports an OpenAlex work's `pmid`**, as bare digits
  (null when OpenAlex has none), closing the round trip in both directions.
  Batched results carry it too. ([#115])
- **`search_openalex`, free-text search across all of OpenAlex.** The primary
  metadata provider was the only one with no search of its own, so discovery was
  `search_arxiv` (preprints, field-scoped) and `search_crossref_by_title`
  (bibliographic match) — neither of which answers "what has been written about
  X". Returns the same slim triage envelope as its siblings, plus
  `cited_by_count` and `is_oa`. Unlike a Crossref hit, **every hit is free to
  chain**: it warms `openalex/works` under the very key `get_paper_metadata`
  reads, which is why the request deliberately sends no `select=` — a projected
  work is a partial object, and warming that key with one would poison every
  reader of it. ([#117])
- **`fallback_crossref` reaches all four paper tools, not just
  `get_paper_metadata`.** A DOI Crossref had indexed and OpenAlex had not gave
  you a title and a venue but no authors, abstract or BibTeX — on a server whose
  stated purpose includes generating BibTeX. `get_paper_authors`,
  `get_paper_abstract` and `get_paper_bibtex` now take the flag too, on the same
  terms: opt-in, a definitive OpenAlex 404, and OpenAlex-routed DOIs only. What
  Crossref cannot supply is null rather than absent — a Crossref abstract is its
  JATS deposit rendered to plain text, and many records carry none. ([#116])

### Fixed

- **A transient Crossref failure inside `fallback_crossref` is no longer
  swallowed.** The agent asked for the fallback and got back only OpenAlex's
  `not_found`, with no way to tell that Crossref had been tried and might work on
  a retry. The error now carries `crossref_fallback_retryable: true`, mirroring
  `published_lookup_retryable`. ([#116])
- **A Crossref-sourced `@phdthesis` carries its `school`.** Crossref deposits
  `institution` as a list of *objects*, not the list of strings `title` and
  `container-title` carry, so reading it alike dropped the field from every
  dissertation. A non-string `publisher` is now dropped rather than stringified
  into the entry. ([#116])
- **`arxiv.org/html/...` URLs route to arXiv.** `/abs/` and `/pdf/` matched but
  `/html/` did not, though it has been the default landing page for new papers
  since late 2023 — the spelling a pasted browser tab carries. Unrecognised,
  `get_paper_metadata` answered "Cannot resolve paper provider" and the pipeline
  filed the paper a **second** time under `manual`. The startup sweep re-files
  anything already misfiled. ([#115])
- **`import_paper` trades a PMID for the paper's DOI**, as every tool that reads
  what it writes already did. It filed under a key `convert_paper`,
  `get_paper_sections`, `get_paper_section` and `find_in_paper` all resolve away
  — an import no tool could read back, under any spelling. ([#115])

### Changed

- **A bare 7–8 digit identifier is now read as a PMID.** Shorter runs are
  unchanged, so a freeform `import_paper(file, "1234")` label still routes to
  `manual`. An **existing** 7–8 digit label is not stranded: the first call that
  resolves it re-files its cached PDF and markdown onto the DOI stem, carrying
  the section index across so an imported file keeps the `"imported"` marker
  that exempts it from `download_pdf`'s cascade. A bare-digit stem is *linked*
  rather than moved — a freeform label could have written it — so the original
  reading survives too. This cannot be a startup sweep: the destination is the
  paper's DOI, which only a network lookup knows. ([#115])

## [2026.09.08] — 2026-09-08

The largest release so far: a review pass over every provider, the PDF
pipeline, the cache, the corpus index and the BibTeX generator, plus the
module layout that now matches the layering underneath them. No tool was
removed and no tool was renamed, but several response shapes gained keys and a
few identifier spellings now route to a different provider — the entries below
say which.

### Added

- **`search_crossref_by_title` takes `max_results`.** The triage list was fixed
  at 5 with no way to widen it, while `search_arxiv` and `search_wikipedia`
  both exposed their provider's cap. Bounded by `crossref.MAX_SEARCH_ROWS`; the
  default is unchanged. ([#105])
- **`search_cached_papers` documents its `unindexable` report.**
  `unindexable_count`, `unindexable` and `unindexable_note` were returned but
  named in no docstring, so no agent had reason to look for them. Each entry now
  also carries `canonical_id` — the note tells the agent to run `find_in_paper`
  on those papers, and it was handing over an on-disk `stem` that no tool
  resolves. The CJK limitation is documented there too: `unicode61` does not
  segment Han/Kana/Hangul, so such a paper is indexed but matches only whole
  whitespace-delimited runs. ([#66], [#105])
- **`get_paper_references` / `get_paper_citations` document their error
  shapes.** Both return `retryable` on a tool-layer error, and
  `get_paper_references` adds `sources: {crossref, opencitations}` when both
  providers fail, with the top-level `retryable` the disjunction of the two —
  none of it named in a docstring, so no agent had reason to branch on it. The
  citations docstring also says what it does *not* have: no `source` parameter
  and no `sources` envelope, because OpenCitations is the only index of incoming
  citations. ([#106])
- **`{python}` now works in `PDF_CONVERTER`, not just `PDF_FAST_CONVERTER`.**
  One placeholder vocabulary across both templates, so a converter installed
  beside the server can be invoked as `{python} -m my_tool {input} {output_dir}`
  without pointing `PDF_CONVERTER_VENV` at a virtualenv. ([#93])
- **`config.number`, the single home for a limit an operator can turn off.**
  The disable vocabulary (`none` / `off` / `disabled` / `0`) was spelled once in
  the download path and again in the pipeline, and the copies had drifted:
  fractions, exponents and `inf` were honoured as timeouts but rejected as size
  caps. The one deliberate difference — a non-positive `MAX_PDF_BYTES` keeps the
  disk guard while a non-positive timeout disables it — is now an explicit
  argument rather than a divergence between two copies. ([#88])
- **`get_server_stats` reports PDF write failures and the `.env` that won at
  import.** `cache_write_failures` counted a failed `cache.put` but not a failed
  PDF write — the largest write the server makes, and so the one most likely to
  hit a full disk. `env_file` names which candidate path was actually loaded,
  which nothing could observe before. ([#81], [#88])
- **A `Known upstream limitations` section in the README.** OpenAlex drops or
  mangles diacritics in author names, reports an author's *current* affiliation
  rather than their affiliation at publication time, and arXiv and the published
  DOI can list different author sets for the same work. These are properties of
  the upstream providers, but until now they were written down only in
  `CLAUDE.md` — a file users don't read — so anyone citing a result had no way
  to know which fields to double-check. ([#73])

### Changed

- **Module layout follows the layering, and the PDF pipeline is a package.**
  `papers.py` (1340 lines, six responsibilities) is now
  `papers/{sections,index,convert}.py` over a `stems` naming layer; the shared
  HTTP modules moved into `net/` (`http`, `throttle`, `clients`, `stats`), the
  cache and its primitives into `store/` (`cache`, `atomic`, `singleflight`,
  `stems`), the two halves of the download story into `download/` (`streaming`,
  `openaccess`), and the leaf helpers into `util/` (`config`, `doinorm`,
  `textnorm`, `useragent`). Module names no longer carry a leading underscore;
  `_app` is `app` and `cache_search` is `corpus`, whose `_MAX_TOP_K` is now the
  public `corpus.MAX_TOP_K` its three siblings always were. Providers no longer
  import the PDF *converter* — and its subprocess, temp-dir and signal
  machinery — just to name a file. Internal only: no tool, response shape,
  environment variable or `.cache/` namespace changes. ([#91], [#106])
- **The bundled fast-extraction backend is `academic_tools_mcp.fast_extract`,
  not `academic_tools_mcp._fast_extract`.** The `PDF_FAST_CONVERTER=pymupdf`
  key is unaffected and needs no change. This only matters if you copied that
  backend's literal command template into a custom `PDF_FAST_CONVERTER`
  value — update the module path if so. ([#106])
- **`providers/acl_anthology.py` is now `providers/acl.py`.** Import it as
  `from .providers import acl`. Its `NAMESPACE` is unchanged — the cache still
  lives in `.cache/acl_anthology/`, and `search_cached_papers(namespace=...)`
  still takes `"acl_anthology"`. ([#94])
- **arXiv IDs are recognised in five more spellings, so one paper no longer
  caches twice.** Now routed to arXiv instead of the `manual` namespace: arXiv's
  own DataCite DOI (`10.48550/arXiv.2301.00001`, also via `https://doi.org/...`
  and `doi:...`), and `abs`/`pdf` URLs carrying a `www.` or `export.` host
  label, no scheme at all, an uppercase host, or a trailing slash. One
  user-visible consequence: `get_paper_metadata("10.48550/arXiv.X")` now answers
  with `_source: "arxiv"` (arXiv's native record) where it used to answer
  `_source: "openalex"` — the shape-based dispatch every other arXiv spelling
  already got. ([#95])
- **Three startup sweeps re-file artifacts cached under a superseded name.**
  ACL Anthology PDFs move to the canonical DOI stem
  (`.cache/acl_anthology/pdfs/P16-1160.pdf` → `10.18653_v1_p16-1160.pdf`), so
  the PDF, markdown and section index finally share one stem as they do in every
  other namespace; identifiers containing characters outside `[A-Za-z0-9._-]`
  are renamed forward to `safe_stem`'s encoding (on a 7,000-file cache, 20 files
  move — Elsevier PII-style DOIs and freeform labels); and papers misrouted to
  `manual` under an id that was always arXiv's are moved into the arXiv
  namespace. All three are idempotent, never overwrite an existing file, and
  leave anything they cannot move for the next run. Without them those papers
  report "not converted yet" and re-run conversions that take tens of minutes.
  ([#48], [#86], [#94])
- **`download_pdf` drops stale markdown whenever it actually downloads, not just
  on `force_refresh=True`.** A PDF that was evicted or pruned and then re-fetched
  left the previous markdown in place, and the next `convert_paper` served it as
  `cached: True` — text for a file that no longer exists. The cascade is now
  keyed on new bytes landing. Markdown you supplied yourself via `import_paper`
  is exempt, since no converter can reproduce it; pass `force_refresh=True` to
  replace that too. ([#104])
- **`get_paper_sections` reports `conversion_mode`, and the field gained
  `"imported"`.** Its own `sections_note` tells you to re-run `convert_paper`
  with `mode='full'` if the markdown came from a fast extraction — which you
  could not tell from the response. `"full"` / `"fast"` / `"imported"` (markdown
  that never ran through a converter), or null for a paper converted before the
  field existed. ([#66], [#104])
- **Every `convert_paper` error carries `conversion_mode` and `retryable`, plus
  `pdf_size_mb` where the PDF was sized.** Fast-mode failures already named their
  mode; full-mode ones, the `busy` response and the "PDF not found" guard did
  not, so an agent had to feature-detect across the two paths to learn which
  backend had failed. On an error the tag names the mode that *failed* — a
  `{timed_out: True, conversion_mode: "fast"}` says a fast retry is pointless.
  An unknown `mode` is now rejected rather than silently running a full
  conversion: the MCP boundary types it `Literal["full", "fast"]`, but a direct
  library caller passing `mode="fasst"` used to start a twenty-minute job. ([#93])
- **The keyword search index moved from a single JSON file to SQLite FTS5.**
  The old index held every document's full term-frequency map, in both a folded
  and an un-folded form, and parsed the whole thing into the heap on first use.
  Measured on a 3,732-paper corpus:

  | | before | after |
  |---|---|---|
  | index on disk | 193 MB | 165 MB |
  | process RSS after first search | **933 MB** | **43 MB** |
  | first search in a fresh process | **1308 ms** | **40 ms** |
  | one new paper | rewrote all 193 MB | one row |
  | ranking cost within a query | ~1300 ms | **1.3 ms** |

  The index is **contentless** (`content=''`): it stores postings, never the
  text. The markdown is already on disk and the top hits are re-read for
  snippets anyway, so not storing it twice is what makes the database smaller
  than the JSON it replaces. Two FTS tables, not one: `normalize` is a
  query-time flag in this API while diacritic folding is a build-time tokenizer
  option in FTS5, so the index carries a folded and an un-folded table and the
  flag selects between them. Migration is automatic — the database is built on
  first search (~7s for 3,700 papers) and the old `index.json` is deleted.
  ([#55])
- **The reference and citation tools reject a non-DOI locally.**
  `get_paper_references{,_count}` and `get_paper_citations{,_count}` forwarded
  whatever they were given to Crossref and OpenCitations, both of which are
  DOI-only: an arXiv ID cost an upstream round-trip to earn a 404 and then
  negative-cached a key that could never have resolved. They now gate on
  `looks_like_doi` — the same predicate `get_paper_metadata` dispatches on — and
  return `{error, not_found, suggestion}` naming the mismatch. ([#77])
- **`import_paper` echoes the canonical cache key, and rejects a blank
  identifier.** The `identifier` field carried the caller's spelling
  DOI-normalized, which is not the key the file was filed under
  (`arXiv:2301.00001v2` was echoed verbatim while the PDF cached as
  `2301.00001v2`); it now returns the routed canonical, so an agent can route on
  what it gets back. A blank identifier (`""`, whitespace, a bare `doi:`) is now
  an error: it keyed the empty string, cached the paper as `.pdf` / `.md`, and
  served every later blank import the *first* one as `cached: True`. ([#89])
- **Open-access PDF downloads are paced at one request per second, per publisher
  host.** They previously had no inter-start gap at all, on the reasoning that
  "every URL is a different host" — an assumption, not a fact. The URLs come from
  OpenAlex, so a reference walk through one journal resolves many DOIs to the
  same domain, which then got fetched back-to-back at the one provider with no
  documented budget and no relationship to trade on. `max_concurrent` stays
  global in both modes: it bounds *our* egress, so a per-host cap would let a
  20-publisher walk open 40 parallel streams. ([#68])
- **`import_paper` and converter post-processing run off the event loop.**
  `import_paper` is `async` but called the synchronous import helpers inline, so
  copying a PDF (up to `MAX_PDF_BYTES`, 200 MB by default) or parsing a large
  markdown file stalled every concurrent tool call for the duration. On the
  converter side the read half was already thread-offloaded and the write half
  was not: a thesis-sized markdown's rewrite, parse, hash and write ran inline
  while the loop served every other tool call. ([#66], [#91])
- **Citation keys pick a better title word and a fuller surname.** Particle
  detection gained BibTeX's own case rule — a lowercase word before the surname
  is part of it — so `da Costa`, `do Nascimento` and `ter Braak` key and render
  like `van Tilborg` already did. `_TITLE_SKIP` grew from 11 English articles to
  the English closed class plus the articles and prepositions of the major
  publication languages, so a French or German title no longer keys on `la` or
  `der`, and hyphenated compounds stay whole (`Pre-exposure` → `preexposure`,
  not `pre`) with a Romance elision stripped (`L'exil` → `exil`). Against a
  595-title OpenAlex sample this changes ~11% of keys. ([#85])
- **Generated titles are double-braced.** `title={{...}}` case-protects the whole
  title, so `plain.bst` and its relatives stop rendering "NaCl" as "nacl". An
  entry regenerated from a cached record will differ from one emitted before this
  change. ([#85])
- **Config values are stripped of surrounding whitespace, and
  `ACADEMIC_TOOLS_ENV_FILE` is authoritative.** A trailing space in a `.env` line
  is a typo, not a setting, so `get` now strips it and reads a whitespace-only
  value as unset — matching `flag`, which always did. And a named `.env` that
  isn't there now means "no `.env`" rather than falling through to the project
  root, `$PWD` or `$XDG_CONFIG_HOME`: a typo'd path was silently loading a
  different file's settings. ([#88])
- **DOI normalization is idempotent and accepts `www.doi.org`.** The `doi:`
  prefix strip loops, so a doubled prefix can no longer survive one pass and key
  separately from its own output. The bare-vs-URL treatment of `?`/`#` is
  unchanged but now documented and pinned: a bare DOI keeps them (they are legal
  suffix characters, and truncating would key a different paper), a URL cuts at
  them. ([#77])
- **Uniform "not converted yet" error.** Four tools produced this condition in
  two different shapes — three jammed the recovery advice into the `error` string
  while `find_in_paper` split it into a `suggestion` key. Agents branch on
  `suggestion`, so the shape now comes from one builder. ([#51])
- **The `[fast]` extra requires `pymupdf>=1.24.3`.** The top-level `pymupdf`
  import alias landed in 1.24.3, so 1.24.0–1.24.2 satisfied the old pin and then
  failed at import — reporting, misleadingly, that pymupdf was not installed.
  That message now carries the underlying `ImportError`, which also
  distinguishes a broken native install from a missing one. ([#108])

### Removed

- **`wikipedia.page_exists()`.** It was reachable from no tool and no caller —
  `get_wikipedia_summary` already answers the same question, returning `type`
  (`standard` / `disambiguation`), `url`, and `{error, not_found: True}` on a
  definitive 404. The README no longer advertises "page existence checks" as a
  Wikipedia capability. ([#100])
- **`papers.HEADING_PATTERN` and `papers.SECTION_LEVELS`.** Both were exported so
  the corpus index could share the heading grammar; it now delegates
  `first_section_heading()` instead, and neither symbol had a reader left. ([#93])
- **The `CITATION_SOURCE` parameter type.** It described a `source` parameter
  that `get_paper_citations` does not have, and nothing referenced it. ([#51])

### Fixed

#### Identifier routing and cache keys

- **arXiv served the wrong version's metadata and PDF bytes.** The cache key
  stripped the version suffix (`2301.00001v2` → `2301.00001`) while the API
  request kept it, so whichever version was fetched first won the shared key.
  Every later version was a silent cache hit returning the first one's title,
  abstract and authors — and `download_pdf` handed back the first one's bytes
  marked `cached: True`. `force_refresh` could not help: it invalidated that same
  shared key. The version is now part of the key, so `2301.00001v1`,
  `2301.00001v2` and the bare `2301.00001` ("latest") are three distinct
  entries. ([#48])
- **The same DOI could land under three different cache keys.** DOI
  normalization existed in six copies — four byte-identical — and only the two
  that had been improved handled `dx.doi.org` and a case-insensitive `doi:`
  prefix. So `https://dx.doi.org/10.x/y` and `DOI:10.x/y` were left unnormalized
  by OpenAlex, Crossref, OpenCitations and ACL, producing distinct cache entries
  *and* malformed upstream URLs (`/works/doi:https://dx.doi.org/...`). All six
  also sliced the `doi:` prefix without re-stripping, so a pasted `"doi: 10.x/y"`
  became `" 10.x/y"` and was reported as an unknown identifier. ([#49])
- **Old-style arXiv ids and the `arXiv:` prefix now reach the arXiv namespace.**
  `math.GT/0309136`, `cond-mat.stat-mech/0501001` and `HEP-TH/9901001` fell
  through to `manual` under a canonical key that was *already* arXiv's, so two
  spellings of one paper cached, downloaded and converted twice. `arXiv:` — what
  arXiv's own "Cite as" box prints — was likewise neither stripped nor
  recognised, giving one paper a second cache entry beside the copy a bare id
  had fetched. Stripping now loops and runs before the URL handling, so
  `arXiv:arXiv:…` and `arXiv:https://arxiv.org/abs/…` both collapse. A versioned
  old-style id no longer produces a dead-end `canonical_id` either. ([#86])
- **bioRxiv/medRxiv identifiers are recognised in every rendering of a preprint.**
  A version suffix (`10.1101/2024.01.01.573838v1`) and a content URL carrying an
  uppercase host, no scheme, a trailing slash, or a page tail other than
  `.full`/`.full.pdf` each used to normalize to itself. The uppercase and
  scheme-less spellings then failed `is_biorxiv_doi` and were filed under
  `manual`, so `get_paper_metadata` answered "could not determine the provider"
  for a URL copied out of a browser; the rest became their own cache key and were
  rejected upstream, then negative-cached as `No paper found` for the hour. All
  collapse onto the bare DOI, which is the identity bioRxiv actually mints — it
  issues one DOI for every version, so unlike arXiv the version is deliberately
  not part of the key. ([#96])
- **`get_author` accepts the `openalex.org` URL family, not one spelling of it.**
  `http://`, a `www.`/`api.` host label, an `/authors/` path segment, a trailing
  slash, uppercase and surrounding whitespace all collapse to the bare ID,
  matching the latitude the DOI and arXiv normalizers carry; previously each was
  passed to the API verbatim and cached under its own key. ([#98])
- **A bare `10.18653/v1/` was treated as an ACL Anthology paper.** It names no
  paper, and the empty Anthology ID it produced mapped to an empty filename stem
  — so every such identifier cached as the same `.pdf` and `download_pdf`
  requested `https://aclanthology.org/.pdf`. It now falls through to the
  generic-DOI route. ([#94])
- **Two imported papers could silently overwrite each other's PDF.** Derived
  paths used two different sanitizers: PDFs collapsed unsafe characters to `_`
  (so `"a b"` and `"a_b"` became the same `a_b.pdf`) while markdown replaced only
  `/`. The PDF and markdown caches therefore disagreed about identity. All three
  derived paths — PDF, markdown and the sections key — now share one `safe_stem`,
  which percent-encodes rather than collapses and so is not lossy. ([#48])
- **The startup sweeps no longer orphan or strand a paper.** A stem containing
  `~` was read as pre-`safe_stem` and re-encoded, renaming a correct file to a
  name the path builder never produces; an `arXiv:`-prefixed paper was moved
  under a name the arXiv namespace never reads; and a freeform label that merely
  *looks* like an old-style arXiv id (`thesis_1234567`) was moved out from under
  itself, after which the label resolved to nothing. Ambiguous cases are now
  hard-linked — one inode under both keys — so neither reading loses its file,
  and only a moved markdown drops the stale section index. ([#89], [#108])
- **`find_in_paper` and the four graph tools echo the identifier in canonical
  form.** `arXiv:2301.00001v2` and `2301.00001v2` are one markdown file and gave
  one paper two identities; `10.1234/X`, `doi:10.1234/x` and
  `https://doi.org/10.1234/x` are one paper sharing one cache key, but each was
  echoed back verbatim, so an agent correlating results across calls saw three
  identities for one work. ([#102], [#105])
- **`search_cached_papers` returned `canonical_id`s that chained nowhere.** Every
  non-arXiv/bioRxiv/ACL DOI lands in the `manual` namespace, whose filename
  inversion passed publisher DOIs straight through as
  `10.1038_s41586-021-03819-2`. The registrant slash is decidable (the registrant
  is digits only) and percent-escapes are now decoded, which no namespace did.
  ([#73])

#### Requests that fetched — and cached — the wrong record

- **A `.` or `..` path segment no longer fetches a different resource.** Both
  characters are unreserved, so percent-encoding cannot escape them, and RFC 3986
  removes the segment *after* encoding. The consequences were provider-specific
  and all silent: a reference lookup for `10.1038/a/..` asked OpenCitations about
  `10.1038` and cached its empty answer as that DOI's references for 7 days, and
  shortened Crossref's path to the `/works` collection, whose work-*list* passed
  every shape check; `get_paper_metadata("10.1101/x/..")` hit bioRxiv's
  interval/cursor endpoint and cached the newest preprint in it as the requested
  paper for 7 days; `get_wikipedia_summary("..")` requested `/api/rest_v1/page`
  and cached the endpoint listing as the article for 30 days; and `get_author("..")`
  requested the API root. All are now refused locally, returning the same
  `not_found` a genuine miss does and caching nothing. ([#98], [#99], [#100], [#101])
- **An identifier that normalizes to nothing no longer spends a request or
  poisons the empty cache key.** OpenAlex builds `/works/doi:{doi}`, and the
  `doi:` prefix kept the last path segment non-empty — so the guard passed, the
  request went out, and the 404 was negative-cached under the empty string, where
  every later blank identifier found it. Reachable without a malformed tool call:
  a bioRxiv record whose upstream `published` field is a bare `doi:` is chained
  straight into `get_work` by `follow_published`. A DOI normalizing to a bare
  `10.1101/` had the same effect on bioRxiv, and an OpenCitations `"doi:"` token
  became `{"doi": ""}` — a blank string flattened onto a reference record, where
  it reads as a real identifier to chain the next call onto. ([#99], [#101])
- **Identifiers are percent-encoded into every request path.** A `#` or `?` in an
  author ID, a bioRxiv DOI or an ACL Anthology ID truncated the request and
  fetched a different record — the hazard `get_work` has always encoded against.
  Real Anthology IDs and ORCID URLs are unaffected: every character in them is
  already unreserved. ([#94], [#96], [#98])
- **Every spacing of one Wikipedia title is now one cache entry.**
  `"Cytochrome P450"`, `"Cytochrome_P450"` and `"Cytochrome  P450"` are one
  article to MediaWiki, which collapses runs of spaces and underscores alike;
  they were three cache entries and three requests. Relatedly, a title starting
  with `ß` asked upstream for the wrong article: Python's `upper()` expands it to
  `SS`. ([#100])

#### Crashes that escaped the `{error}` contract

- **Malformed upstream JSON escaped on Crossref and bioRxiv.** Five of seven
  providers guarded the decoded payload with `isinstance`; these two were missed.
  A 200 whose body is `null` or a scalar made `"message" not in data` raise
  `TypeError` and `data.get("collection")` raise `AttributeError` — neither is in
  the parse or transport error sets, so both propagated out of the tool. A
  malformed bioRxiv payload is now distinguished from a legitimately empty one,
  so it stays retryable instead of being negative-cached as "not found". ([#49])
- **An OpenAlex response of the wrong shape crashed the tool that read it.**
  Three values arrive from untyped JSON and were consumed where nothing above
  catches an `AttributeError`/`TypeError`: `get_paper_abstract` on a malformed
  `abstract_inverted_index`, `download_pdf` on a work whose `best_oa_location` /
  `primary_location` / `open_access` is not an object, and `get_papers_metadata`
  on any batch response carrying one record with a non-string `doi` — that one
  took down the whole batch of up to 50. `best_pdf_url` now also returns a
  non-empty string or nothing, so a non-string can't reach the downloader. ([#98])
- **OpenAlex nulls crashed `get_paper_authors`, `get_author`,
  `get_paper_metadata` and `get_paper_bibtex`.** OpenAlex emits
  `"authorships": null`, `"author": null`, `"display_name": null` and
  `"institutions": null` rather than dropping the key, and the work is
  positive-cached before the tool ever sees it — so one such record turned every
  call for that paper into a traceback for the full cache TTL. The `@phdthesis`
  school lookup was the last unguarded read in `bibtex.py`. `get_author`
  additionally drops a non-integer entry from an affiliation's `years` instead of
  letting `sorted` raise on a mixed list. ([#85], [#103], [#107])
- **Non-dict rows inside a valid Crossref response crashed three tools.**
  `get_work` and `search_works` return the upstream `message` verbatim and
  guarantee only that it *is* a dict — nothing types the rows inside it. A bare
  string in `author` or in `reference` reached `.get()` as an `AttributeError`,
  and the wrong-shape body is positive-cached for the full TTL, so every call
  raised until the entry expired. It was also asymmetric:
  `get_paper_references_count` counted the same row happily, so the survey
  pointed the agent at a source that then raised. `_crossref_date` additionally
  raised on a `date-parts` that is not a list and one holding bare ints — it
  null-checked every level but type-checked none. ([#102], [#105])
- **`search_crossref_by_title` and `search_wikipedia` crashed on, or misreported,
  a malformed response.** A Crossref search body whose `items` was not a list of
  objects raised out of the provider; a non-object `message` was reported as an
  empty—but successful—result set, indistinguishable from a query that genuinely
  matched nothing. A Wikipedia body that isn't the 4-element OpenSearch array
  returned `{"results": []}`, and a string where a list of titles belongs zipped
  into per-character "hits" an agent would chain onto. Both are now the uniform
  retryable error; individual malformed hits inside an otherwise valid list are
  skipped rather than fatal. The same class of crash is fixed in the
  OpenCitations records, whose ID field was also assumed to be a string, and in
  a Wikipedia summary whose `content_urls` is not a dict at either level. ([#97], [#100])
- **A superscript year raised `ValueError` out of `search_arxiv`.**
  `"²⁰²³".isdigit()` is `True` and `int()` on it raises, and the resulting error
  matched neither except clause. ([#95], [#105])
- **A full or read-only disk escaped as a raised `OSError` in three places.**
  `cache.put` runs inside every provider's `fetch` closure *after* the network
  request succeeded, so an ENOSPC there threw away data we had just paid an HTTP
  request for; `stream_to_file` caught only transport errors, in the one path
  whose `MAX_PDF_BYTES` cap exists precisely to protect the disk; and
  `import_local_pdf`'s post-copy size read sat outside its `try`. All three now
  return `{error, retryable: True}` — retryable, so a full disk stays out of the
  negative cache rather than being recorded against the paper — and count as
  `cache_write_failures`. ([#56], [#79], [#87])
- **A malformed `PDF_CONVERTER` / `PDF_FAST_CONVERTER` escaped as a raw
  exception.** `str.format` raises `KeyError` on an unknown placeholder,
  `IndexError` on a positional one and `ValueError` on an unbalanced brace — and
  its failure set is open besides: `{input.name}` raises `AttributeError`,
  `{input[0]}` `TypeError`, an absurd width spec `MemoryError`. The fast path had
  no `try` at all. Both builders now raise a named `ConverterTemplateError`
  naming the offending variable and the valid placeholders, and both call sites
  return `{error, retryable: False}`. ([#52], [#93])
- **`find_in_paper` read cached markdown using the host locale.** It was the one
  read path in the pipeline without an explicit `encoding="utf-8"`, so under
  `LC_ALL=C` (containers, systemd units) it raised `UnicodeDecodeError` straight
  out of the tool. It also lacked the `FileNotFoundError` guard
  `get_paper_section` has, so a concurrent `force_refresh` cascade between the
  `exists()` check and the read raised instead of degrading to the "not
  converted" error. ([#52])
- **A non-positive `max_attempts` escaped as `UnboundLocalError`.**
  `get_with_retry` skipped its loop entirely and fell through to an unbound
  `response`; an `UnboundLocalError` is a `NameError`, so it is not in the
  transport error set and would have escaped the provider instead of becoming an
  `{error}` dict. It clamps to one attempt, so a misconfigured `Throttle` degrades
  to "no retries" rather than crashing the tool. `Throttle.__init__` clamps its
  own numeric policy the same way: a typo'd `max_concurrent=0` used to deadlock
  on `Semaphore(0)` with no timeout. ([#78], [#83])

#### Retry verdicts and error classification

- **Transient HTTP errors now carry `retryable: True`, the flag two callers
  already branch on.** `error_dict` set it for local backpressure alone; a 429, a
  5xx, a timeout and a network error each announced themselves as "Transient —
  retry." in prose and carried no machine-readable flag. Both consumers that read
  the key got it wrong: the open-access path answered an OpenAlex timeout with
  "fetch the PDF by hand and call `import_paper`", and `get_paper_references`'
  both-sources-failed response told the agent that "retryable errors are flagged
  `'retryable': true`" while flagging nothing. Other 4xx stay deliberately
  unflagged: `retryable: False` is an explicit "definitive, safe to
  negative-cache" signal, and a paywalled 403 is not something we know that
  about. ([#78])
- **`_RETRYABLE_STATUSES` is the single definition of "transient status".**
  `get_with_retry` read the allowlist while `error_dict` used a `500 <= status <
  600` range, and the two disagreed in both directions: a 501 was advertised as
  transient though the retry helper declines to retry it, and a 408 / 425 —
  which it *does* retry — reached the agent unflagged, as a body snippet. A
  retryable 4xx now reads "temporary rejection (HTTP 408)" rather than being
  mislabelled a server error. ([#78])
- **`Retry-After` reaches the agent, in both RFC 9110 forms, clamped.**
  `error_dict` read the header only inside the 429 branch, so a 503 maintenance
  window advertising `Retry-After: 300` was slept on internally and then
  discarded. Only the numeric form was parsed, so the HTTP-date form that
  Wikimedia- and Cloudflare-fronted endpoints emit fell back to our own
  backoff — as little as 1.0s against a server that had just asked for minutes. A
  naive date is read as UTC rather than local time, non-finite values are
  rejected, and the 600s ceiling the internal retry path always honoured now
  applies to the surfaced value too, so a misconfigured `Retry-After: 86400` no
  longer tells an agent to wait a day. ([#50], [#78])
- **A definitive miss carries `not_found` on every provider.** Crossref,
  OpenCitations and bioRxiv were the exceptions;
  `get_paper_references{,_count}` and `get_paper_citations` forward the flag, so
  a source reporting nothing can finally be told apart from a source that was
  briefly unreachable — previously indistinguishable for exactly the two
  providers the reference-graph tools use. arXiv's HTTP 404 branch also stopped
  caching a different payload from its other two "not found" shapes.
  ([#95], [#96], [#97])
- **A transient failure on the open-access download path was cached as permanent
  for 24 hours.** The classifier asked `retryable is not True` — a denylist —
  while `error_dict` set `retryable` on exactly one of its six branches, so a
  timeout, a connection reset, a 503 and a 429 were all classified permanent and
  negative-cached, stranding the identifier behind a stale "no open-access copy
  exists". The same polarity bug ran the other way on the recovery hatch: an
  OpenAlex 403 or 451 was reported as a permanent dead end when a retry would
  have fixed it, while a genuinely definitive download failure — a publisher 404,
  a landing page served under a `%PDF-` promise — was missing the hatch it needed.
  Both classifiers are now allowlists on an explicit `retryable: False`.
  ([#65], [#90])
- **A PDF that 404s no longer re-hits the upstream on every `download_pdf`
  call.** Only the open-access path negative-cached its download failures; arXiv,
  bioRxiv and ACL Anthology cached nothing, so a withdrawn paper or a wrong
  Anthology ID cost a request every single time an agent retried. All four now
  share `cached_download` — the file-artifact sibling of `cached_lookup` — with
  per-provider TTLs: 1h for the preprint servers, which render PDFs lazily, and
  24h for the static CDNs. `download_pdf`'s 404 also carries `retryable: False`,
  which it previously lacked. ([#65], [#68])
- **A batch no longer files a live DOI as "not found".** Any requested DOI absent
  from `results` was cached as not-found for 24h — including one whose
  OpenAlex-stored DOI string merely differed from the request, and with no check
  of `meta.count` against the number of records returned, so a truncated response
  poisoned the rest of the chunk. The same gap covered a record that was
  unreadable or carried no DOI at all. A missing DOI is now only definitive when
  the response accounted for everything; otherwise the chunk's misses come back
  `retryable` and uncached. Records returned under an unexpected DOI are cached
  under their own key instead of discarded. ([#49], [#98])
- **A bioRxiv "not found" now requires both servers to have answered.** Nothing
  in the shared `10.1101/` prefix tells bioRxiv from medRxiv, so `get_paper` asks
  both. If one returned an anomalous body and the other a well-formed empty
  result, the miss was still negative-cached as definitive — filing a live
  preprint as absent for the hour on half the evidence. ([#96])
- **A malformed `search_arxiv` query came back as a search hit.** arXiv answers a
  bad `search_query` with HTTP 200 and a synthetic entry whose id points at
  `api/errors`; `search_papers` parsed it as a normal paper, so the agent
  received `{arxiv_id: "http://arxiv.org/api/errors#...", title: "Error"}` and
  chained its next call onto that. It is now a structured
  `{error, retryable: false}`. The tool also stopped attaching a "wait for the
  outage" suggestion to an error the provider had already classified
  non-retryable. ([#95], [#105])
- **`published_lookup_retryable` no longer promises a retry that cannot
  succeed.** When `follow_published=True` falls back to the preprint record, the
  tag is now set only for errors OpenAlex actually reports as retryable (5xx,
  429, timeouts). It was set for anything that was not a definitive 404, which
  swept in unclassified 4xx — an OpenAlex `403` told the agent to keep retrying
  the chain. ([#103])
- **Two graph errors that carried no retry verdict now carry one, and a non-ACL
  DOI is refused definitively.** `page > 1` with `source="auto"` is
  `retryable: False` — re-issuing the identical call can never help — and the
  both-sources-failed envelope gained a top-level `retryable` that is true when
  either nested source might answer on a retry. `download_pdf`'s rejection of a
  non-ACL DOI carried neither `not_found` nor `retryable`, and every consumer
  that classifies an error reads an unflagged error as transient. ([#101], [#102])
- **Multi-source errors lost their retry payload.** `_source_error` forwarded
  only `error` / `retryable` / `suggestion`, discarding `retry_after_seconds`,
  `backpressure`, `max_concurrency` and `not_found` — exactly the fields that let
  an agent tell a definitive miss from a transient one, which is what the
  helper's own docstring says it exists to preserve. ([#51])
- **A `Throttle`'s `label` reaches the agent on backpressure.**
  `LocalBackpressureError` carried it and `error_dict` ignored it, building the
  message from its own argument instead. Every provider's `label` matches its
  call-site literal, so behaviour is unchanged today — but a new provider that
  set the two differently would have found it silently discarded. ([#78])
- **`force_refresh` never reached the Crossref fallback.**
  `get_paper_metadata(doi, force_refresh=True, fallback_crossref=True)` served a
  stale cached Crossref record, in the one path that exists specifically for
  brand-new DOIs. ([#51])
- **Failure responses now carry a `suggestion` naming a next step.**
  `download_pdf` was the one pipeline tool that could hand an agent a dead end —
  a withdrawn arXiv paper, a 503 and a size-cap abort all arrived with a retry
  verdict and no next step. `convert_paper` answered one catch-all — "the PDF may
  be too large, corrupted, or in an unsupported format" — to three failures that
  contradict it: a `mode='fast'` timeout (now pointed at `mode='full'`, which has
  a far longer budget), a missing fast extractor (an operator fix) and a PDF
  unlinked mid-call (re-download it). `import_paper`'s unsupported-extension
  advice moved out of the `error` prose and into `suggestion`, where agents
  branch. ([#104])

#### Corpus search

- **`search_cached_papers` ranking no longer degrades as the index ages.** The
  FTS5 tables are contentless, so SQLite cannot decrement their corpus statistics
  when a document is deleted — every re-converted or removed paper permanently
  inflated the document count and average length that `bm25()` divides by. Left
  to accumulate, a rare query term's relevance converged with a common one's: in
  a 100-paper corpus, 40 forced refreshes pushed every paper containing the rare
  term out of the top six entirely. The statistics are now reset on
  `force_refresh` and whenever replacements plus removals reach the corpus size,
  which bounds drift instead of letting it compound. ([#107])
- **A non-Latin or accented query returns the documents the index holds.** The
  move to FTS5 left the query side gating on an ASCII-only regex from when this
  module built its own index, so a query of purely non-Latin words tokenised to
  nothing and returned early even though a raw `MATCH` against the same index
  found the paper, and "Gutiérrez" was split into `guti OR rrez`, which matches
  nothing. Queries are now tokenised by FTS5, the same way the corpus is. A Greek
  word ending in a capital sigma is searchable again too:
  `"ΟΔΥΣΣΕΥΣ".lower()` ends in `ς`, not `σ`, but the offset-mapping helper
  lowercased character by character and emitted `σ`. ([#55], [#56], [#82])
- **Stopwords leaked into the FTS5 `MATCH` expression.** Building it from raw
  words dropped the stopword and single-character filter along with the ASCII
  one. FTS5 indexes the raw markdown under `unicode61`, which strips neither, so
  `search("the transformer")` OR-ed in a term matching essentially every document
  and returned papers with no connection to the query. ([#56])
- **A query that repeats a word in another case no longer double-counts it.**
  Terms were deduplicated on the raw word, so `"Attention attention"` built
  `"Attention" OR "attention"` — two spellings FTS5 tokenises identically — and
  BM25 counted the term twice. The same query typed in one case ranked
  differently from the other. ([#86])
- **Namespace-filtered search no longer changes a paper's score, and no hit
  rounds to zero.** Corpus statistics were scoped to the filtered subset, so the
  same paper scored differently in a filtered and an unfiltered search; term
  rarity is now computed over the whole index, and `namespace` selects which
  documents come back, not how they are ranked. Scores are rounded to six
  *significant figures* rather than decimal places: FTS5 clamps a degenerate IDF
  to `1e-6` and BM25 length normalisation scales it down, so a long paper in a
  small corpus scored around `5e-08` and was reported as `0.0`, against the
  stated invariant that every returned hit scores above zero. Equal-scoring hits
  are ordered deterministically by `(namespace, canonical_id)`. ([#55], [#86])
- **The documented `search_cached_papers` → `get_paper_section` chain failed for
  repeated headings.** Corpus search returned the matched section's *title*, and
  the docstring told the agent to chain it into `get_paper_section` — which
  rejects a repeated title as ambiguous. Measured on a real 2,493-paper corpus,
  **271 (10.9%) have at least two sections sharing a title**, so the documented
  workflow dead-ended roughly one time in nine. Hits now carry `section_index`
  and `char_offset`. ([#54])
- **A paper with no headings was indistinguishable from a one-section paper.**
  Converter output without markdown headings — `pdftotext`'s layout mode,
  notably — collapses to a single synthetic `"Preamble"` section. On a real
  corpus **every** single-section paper above 100 KB was this case: theses and
  long preprints, where blind paging is the worst available reading strategy.
  Responses carry `sections_detected` plus a `sections_note` pointing at
  `find_in_paper` and at re-running with `mode="full"`. Imported markdown was
  exempt from the check because it assembled its own cache entry without the
  flag, and the reader accepts any entry whose checksum matches — so the missing
  flag was never recomputed and the tool reported `true`. Every sections-cache
  entry now has one writer. ([#54], [#66])
- **Papers the index cannot use are reported instead of vanishing — and papers it
  *can* use are no longer reported.** A file yielding no terms was dropped from
  the index and became permanently invisible with no error and no diagnostic;
  those are now surfaced as `unindexable_count` / `unindexable`. The probe that
  decides this was itself ASCII-only, so a paper in Japanese, Cyrillic or Greek
  was recorded as `no_indexable_tokens` even though `unicode61` had indexed it
  perfectly well — telling the agent to fall back to `find_in_paper` on a paper
  `search_cached_papers` would have found. The probe now tests for any Unicode
  letter or digit, and `unindexable_note` is built from the `reason` actually
  recorded per file, so an I/O failure is no longer blamed on encoding. ([#52], [#58], [#66])
- **A busy search index is no longer deleted, and an unreadable one is reported
  rather than answered as an empty corpus.** `_connect` treats a database it
  cannot open as corrupt and unlinks it, which is right for "file is not a
  database" — but `sqlite3.OperationalError` is a *subclass* of
  `sqlite3.DatabaseError`, so the same handler answered a healthy, momentarily
  busy index by destroying it. The query wrapper's `except OperationalError:
  return []` was written for FTS5 syntax errors, but every query word is a quoted
  phrase, so what it actually swallowed was "database is locked" / "no such
  table" — answering "which paper mentioned X?" with a confident "none".
  `search_cached_papers` now returns `{error, retryable, suggestion}`, and also
  catches `OSError`, which `sqlite3.Error` does not cover. ([#86])
- **An unreadable schema version rebuilds the index instead of certifying it.**
  `_ensure_schema` dropped the old tables only when it had read a version that
  *differed*; a value it could not parse came back as "unknown", skipped the
  drops, and then stamped whatever tables were on disk with the current version,
  so a stale schema was silently declared current and never rebuilt again. ([#86])
- **A paper that was briefly unreadable is retried instead of frozen out.** The
  refresh recorded the `(mtime, size)` that *succeeded* alongside the failure, so
  the staleness check matched forever: a lock that cleared, or a `chmod` (which
  leaves mtime alone), was never noticed and the paper stayed unindexed until
  `force_refresh`. ([#86])
- **A snippet the scan cannot centre now says so.** `unicode61` treats `_` as a
  separator while Python's word boundary counts it as a word character, so
  `attention_model` is findable but not locatable; the hit reported `section` and
  `char_offset` from offset 0. Both are now `null`. ([#86])
- **`find_in_paper` no longer reports an empty `match` for a partial ligature
  hit, and `get_paper_section` finds an accented heading from its ASCII
  spelling.** With `normalize=True`, a query matching only part of one original
  character's expansion (the `f` of a folded `ﬁ`, the `1` of a folded `½`)
  resolved both ends of the span to the same original index and sliced nothing. A
  title lookup compared raw lowercased substrings, so `"Resume"` missed a
  `"Résumé"` heading and came back listing every section; it now falls back to a
  folded comparison, but only when the exact pass finds nothing, so a paper
  carrying both spellings still resolves each one. ([#82])

#### PDF pipeline and conversion

- **A paper's section index could permanently describe a different document.**
  `store_markdown_and_index` wrote the markdown and then re-read the file to
  checksum it. Full-mode `convert_paper` is the one markdown writer that holds
  only the global conversion lock and never the per-paper `sections_lock` — which
  `import_paper` does hold for the same path — so a write landing in that window
  left an entry holding document X's sections under document Y's checksum. A
  matching checksum is exactly what suppresses a re-parse, so the wrong titles
  were served for that paper indefinitely. The checksum now comes from the text
  that was parsed, making the entry correct by construction. `import_paper`'s PDF
  branch also takes the sections lock, which it skipped while
  `_invalidate_derived` unlinked the markdown in a worker thread. ([#73], [#91])
- **`convert_paper` could corrupt the text of a converted paper.** The rewrite
  that strips unresolvable image paths stopped at the first `)` *inside* the
  path, so `![Figure 1](fig(1).png)` became `![Figure 1]().png)` — the tail read
  as body content by every downstream reader, including corpus search. Converter
  filenames derive from the PDF stem, and an Elsevier-PII DOI carries
  parentheses. A caption containing brackets was skipped entirely, leaving a path
  into the deleted extraction directory in agent-visible markdown. ([#91])
- **`conversion_mode` was mislabelled and could be erased.** Re-reading an
  already-converted paper whose entry predates the field — `null`, meaning nobody
  knows what produced it — relabelled it `"fast"` and wrote that claim to disk
  permanently, while `mode="full"` answered `null` for the identical state. And
  `get_paper_sections(force_refresh=True)` dropped the cached entry before
  reading it, so the re-parse had no provenance left to preserve and wrote
  `null` — a claim a refresh must not be able to manufacture, since the markdown
  is untouched by one. ([#91], [#93])
- **Which markdown file became the paper was nondeterministic.** MinerU emits
  several `.md` files per run, and both the exact-stem lookup and the fallback
  used `list(glob(...))[0]` — filesystem order. Both are now sorted, and one
  ordering governs both passes: previously only the fallback sorted
  shallowest-first, while the first pass sorted on the whole path and so
  preferred a nested file whenever the directory name sorted before the stem.
  ([#56], [#93])
- **The converter's stderr was captured inconsistently.** The command was run as
  `{cmd} 2>&1` with `stderr=PIPE`, which was worse than merely redundant: `;` and
  `&&` bind tighter than the redirect, so streams were merged only for a
  *single-command* converter. With `PDF_CONVERTER_VENV` set, the redirect
  attached to whatever the last command happened to be. Whether a converter's
  error reached the agent depended on the shape of the operator's converter
  string. Stderr is now captured on its own pipe and appended last, so a
  converter that keeps logging after it fails can no longer push its own error
  out of the 500-character window. ([#56])
- **An empty conversion produced the literal error `out of range (0--1)`.** A
  converter can exit 0 having written an empty markdown file — a 0-page PDF, an
  image-only scan — and every section read fell through to the range check. It
  now says the markdown is empty and suggests the likely cause (no extractable
  text layer). ([#56])
- **A cancelled conversion orphaned the converter process.** `convert_pdf` and
  the fast path caught only `TimeoutError`, so on `CancelledError` (client
  disconnect, tool-call cancellation, server shutdown) the `finally` removed the
  extraction directory and released the global conversion lock while the
  subprocess was never signalled — a MinerU run kept pinning CPU/GPU with its
  output directory deleted underneath it, and the child was never reaped. Both
  paths now tear the process group down and re-raise. ([#52])
- **A zero-length HTTP 200 installed a permanent 0-byte PDF.** With no chunks the
  streaming write loop never ran, the `%PDF-` sniff never fired, and the atomic
  rename installed an empty file reported as a successful download. Every
  downstream `exists()` then treated it as cached forever and `convert_paper`
  handed it to the converter. `stream_to_file` now rejects an empty body, and the
  three native PDF providers plus `convert_paper` validate cached files through
  the shared `is_usable_pdf` (non-zero size + `%PDF-` header) instead of a bare
  existence check. ([#48])
- **An unreadable directory under `.cache/` stopped the server from starting.**
  The startup sweep guarded only the rename itself, so a `PermissionError` from
  listing a cache subdirectory propagated out of the FastMCP lifespan. It is
  documented as best-effort and now behaves that way. Relatedly, the sweep used
  to rename every file under `pdfs/` and `markdown/`, including another
  process's in-flight `.tmp`, whose stem still carries the destination's legacy
  characters — making that writer's atomic rename fail. It now only touches
  `.pdf` and `.md`. ([#91], [#92])
- **`_atomic_copy` no longer backdates its in-flight temp file.**
  `shutil.copystat` carried the source's mtime onto the temp before the rename,
  so importing a PDF older than the orphan-`.tmp` cutoff presented the sweeper
  with a live temp that read as an orphan; a sweep racing the copy unlinked it
  and `import_paper` failed with a `FileNotFoundError` from the rename. The
  destination now takes its own write time and only the source's mode. ([#87])
- **`approx_tokens` disagreed between the two tools that report it.**
  `get_paper_sections` measured the *unstripped* line join, so every section's
  estimate was inflated by its surrounding blank lines and `total_approx_tokens`
  summed the inflated variant, while `get_paper_section` measured the stripped
  text it actually returns. An agent budgeting context from the index got a
  different number than the read produced. ([#53])
- **The "PDF not found" error printed an absolute cache path.** No cache
  filesystem path is supposed to cross the MCP boundary, and the tool layer's
  scrubber only drops path-valued *keys*, not a path embedded in an error
  message. ([#93])

#### BibTeX

- **Author fields were never escaped**, so an author or organisation containing
  `&`, `%`, `$`, `#` or `_` (`AT&T Labs`, `Sanofi-Aventis R&D`, and every
  OpenAlex org-authorship) produced a `.bib` that fails to compile with
  *"Misplaced alignment tab character &"*. Title, journal and DOI were escaped
  from the start; the author path was not. DOIs interpolated into
  `howpublished={\url{...}}` were also raw — an unescaped `%` there comments out
  the rest of the file. `_escape_doi` now covers `{`, `}`, `\`, `$`, `~` and `^`
  as well, in a single pass so a backslash's own escape braces are not
  re-escaped. ([#48])
- **Conference papers render as `@inproceedings` again.** `_TYPE_MAP` was keyed
  on Crossref's work vocabulary, which OpenAlex replaced in 2023 —
  `type_crossref` is gone from the work object entirely, so `proceedings-article`,
  `posted-content`, `monograph` and `proceedings` could never match, and the 16M
  works OpenAlex types as `conference-paper` all fell through to `@misc`, losing
  the `booktitle`. The map now follows OpenAlex's own vocabulary and covers the
  types added since. ([#85])
- **A `doi=` field or a `howpublished` link could carry a resolver URL or a
  broken escape.** `generate_bibtex` stripped OpenAlex's DOI with a local
  `startswith("https://doi.org/")` test, so an older record served over plain
  http emitted `doi={http://doi.org/10.x/y}`, which every BibTeX style renders as
  a doubled resolver link; all three generators now route the provider's DOI
  through the shared normalizer. `\url{}` takes verbatim catcodes from url.sty,
  so the backslash escape the field used to carry landed in the link target
  itself — URLs are percent-encoded now, which the DOI resolver decodes, while
  the `doi=` field, which is prose, keeps its backslash escapes. A non-arXiv
  preprint's `howpublished` emits `https://doi.org/<normalized>`, falls back to
  the OpenAlex landing page, and emits nothing when there is neither. ([#77], [#85])
- **An old-style arXiv DOI keeps its archive path.**
  `10.48550/arXiv.hep-th/9901001` yielded `eprint={9901001}`, because the id was
  read as the last `/` segment. Matching the `10.48550/arxiv.` prefix keeps
  `hep-th/9901001`, and also stops a DOI that merely contains "arxiv" in its
  suffix from being rendered as an eprint. ([#85])
- **Numeric and special-character `biblio` values.** An integer `first_page`
  raised `TypeError` mid-render, and volume/issue/pages reached the entry
  unescaped, so a volume of "1 & 2" broke the whole `.bib`. All three are escaped
  like any other prose field now, and whitespace runs inside a field collapse to
  one space — an Atom-wrapped `journal_ref` used to split the entry's
  one-field-per-line layout. ([#85])
- **A leading number no longer becomes the citation key's title word, and a name
  that escapes to nothing no longer crashes the author field.** "3D Shape
  Analysis" keyed on `d`, the digit having been split off by the word regex; it
  keys on `3d` now, while a wholly numeric token ("100 Years of…") is skipped as
  undistinguishing. A display name of `"{"` passes the blank check and then
  escapes to the empty string, which reached the surname walk with no tokens;
  blank formatted names are dropped where the joining happens, so one bad entry
  can't emit a dangling ` and ` either. A null `publication_year` printed the key
  `smithNonesome`, and now routes through the same ASCII-digits gate the word
  components use. ([#85])
- **`get_paper_bibtex` no longer implies `eprint` / `archiveprefix` are
  `@misc`-only.** An arXiv paper with a `journal_ref` renders as `@article` and
  carries them alongside `journal`; the entry type turns on `journal_ref` alone.
  ([#108])

#### Concurrency and shutdown

- **A cancelled single-flight call took down its neighbours, in both
  directions.** Cancelling a task cancels the future it is suspended on, and for
  a follower that is the *shared* future — so one follower giving up cancelled
  the slot for everyone: the leader then called `set_result` on a cancelled
  future and raised `InvalidStateError` into its own caller in place of a
  perfectly good result, while every remaining follower re-ran the factory and
  issued a second outbound request for a key already in flight. The documented
  fan-out is four parallel calls for one paper, so a cancellation lands on a
  follower three times in four. The mirror case — a cancelled leader carrying
  `CancelledError` to every follower — is fixed too: a follower that is not
  itself being cancelled takes over and runs the factory. Genuine failures are
  still shared. ([#53])
- **Shutdown's per-client close timeout was not a bound.** `aclose_all` wrapped
  each `client.aclose()` in `asyncio.wait_for`, which cancels the coroutine on
  timeout and then *awaits it* — so a teardown that keeps awaiting past
  cancellation (a TLS shutdown hanging on the peer, the case the timeout exists
  for) pinned the FastMCP lifespan for as long as it liked. It also closed them
  one at a time, so eight wedged sockets could take up to 40 seconds. The whole
  set is now bounded once by `asyncio.wait`; stragglers are cancelled and
  deliberately left unawaited, since the process is exiting and the kernel reaps
  the socket. Cancellation of the shutdown itself now propagates rather than
  being reported as a clean close. ([#53], [#76])
- **`get_works_batch` rejoins the shared getter protocol.** It had no
  single-flight at all, so two concurrent `get_papers_metadata` calls with
  overlapping DOI lists both issued full batch GETs; chunks are now coalesced on
  their sorted contents. The chunk loop was also a serial `for ... await`,
  issuing four requests one after another while three of OpenAlex's four
  concurrency slots sat idle. Results also come back in the order they were asked
  for, rather than cache hits first, then singleton fallbacks, then batched
  chunks. ([#49], [#98])

#### Politeness and observability

- **Crossref was requesting at polite-pool rates without a polite-pool
  identity.** The rate constants were hardcoded to the polite tier
  unconditionally, while the `User-Agent` carrying `CROSSREF_MAILTO` was set only
  when one was configured. So the documented default — an empty `.env`, which the
  README calls a supported starting point — requested at **2x the public-pool
  rate, 3x its concurrency and 10x its search rate**, while identifying itself as
  `python-httpx/x.y`. The tier is now resolved from whether a contact address is
  present, and a whitespace-only `CROSSREF_MAILTO` no longer claims the pool: `" "`
  was truthy, so the client took the polite tier while the address was correctly
  dropped, sending those requests with no contact at all. Search is also paced
  separately, since Crossref limits it far more tightly than singleton lookups.
  ([#50], [#88])
- **Three providers and the open-access download path sent no `User-Agent` at
  all.** `biorxiv`, `opencitations`, `acl_anthology` and the OA path passed no
  headers, so they went out as `python-httpx/x.y` — the generic agent several
  upstreams throttle hardest. ACL was the least polite configuration in the tree:
  four parallel anonymous PDF pulls at zero gap from a nonprofit. The four agents
  that *were* sent advertised a repository URL that does not exist and a version
  hardcoded to `1.0`. All eight now share one builder emitting the real
  repository URL and the real package version — which is also cached, since the
  lookup ran an `importlib.metadata` `sys.path` scan on every outbound call.
  ([#50], [#84])
- **A malformed `*_MAILTO` broke every outbound request.** A stray CRLF injected
  a header that `httpx` accepted at construction and rejected at send time,
  degrading every request to a misleading "network error" dict; a non-ASCII
  address raised `UnicodeEncodeError` inside `httpx` at client construction,
  outside the caught set, and crashed uncaught; a whitespace-only value emitted a
  bare `mailto:`. The contact is now scrubbed to printable ASCII minus parens and
  stripped of a redundant `mailto:` prefix. ([#84])
- **The per-host prune dropped a host that was still owed a wait.** The age sweep
  bounding the last-start map is documented as semantics-preserving, but it was
  handed the caller's *reserved* start (`now + wait`) rather than the real clock,
  so it swept live entries too. Past the tracked-host ceiling, an open-access
  download could start a request early against a host it had just fetched from.
  The gap check also read a recorded start of `0.0` as "never seen", so a
  monotonic clock reading zero skipped one gap. ([#83])
- **The counters an operator reads were wrong in both directions.** `http_calls`
  was incremented once per throttle *slot*, but a slot issues up to
  `retry_attempts` real requests — under-reporting outbound volume by up to 3x
  for arXiv, which is exactly the number a politeness audit reads. `cache_misses`
  was booked by `cache.get` whenever no *positive* entry was found, so every
  negative-cache hit was also billed a miss (a known-bad DOI queried 100 times
  reported 100 misses despite one upstream call), the double cache check inside
  `cached_lookup` billed a genuine miss twice, and every read of purely local
  data — the sections index — inflated both counters. `cache_hits`,
  `negative_hits` and `cache_misses` now partition served lookups instead of
  overlapping. ([#50], [#87], [#93])
- **A provider no longer has to be registered in two hand-maintained lists to be
  visible, and a second throttle on one provider is no longer hidden.** The
  stats module matched the attribute literally named `_throttle` and *assigned*
  `in_flight` per namespace instead of summing, so a module holding two throttles
  would have reported one; the roster of provider modules was hand-maintained in
  two places, so a provider missing from either reported no in-flight count or
  leaked throttle state between tests. Throttles are now discovered by scanning
  imported modules, in-flight is filed under the throttle's own `namespace`, and
  `snapshot()` no longer imports a provider as a side effect of sampling it.
  ([#81], [#83])
- **`DEBUG_REQUESTS` and `ENABLE_DEBUG_TOOLS` parsed truthiness separately.** Two
  copies of the same `in ("1", "true", "yes", "on")` tuple, either of which could
  drift; the stats module also read `os.environ` directly rather than through
  `config`, so a `DEBUG_REQUESTS` set in `.env` depended on some other module
  having loaded it first. ([#81])

#### Configuration and startup

- **`.env` was silently ignored for an installed wheel.** The path was resolved
  as `<package>/../../../.env`, which is the project root from a source checkout
  and meaningless from `site-packages` — so every environment variable was
  dropped in a mode the project explicitly supports (`pyproject.toml` ships a
  console script; `.env.example` tells operators to set `CACHE_DIR` "when running
  from an installed wheel"). Resolution now tries `ACADEMIC_TOOLS_ENV_FILE`, the
  project root, `$PWD/.env`, then `$XDG_CONFIG_HOME/academic-tools-mcp/.env`.
  ([#51])
- **A `.env` that isn't UTF-8 no longer crashes the server at startup.**
  `load_dotenv` raises `UnicodeDecodeError`, which is not an `OSError`, so a file
  saved as UTF-16 escaped the guard meant to skip unreadable candidates and
  aborted the import — the console script would not start. It is now skipped like
  any other unusable candidate, as are a deleted working directory and an
  unresolvable home directory. ([#88])
- **`.cache/` and `.env` are found by name, not by counting directories.** Both
  were resolved with `Path(__file__).parents[n]`, which silently changes meaning
  when the module holding it moves. Both now walk up to the package directory, so
  they are correct at any depth. Only reachable on this release's layout change,
  but the failure mode is worth naming: a cache root pointed one directory off
  orphans every artifact already in it, without an error. ([#106])
- **`MAX_PDF_BYTES=-1` silently disabled the size cap**, since the resolver
  treated any value `<= 0` as "disabled" — so the `-1`-means-unlimited idiom from
  other tools, or a typo, removed the disk guard with no signal. Negative values
  now fall back to the default. A non-finite `PDF_CONVERT_TIMEOUT` no longer
  reaches `asyncio.wait_for` either: `float("nan")` is neither `> 0` nor `<= 0`,
  so it passed both branches of the resolver and became a deadline no elapsed
  time could satisfy. ([#79], [#88])

#### Agent-facing documentation

- **`download_pdf`'s docstring told the agent the opposite of what the code
  does.** It stated that re-downloading does *not* invalidate converted markdown
  and that `force_refresh` must be passed to `convert_paper` too — but the cascade
  has been implemented all along, and both the server instructions and the design
  rules describe it correctly. The one string the model actually reads at call
  time was the only wrong one, and it induced a redundant multi-minute
  conversion. Its `force_refresh` description also claimed to be a no-op on
  imported papers: because an import is stored under the identifier's *own*
  provider namespace, a forced re-download of an arXiv/bioRxiv/ACL-shaped
  identifier replaces the PDF you supplied and drops its markdown. ([#51], [#108])
- **Four tool docstrings promised a `suggestion` key the unknown-identifier error
  never carries.** `get_paper_metadata` / `_authors` / `_abstract` / `_bibtex` all
  said an unresolvable identifier returns `{error, suggestion}`; it returns
  `{error}` alone, with the advice inside the message. They now distinguish that
  from a provider failure, which does carry `suggestion`. The same overstatement
  was in `README.md`. ([#110], [#112])
- **`get_paper_references` claimed `retryable` is always present on a tool-layer
  error.** It is absent on a definitive miss (`not_found: true`) and on an
  unclassified 4xx, so an agent branching on its absence read a permanent failure
  as retryable. The docstring now states the three-state verdict and says to
  branch on `retryable is true`. ([#110])
- **The OpenCitations "no edges in this index" caveat reached no agent.** It was
  in `README.md` and `CLAUDE.md` but in none of the graph tool docstrings, so
  `total: 0` / `count: 0` read as "this paper cites nothing". It is now stated in
  `get_paper_references`, `get_paper_citations` and
  `get_paper_citations_count`. ([#110])
- **`get_paper_references`' `source="auto"` is documented as what it does.** The
  parameter description an agent reads, and `README.md`, both said auto "picks
  the one with more references"; it is deliberately biased toward Crossref, so
  OpenCitations wins only on a materially larger list. Behaviour is unchanged —
  the docs were wrong, not the code. ([#102])
- **`search_crossref_by_title` does not warm what `get_paper_metadata` reads.**
  It warms the Crossref namespace while dispatch sends plain DOIs to OpenAlex, so
  the "free cache hit" promise was wrong in `README.md` and in the
  `instructions=` string every agent loads. ([#73])
- **The server's `instructions` no longer contradict `download_pdf` and
  `convert_paper`.** The connect-time preamble told agents flatly that a PDF
  outside arXiv/bioRxiv/ACL must be fetched by hand, which stopped being the whole
  rule when `allow_oa_url` shipped, and it described the PDF pipeline as if
  `convert_paper` had a single backend — `mode="fast"` went unmentioned. An agent
  that took the preamble as a rule had no reason to look for either
  parameter. ([#107])
- **`convert_paper`'s docstring said full conversion takes "up to 10 minutes".**
  `PDF_CONVERT_TIMEOUT` defaults to 30 minutes, so an agent reading the tool
  description gave up or downgraded three times too early. It now names the knob
  instead of a number. ([#104])
- **`get_server_stats` describes the shape it actually returns.** The counters
  were listed as though they were top-level; they are nested under a `providers`
  key, keyed by *cache namespace* rather than provider name (`acl_anthology`,
  `oa_download`), and a counter is absent from a row until first incremented — an
  agent could not tell absent from zero. ([#108])
- **`fallback_crossref` no longer describes a "reduced field set".** A Crossref
  fallback returns the same keys as an OpenAlex response with the four
  open-access fields set to null, so an agent feature-detecting `oa_url` found it
  present and read the null as "not open access". ([#108])
- **Response and error keys that no docstring named.** `download_pdf`'s
  `retry_after_seconds` / `backpressure` / `max_concurrency` / `size_bytes` /
  `cached` and ACL's `anthology_id` / `pdf_url`; `import_paper`'s
  `cascaded_invalidated`; `get_paper_section`'s out-of-range and empty-markdown
  errors; `get_paper_authors`' `author_count` covering the whole list, not the
  page; `get_author`'s `top_topics` element shape; the graph tools' forwarded
  provider-error keys; `get_wikipedia_summary`'s `pageid`; and
  `search_cached_papers`' real tie-break (`ORDER BY bm25(), ns, stem`, not
  "first-seen file in alphabetical order"). `get_paper_sections` also documented
  a `preview` field that does not exist — it is `h3s` — and
  `get_wikipedia_summary` described `extract` as a list on a disambiguation page,
  which nothing in the stack can produce. Roughly fifty further claims across
  `net/`, `store/`, `download/`, `providers/`, `papers/` and `tools/` had drifted
  from the code they annotate and were corrected in place.
  ([#51], [#104], [#108], [#110], [#112])
- **`find_in_paper` and `search_cached_papers` gave a chain snippet that fails
  validation.** `get_paper_section`'s `section` parameter is a string, so passing
  the integer `section_index` is rejected by pydantic; both snippets now show
  `str(section_index)`. ([#110])

## [2026.09.04] — 2026-09-04

### Fixed

- `download_pdf(..., allow_oa_url=True)` no longer fails with `Attempted to
  access streaming response content, without having called read()` when the
  open-access URL returns a 4xx other than 404. `error_dict` builds the 4xx
  message from `response.text`, which is unavailable on an unread streaming
  response; the resulting `httpx.ResponseNotRead` subclasses `RuntimeError`
  rather than `HTTPError`, so `except HTTPX_ERRORS` did not catch it and it
  propagated out of the download, hiding the real status. `stream_to_file` now
  reads the body before `raise_for_status()` on an error status, and
  `error_dict` degrades to a placeholder snippet if a caller has not. Callers
  now see e.g. `OA download HTTP 403: <body snippet>`. Success responses are
  still streamed and never buffered. ([#44])

## [2026.06.04] — 2026-06-04

### Fixed

- arXiv metadata/search/PDF requests now send a **descriptive `User-Agent`**
  instead of the default `python-httpx/x.y`. arXiv's Fastly edge throttles
  anonymous library traffic far more aggressively — returning HTTP 429/503 on
  modest bursts — which surfaced to agents as "couldn't get arXiv metadata"
  errors. The new optional `ARXIV_MAILTO` env var is appended as a contact when
  set; the descriptive UA is sent even when it's blank. ([#43])
- arXiv requests now retry **twice** (three attempts total) instead of once.
  arXiv's edge returns 429/503 with no `Retry-After` when an IP is briefly
  penalty-boxed, where a single retry tended to land in the same cooldown
  window. ([#43])

### Changed

- `get_with_retry` now backs off **exponentially** across attempts
  (`backoff_seconds`, then 2×, 4×…, capped at the 10-minute ceiling). At the
  default of one retry this is byte-for-byte unchanged; it only widens the gap
  for providers that opt into more attempts (currently arXiv, via a new
  per-provider `Throttle(retry_attempts=…)` knob). ([#43])

## [2026.05.31] — 2026-05-31

### Removed

- `get_paper_citations` no longer accepts the inert `source` parameter. It was
  reserved for a future second incoming-citation provider but was never read —
  OpenCitations is the only source of incoming citations — so passing it had no
  effect. The matching unused `CITATION_SOURCE` type was dropped too.
  (`get_paper_references` keeps its `source` param, which has three live values.)
  ([#40])

### Security

- arXiv API responses are now parsed with `defusedxml` instead of the stdlib
  XML parser, so a hostile entity-expansion ("billion laughs") payload is
  refused rather than expanded. arXiv is a trusted source, but this closes a
  denial-of-service vector if a response is ever spoofed or corrupted in
  transit. ([#30])
- The PDF converter subprocess is now hardened against shell injection via a
  paper identifier. The built-in converter command templates hand-quoted
  `"{input}"` with double quotes, which do not neutralise `$`, backticks, or an
  embedded `"`; combined with manual-namespace filenames that only stripped `/`
  and `:`, an exotic identifier could smuggle shell metacharacters into the
  `bash -c` conversion command. Two layers now defend this: `{input}` /
  `{output_dir}` / `{python}` and the venv-activate path are substituted
  **shell-quoted** (`shlex.quote`), and canonical→filename mapping
  (`manual._pdf_filename`) restricts to a safe charset (`[A-Za-z0-9._-]`).
  **Breaking for custom converters:** a custom `PDF_CONVERTER` /
  `PDF_FAST_CONVERTER` template must now use **bare** `{input}` / `{output_dir}`
  placeholders (the value arrives already quoted) — drop any quotes you wrapped
  around them. ([#28])
- The PDF→markdown extraction directory is now a private `tempfile.mkdtemp`
  (mode 0700, unguessable suffix) instead of a predictable
  `/tmp/pdf-convert-<canonical>` path that was `rm -rf`'d before each run. The
  old fixed path invited symlink/pre-creation interference on a shared host and
  could collide across multiple server instances. ([#28])

### Added

- `get_wikipedia_summary` (and the underlying `wikipedia.get_summary`) now
  accept `force_refresh=True`, dropping the cached entry and re-fetching — parity
  with every other cached getter, for an article that may have been edited since
  the cached 30-day-TTL fetch. ([#41])
- `get_author` now accepts `force_refresh=True`, dropping the cached OpenAlex
  profile and re-fetching — bringing it in line with the unified paper tools.
  Author stats (`h_index`, `cited_by_count`, `works_count`) drift on the same
  30-day TTL as works, so an agent can now bust the cache for a fresh profile
  instead of waiting out the TTL. ([#37])
- The reference/citation graph tools (`get_paper_references[_count]` /
  `get_paper_citations[_count]`) now accept `force_refresh=True`, dropping the
  cached entry and re-fetching from the upstream source(s). The citation graph
  grows continuously (the reason for the 7-day OpenCitations TTL), so this lets
  an agent pull fresher coverage on demand instead of waiting out the TTL. For
  `get_paper_references` the refresh applies to **both** OpenCitations and
  Crossref (`crossref.get_work` gained `force_refresh` to match
  `openalex.get_work`); pass it on the first page and omit it when paginating so
  page 2..N reuse the warmed cache. ([#34])
- `import_paper(..., force_refresh=True)` re-imports a file even when one is
  already cached under the same identifier, replacing the cached copy. For a PDF
  it also drops the cached markdown + section index (cascade), so the next
  `convert_paper` re-runs on the new bytes — the way to swap in a corrected PDF
  or a higher-quality manual conversion. Previously a re-import under an existing
  identifier was a silent no-op that returned the stale cached file. ([#26])
- `CACHE_DIR` env var relocates the on-disk response cache root. It defaults to
  a `.cache` directory next to the project; set `CACHE_DIR` when running from an
  installed wheel or anywhere the project tree isn't writable (`~` is expanded).
  ([#25])

### Changed

- The search-list tools now report a `result_count` (how many hits the call
  returned) alongside `total_results`, and `total_results` is now the **upstream
  match count** consistently across `search_arxiv` and `search_crossref_by_title`.
  Previously `search_crossref_by_title` set `total_results` to the length of the
  returned page (≤5), so an agent comparing it against `search_arxiv`'s upstream
  total (which can be thousands) was silently misled — the two tools advertise the
  same shape. `search_crossref_by_title` now surfaces Crossref's
  `message.total-results` (which the provider had been discarding); `search_arxiv`
  keeps its upstream total and gains `result_count`. `search_wikipedia` /
  `search_cached_papers` already reported `result_count`. ([#38])
- `get_paper_references(source="auto")` now biases its source pick toward
  Crossref. Selection previously chose whichever provider returned more
  references, so a one-or-two-entry margin could flip to OpenCitations' bare
  DOI links over Crossref's structured author/title/year metadata; OpenCitations
  now wins only when it has materially more references (>1.2×). `source="auto"`
  also no longer resolves past page 1 — re-surveying mid-walk could pick a
  different source if the cached counts drifted (a `force_refresh` on a later
  page, or a TTL lapse) and silently shift the pagination offsets, so paging
  past page 1 now returns an actionable error directing the agent to pin the
  `_source` returned on page 1. ([#36])
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

- Concurrent calls for the same identifier no longer share a single response
  object. When several callers raced for one paper, single-flight handed every
  follower the *same* dict the leader returned, so a caller mutating its result
  corrupted the others'. Each caller now receives an independent deep copy. ([#41])

- `get_paper_sections` and `get_paper_section` now read cached markdown as UTF-8
  explicitly. Under a non-UTF-8 host locale (e.g. an `LC_ALL=C` container/cron
  job) the previous encoding-less read mis-decoded or raised `UnicodeDecodeError`
  on any paper with non-ASCII content (accented author names, math, CJK), even
  though the markdown is always written UTF-8 — matching the explicit-UTF-8 reads
  the rest of the pipeline already does. ([#39])
- `get_paper_sections` no longer raises an uncaught `FileNotFoundError` when a
  concurrent `download_pdf(force_refresh=True)` cascade unlinks the cached
  markdown in the window between its existence check and its read. It now
  degrades to the tool's `{error}` "not converted" contract — the same defence
  `convert_paper` already applies. `get_paper_section` gained the matching guard.
  Both also read the markdown off the event loop so a large paper's disk read
  doesn't stall concurrent fetches. ([#39])
- `convert_paper` now strips cache filesystem paths from its *error* responses,
  not just its success responses, so the pipeline's path-free boundary holds on
  every code path. ([#39])
- `search_crossref_by_title` no longer crashes on malformed Crossref date
  metadata. A record whose `date-parts` was `null` or `[]` (both occur in the
  wild) raised an unhandled `TypeError`/`IndexError` instead of returning the
  tool's `{error}` contract, taking down the whole 5-result page. Year extraction
  is now defensive and also reads `published` / `posted` / `issued`, so preprint
  records — including every bioRxiv DOI, whose date lives under `posted` — report
  a `year` instead of silently `null` (this tool is the de-facto bioRxiv search).
  ([#38])
- `search_crossref_by_title` now surfaces organisational/consortium first authors
  (e.g. `The ATLAS Collaboration`), which Crossref stores in a `name` field with
  no given/family. Such papers previously returned `first_author: null` despite a
  non-zero `author_count`. ([#38])
- `find_in_paper` now returns a `{error, suggestion}` pair (matching every other
  tool) when a paper isn't converted yet, and reads the cached markdown off the
  event loop so a large paper's disk read doesn't stall concurrent fetches. ([#38])
- `get_paper_metadata(doi, fallback_crossref=True)` now reports a
  `publication_year` for a preprint-only Crossref DOI (e.g. a bioRxiv record
  whose date lives under `posted`, not `issued`). The Crossref metadata
  formatter walked a date-key order that omitted `posted`, so the fallback
  returned `publication_year: null` for any record without an `issued` date —
  even though `search_crossref_by_title` already honoured `posted`. Both call
  sites now share one date helper walking a single canonical key order (`issued`
  first, `posted` last). ([#40])
- `get_paper_metadata(follow_published=True)` no longer mislabels a *transient*
  OpenAlex failure on the journal-version lookup as "not indexed yet". When the
  bioRxiv→journal chain hits a 5xx/timeout (a retryable error, not a definitive
  404), the preprint fallback now carries `published_lookup_retryable=True` so
  the agent can retry the chain rather than assuming the journal version is
  unindexed. A genuine 404 still falls back silently (`followed_published=False`
  only), since retrying won't surface a record that isn't there. ([#37])
- The reference/citation graph tools no longer drop the structured error signal
  when surfacing an upstream failure. `get_paper_references_count` and the
  combined-error response from `get_paper_references(source="auto")` previously
  copied out only the error *message*, discarding the `retryable` flag — so an
  agent couldn't tell a transient failure (worth a retry) from a definitive one.
  Both now forward `error` + `retryable` (+ `suggestion` when present). In
  addition, when `source="auto"` and exactly one provider errors, the served
  page now carries a `partial_failure` field naming the failed source, so an
  empty result from the surviving source isn't mistaken for a confident
  "no references" when the other source merely had a transient blip.
  `get_paper_citations_count` also reads the count defensively (`data.get`),
  returning `0` rather than raising on a success response that lacks the key. ([#36])
- Wikipedia tools (`search_wikipedia` / `get_wikipedia_summary`) are now hardened
  to match the rest of the providers. Four issues are fixed: (1) a 200 with a
  garbled/truncated body — or, for the summary, a non-dict JSON payload —
  previously raised an uncaught `JSONDecodeError`/`AttributeError` out of
  `search` / `get_summary`; both now return the uniform `{error, retryable: True}`
  dict (not negative-cached, so a retry re-fetches). (2) Page titles are now
  percent-encoded into the summary request path, so a title containing `/`, `#`,
  or `?` (e.g. `AC/DC`) no longer splits the path or truncates the request to the
  wrong record. (3) The summary cache key is now case-sensitive beyond the first
  letter (matching MediaWiki's own title normalization) — previously the whole
  title was lowercased, so case-distinct articles like `PET` and `Pet` collided
  and one's summary was served for the other. (4) `page_exists` no longer reports
  a transient failure (timeout / 5xx / backpressure) as a confident "doesn't
  exist"; only a definitive 404 (now tagged `not_found: True`, mirroring
  `openalex.get_work`) yields `exists: False`, while transient errors are
  propagated so the caller can retry. Completes the parse-hardening sweep across
  arXiv ([#30]), bioRxiv ([#31]), Crossref ([#32]), OpenAlex ([#33]), and
  OpenCitations ([#34]). ([#35])
- OpenCitations reference/citation lookups (`get_paper_references[_count]` /
  `get_paper_citations[_count]`, `source="opencitations"`) no longer crash on a
  malformed response. A 200 with a garbled/truncated JSON body previously raised
  an uncaught `JSONDecodeError` out of `get_references` / `get_citations`, and an
  anomalous 200 that wasn't the expected list of records (a dict / null / string)
  crashed the record comprehension with an `AttributeError`; both now return the
  uniform `{error, retryable: True}` dict (not negative-cached, so a retry
  re-fetches). DOIs are also percent-encoded into the `.../doi:{doi}` request
  path — a `#`/`?` previously truncated the request and silently fetched
  references for the wrong record. Completes the parse-hardening sweep across
  arXiv ([#30]), bioRxiv ([#31]), Crossref ([#32]), and OpenAlex ([#33]). ([#34])
- OpenAlex paper/author tools (`get_paper_metadata` / `_authors` / `_abstract`
  / `_bibtex`, and the batch `get_papers_metadata`) no longer crash on a
  malformed response. A 200 with a garbled/truncated JSON body previously
  raised an uncaught `JSONDecodeError` out of `get_work` / `get_author` /
  `get_works_batch`; all now return the uniform `{error, retryable: True}` dict
  (the parse failure is not negative-cached, so a retry re-fetches), and an
  anomalous 200 that is non-dict or missing the entity `id` key is treated the
  same instead of positive-caching garbage for the 30-day TTL. Completes the
  parse-hardening sweep across arXiv ([#30]), bioRxiv ([#31]), and Crossref
  ([#32]). ([#33])
- OpenAlex DOIs are now percent-encoded into the `/works/doi:{doi}` request
  path (a `#`/`?` previously truncated the request to the wrong record), and
  `_normalize_doi` strips surrounding whitespace and an `http://doi.org/`
  prefix (not just `https://`). The normalization fix also stops
  `get_papers_metadata` from reporting an `http://`-form DOI as not-found when
  the batch response actually contained it (the request and response
  canonicalizers disagreed on the scheme). A DOI containing OpenAlex filter
  metacharacters (`|`, `,`) is now resolved via the singleton path instead of
  corrupting the OR-joined batch filter. `get_author` also gains `force_refresh`
  and tags its 404 with `not_found: True`, matching `get_work`. ([#33])
- Crossref paper tools (`get_paper_metadata` / `_bibtex`, reference/citation
  lookups, `search_crossref_by_title`) no longer crash on a malformed response.
  A 200 with a garbled/truncated JSON body previously raised an uncaught
  `JSONDecodeError` out of `get_work` / `search_works`; both now return the
  uniform `{error, retryable: True}` dict (the parse failure is not
  negative-cached, so a retry re-fetches), and an anomalous 200 missing the
  `message` payload is treated the same way instead of positive-caching an empty
  record. Matches the arXiv ([#30]) and bioRxiv ([#31]) hardening. ([#32])
- Crossref DOIs containing reserved URL characters (e.g. a `#` or `?`) are now
  percent-encoded in the request path. Previously the raw DOI was interpolated
  into `/works/{doi}`, so `httpx` read everything after a `#` as a fragment and
  silently fetched the wrong record; the prefix/suffix slash stays literal.
  ([#32])
- bioRxiv/medRxiv paper tools no longer crash on a malformed response. A 200
  with a garbled/truncated JSON body previously raised an uncaught
  `JSONDecodeError`, and a non-numeric `version` in a multi-version record
  raised an uncaught `ValueError` out of `get_paper`; both now return the
  uniform `{error, retryable: True}` dict (the parse failure is not
  negative-cached, so a retry re-fetches), matching the arXiv hardening in
  ([#30]). ([#31])
- bioRxiv/medRxiv DOI URLs with a trailing query string or fragment (e.g.
  `https://doi.org/10.1101/2024.01.01.573838?ref=x` or a
  `biorxiv.org/content/...v1?download=1` link) now normalize to the bare DOI
  instead of baking the query into the canonical cache key. ([#31])
- `published_doi` is now `None` for an unpublished preprint whose `published`
  field is an empty string (not just the literal `"NA"`), so a falsy-but-present
  `""` no longer leaks out through `get_paper_metadata`. ([#31])
- `get_paper_metadata` / `search_arxiv` for arXiv no longer crash on a
  malformed or truncated XML response. A 200 with an unparseable body (e.g. a
  connection that dropped mid-stream) previously raised an uncaught
  `ParseError` out of the tool; both paths now return the uniform
  `{error, retryable: True}` dict like every other failure, and the transient
  parse failure is not negative-cached so a retry re-fetches. A genuine HTTP 404
  is now negative-cached (matching arXiv's 200-with-error-entry shape), while
  transient 5xx/timeout failures remain uncached. ([#30])
- arXiv abstract/PDF URLs with a trailing query string or fragment (e.g.
  `https://arxiv.org/abs/2301.00001?context=cs`) now normalize to the bare ID
  instead of baking the query into the cache key. ([#30])
- ACL Anthology DOIs are now detected case-insensitively. The `10.18653/v1/`
  prefix was matched case-sensitively, so a DOI handed in with an uppercased
  `V1` (DOIs are officially case-insensitive) was rejected — misrouting it to
  OpenAlex for metadata and failing `download_pdf` with "Not an ACL Anthology
  DOI". ACL was the only provider exposed (every other prefix is all-digit). The
  anthology-id suffix handling is unchanged. ([#29])
- `convert_paper` no longer crashes with an unhandled `FileNotFoundError` when a
  concurrent refresh deletes a paper's cached markdown mid-read. `convert_pdf`
  checked `markdown.exists()` *before* taking the per-paper lock, then read the
  file inside it — so a `convert_paper(force_refresh=True)` or the `download_pdf`
  force-refresh cascade (which now also holds the lock) could unlink the file in
  that window. The read is now guarded and a vanished file is treated as a cache
  miss (re-converting cleanly). ([#28])
- `convert_paper(mode="fast")` no longer relabels a previously full-converted
  paper's markdown as degraded `conversion_mode: "fast"` in the rare race where
  the fast path's cached re-check fires; the recorded `"full"` mode is preserved.
  ([#28])
- Corrected the `_resolve_*_timeout` documentation: `PDF_CONVERT_TIMEOUT` /
  `PDF_FAST_CONVERT_TIMEOUT` set to `"0"`, a negative number, or
  `none`/`off`/`disabled` **disable** the timeout (the code always did this); the
  prior docstring/comments wrongly said `"0"`/negative fell back to the default.
  ([#28])
- `download_pdf(doi, allow_oa_url=True)` for a generic publisher DOI is now
  hardened on its failure paths. A *transient* OpenAlex lookup error (timeout /
  5xx, `retryable: True`) is surfaced as-is so the agent retries, instead of
  being wrongly told to go fetch the PDF by hand. A *definitive* failure
  (closed-access / no OA URL, or an OA URL that resolves to an HTML landing page
  rather than a PDF) is now negative-cached for 24h, so a retrying agent no
  longer re-resolves OpenAlex and re-fetches the same non-PDF on every call;
  `force_refresh=True` clears the entry. The closed-access error now carries
  `retryable: False` to match the rest of the error contract, and a 0-byte /
  pre-header leftover at the destination is treated as a miss (via
  `manual._looks_like_cached_pdf`) instead of served as a cache hit. A
  `MAX_PDF_BYTES` size-cap abort is deliberately *not* negative-cached, so
  raising the cap takes effect without `force_refresh`. ([#27])
- Manual import now writes atomically. `import_paper` copied a local PDF straight
  to its canonical cache path (and wrote imported markdown the same way), so a
  crash / disk-full mid-write could leave a truncated file that was then served as
  a complete cache hit forever. PDFs now copy through a sibling temp + atomic
  rename (`cache._atomic_copy`) and markdown writes through `cache._atomic_write_text`,
  so a reader never sees a half-written file. A 0-byte / non-`%PDF-` leftover at the
  canonical PDF path is now treated as a miss and overwritten instead of returned as
  cached. ([#26])
- Imported markdown is now read back as UTF-8 on the cached-hit path (and the
  full PDF→markdown conversion path reads/writes UTF-8 explicitly), so a
  pre-converted paper containing non-ASCII text survives a re-import or section
  read on a non-UTF-8 host locale (`LC_ALL=C`) instead of mis-decoding or raising.
  Extends the [#25] cache-read fix to the markdown files. ([#26])
- Cache reads now decode as UTF-8 explicitly (matching the UTF-8 write path),
  so cached records containing non-ASCII text (accented author names, etc.)
  survive on hosts with a non-UTF-8 locale. Previously, under `LC_ALL=C`
  (common in containers/cron) a read defaulted to ASCII, raised
  `UnicodeDecodeError`, and the self-heal path silently deleted the good entry —
  so those records were effectively never cached. ([#25])
- `get` / `get_negative` now treat a non-dict JSON payload (external tampering
  or a foreign writer) as corruption — unlink and return `None` — instead of
  returning a value that violates the `dict | None` contract or crashing on the
  `_expires_at` lookup. ([#25])
- Negative-cache reads no longer drop caller payload keys that begin with `_`
  (e.g. `_canonical_id`); only the internal `_expires_at` bookkeeping field is
  stripped. ([#25])
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

[2026.09.08]: https://github.com/hunter-heidenreich/academic-tools-mcp/compare/v2026.09.04...v2026.09.08
[2026.09.04]: https://github.com/hunter-heidenreich/academic-tools-mcp/compare/v2026.06.04...v2026.09.04
[2026.06.04]: https://github.com/hunter-heidenreich/academic-tools-mcp/compare/v2026.05.31...v2026.06.04
[2026.05.31]: https://github.com/hunter-heidenreich/academic-tools-mcp/compare/v2026.05.29...v2026.05.31
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
[#25]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/25
[#26]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/26
[#27]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/27
[#28]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/28
[#29]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/29
[#30]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/30
[#31]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/31
[#32]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/32
[#33]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/33
[#34]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/34
[#35]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/35
[#36]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/36
[#37]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/37
[#38]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/38
[#39]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/39
[#40]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/40
[#41]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/41
[#43]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/43
[#44]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/44
[#48]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/48
[#49]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/49
[#50]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/50
[#51]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/51
[#52]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/52
[#53]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/53
[#54]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/54
[#55]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/55
[#56]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/56
[#58]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/58
[#65]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/65
[#66]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/66
[#68]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/68
[#73]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/73
[#76]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/76
[#77]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/77
[#78]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/78
[#79]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/79
[#81]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/81
[#82]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/82
[#83]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/83
[#84]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/84
[#85]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/85
[#86]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/86
[#87]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/87
[#88]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/88
[#89]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/89
[#90]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/90
[#91]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/91
[#92]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/92
[#93]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/93
[#94]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/94
[#95]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/95
[#96]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/96
[#97]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/97
[#98]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/98
[#99]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/99
[#100]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/100
[#101]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/101
[#102]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/102
[#103]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/103
[#104]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/104
[#105]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/105
[#106]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/106
[#107]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/107
[#108]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/108
[#110]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/110
[#112]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/112
[#115]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/115
[#116]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/116
[#117]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/117
