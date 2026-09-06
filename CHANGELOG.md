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

- **`get_server_stats` now reports PDF write failures.** `cache_write_failures`
  counted a failed `cache.put` but not a failed PDF write — and a PDF is the
  largest write the server makes, so the write most likely to hit a full disk
  was the one invisible in the counter that exists to show it. Both
  `_pdf_download.stream_to_file` and `manual.import_local_pdf` now count under
  the provider's cache namespace, in the same row as its other counters.
  ([#81])

- **Guard tests for the counters an operator reads.** Nothing pinned that the
  snapshot's rows are copies (a caller stripping a key before logging edited
  the live counters), that a provider's in-flight sample lands in the row
  holding its cache counters rather than a second row of its own, or that
  `snapshot()` does not import modules in order to measure them. The
  `reset()` test could not fail: an `or` whose first branch passed trivially.
  `config.flag` and the `DEBUG_REQUESTS` spellings are covered directly.
  ([#81])

- **`stream_to_file`'s in-slot re-check and cap boundary are now actually
  tested.** The re-check is the `cached_download` protocol's core concurrency
  guarantee — a caller that missed the outer check picks up the leader's
  just-written PDF instead of re-streaming it — and both of its branches, plus
  the deliberate `force_refresh` skip, were uncovered. So was the
  exactly-at-`MAX_PDF_BYTES` case the design rules name explicitly. Deleting
  the re-check passed the whole prior suite. `_pdf_download` is now at full
  statement coverage. ([#79])

- **Property-based tests for the retry/backoff arithmetic.** `_http`'s two
  numeric invariants — a parsed `Retry-After` is either `None` or a finite
  positive float, and every sleep sits between the `backoff_seconds` floor and
  the `_MAX_RETRY_AFTER_SECONDS` ceiling — were stated in prose and covered by
  a handful of examples. `tests/test_http_properties.py` holds them over
  arbitrary header values. The `isfinite` guard turned out to be defended by no
  example test at all: deleting it passes the whole example suite. ([#78])

- **A unit-test file for the shared DOI normalizer, which had none.** `_doi`
  was covered only by property tests and, transitively, by five provider
  wrappers that each re-derived the same six examples. Neither side of the
  four-digit registrant boundary was asserted anywhere, `looks_like_doi` had no
  direct negative case, and nothing pinned which near-miss spellings
  (`info:doi/`, `DOI 10.x/y`, prose punctuation) are deliberately rejected.
  `tests/test_doi.py` covers those, and the three identical provider wrapper
  test blocks collapse into one parametrized test asserting *delegation* —
  which is the actual invariant — instead of re-deriving the behaviour three
  times. `cache_search` now builds its filename-stem regex from the exported
  `_doi.REGISTRANT_PATTERN` rather than a second spelling of it. ([#77])

- **A `Known upstream limitations` section in the README.** OpenAlex drops or
  mangles diacritics in author names, reports an author's *current* affiliation
  rather than their affiliation at publication time, and arXiv and the published
  DOI can list different author sets for the same work. These are properties of
  the upstream providers, but until now they were written down only in
  `CLAUDE.md` — a file users don't read — so anyone citing a result had no way
  to know which fields to double-check. ([#73])

- **Tests for `get_client`, which had none.** The pool's shutdown half was well
  covered; the singleton half — the entire body of `get_client` — was executed
  only incidentally, by a politeness test reading a User-Agent back off a built
  client. Neither the identity invariant (same name, same object, one connection
  pool) nor the documented footgun (a second call silently ignores its `headers`
  and `timeout`) was pinned anywhere, and every existing test duck-typed
  `aclose`, so no test drove a real `httpx.AsyncClient` through construction and
  close. Two pairs of near-verbatim duplicate shutdown tests are merged, and the
  two groups' disagreeing setup styles (`monkeypatch.setattr` vs. direct
  mutation of the module global) are settled on monkeypatch. ([#76])

- **Tool-layer tests for the three MCP tools that had none, and a CI coverage
  floor to stop it recurring.** `get_paper_references_count`, `search_wikipedia`
  and `get_wikipedia_summary` were untested — the only occurrence of
  `get_paper_references_count` anywhere under `tests/` was inside a docstring.
  The providers beneath them were well covered; it was the `@mcp.tool` wrappers
  that were not, which is where the response shapes agents branch on and the
  `suggestion` recovery hints live. `tools/graph.py` and `tools/search.py` were
  the two lowest-covered files in the tree at 75% and 85%, against 93% for the
  providers they call.

  `get_paper_references_count`'s partial-failure branch turned out to be the
  only shape in the reference family exercised nowhere end-to-end: sibling
  `get_paper_references` reports a failed source via `partial_failure` on the
  winning page, while this tool reports it in place inside `sources`.

  CI now runs the suite under `--cov-fail-under=90` (92.8% today, so it
  ratchets rather than stretches). The flags are deliberately **not** in
  `addopts`: applied to every invocation they also apply to
  `pytest tests/test_bibtex.py`, a workflow the README documents, which reports
  ~23% and would fail. `pyproject.toml` also gained a
  `[tool.pytest.ini_options]` section — there was none — pinning `testpaths`
  and `asyncio_mode = "strict"`, which was already the effective default and is
  the reason every async test needs its own `@pytest.mark.asyncio`. ([#67])

- **Tests for `_fast_extract`**, which was at 0% coverage — the only test
  mentioning it asserted on the command string built for it and never ran it.
  Its entire job is behaving correctly when things go wrong (pymupdf absent,
  PDF corrupt, wrong argv), so none of that was exercised. Now 95% under CI's
  `--all-extras`, including a real `python -m` round trip. ([#53])

- **Continuous integration.** A GitHub Actions workflow now runs `ruff check`,
  `ruff format --check`, `mypy`, and the full test suite on every push to `main`
  and every pull request, against Python 3.11 and 3.13. All four gates were
  already green locally; CI pins that bar rather than chasing it. ([#47])

### Changed

- **The download temp file is created only once the response is worth
  writing.** `stream_to_file` used to `mkstemp` before opening the connection,
  so a 404 created and deleted a scratch file (and `mkdir`'d the destination
  directory) for nothing, and every early return had to be covered by an
  `fd_handed_off` flag and a manual `os.close`. Moving it past the status and
  Content-Type gates and switching to `NamedTemporaryFile` — which binds the fd
  to the file object, so no raw fd can leak — removes the flag entirely.
  ([#79])

- **`manual.import_local_pdf`'s cache-hit branch routes through
  `_pdf_download.cached_hit`.** It was the last hand-rolled
  check-then-`stat` pair — the exact shape `cached_hit` was introduced to
  retire in the ACL provider — so a PDF unlinked between the usability check
  and the size read raised `OSError` out of the tool instead of counting as a
  miss. Its now-unused `_looks_like_cached_pdf` alias is gone. ([#79])

- **arXiv and bioRxiv name their PDF timeout** (`_PDF_TIMEOUT_SECONDS`) instead
  of passing a bare `60.0`, matching `acl_anthology` and `oa_download`. ([#79])

- **arXiv's parse-error helper delegates to the shared one instead of
  respelling it.** `_http.parse_error_dict` grew a `detail` parameter
  specifically so arXiv could say "could not be parsed as XML" without forking
  the shape; arXiv kept its own byte-identical copy anyway. ([#78])


- **The reference and citation tools reject a non-DOI locally.**
  `get_paper_references{,_count}` and `get_paper_citations{,_count}` forwarded
  whatever they were given to Crossref and OpenCitations, both of which are
  DOI-only: an arXiv ID cost an upstream round-trip to earn a 404 and then
  negative-cached a key that could never have resolved. They now gate on
  `_doi.looks_like_doi` — the same predicate `get_paper_metadata` dispatches on
  — and return `{error, not_found, suggestion}` naming the mismatch. ([#77])

- **`_doi.normalize` is idempotent, and accepts `www.doi.org`.** The `doi:`
  prefix strip now loops, so a doubled prefix can no longer survive one pass
  and key separately from its own output. The bare-vs-URL treatment of `?`/`#`
  is unchanged but now documented and pinned: a bare DOI keeps them (they are
  legal suffix characters, and truncating would key a different paper), a URL
  cuts at them (there they are unresolvable unless percent-encoded). ([#77])

- **The ruff rule set went from 8 rule families to 38.** Every added rule is one
  the codebase already satisfied or was made to satisfy here, so each is a
  ratchet against regressions rather than a backlog; `ANN`/`D`/`S` hold for
  `src/` only, since `tests/` is ~2k bare asserts and undocumented fixtures by
  design. The visible effects on the code: 15 `try`/`except`/`pass` blocks became
  `contextlib.suppress`, 16 private helpers gained return annotations, and 8
  modules and 3 packages gained docstrings. Every entry in `ignore` now carries
  the reason it is there, and the one-off exceptions (`crossref`'s
  search-throttle globals, the f-string FTS query, one async `timeout`
  parameter) carry a per-site `noqa` instead, so new occurrences still get
  flagged. ([#74])

- **Open-access PDF downloads are paced at one request per second, per
  publisher host.** They previously had no inter-start gap at all, on the
  reasoning that "every URL is a different host" — an assumption, not a fact.
  The URLs come from OpenAlex, so a reference walk through one journal
  resolves many DOIs to the same domain, which then got fetched back-to-back
  at the one provider with no documented budget and no relationship to trade
  on. `_throttle.Throttle` gained an opt-in `per_host=True` keying the gap by
  `urlsplit(url).netloc`; only this path uses it, and the seven API clients
  keep their single global timestamp.

  The gap-lock now **reserves** a caller's start instant and sleeps outside the
  lock rather than holding it across the sleep. Without that, one host's wait
  blocks an unrelated host from even computing its own, collapsing the
  effective rate back to one global gap and making per-host pacing pointless —
  pinned by a test asserting a fresh host is not delayed behind a paced one.
  Observable pacing in global mode is unchanged: `asyncio.Lock` is FIFO and
  reserved starts stay spaced by exactly `min_gap_seconds`.

  `max_concurrent` stays global in both modes. It bounds *our* egress —
  sockets, file descriptors, and simultaneous in-flight streams, since a PDF
  download holds its slot for the whole transfer — so a per-host cap would let
  a 20-publisher walk open 40 parallel streams. ([#68])

- **Source comments state the invariant they protect, not the regression that
  produced them.** Roughly 35 sites across 18 files carried past-tense prose —
  "the command used to be wrapped as `2>&1`", "six providers carried
  byte-identical copies", "the previous implementation scoped corpus
  statistics to the filter". That is what `CHANGELOG.md` and
  `git log -S "<phrase>"` are for, and prose about deleted code outlives the
  code and then misleads: `_pdf_download.cached_hit` claimed to have
  "collapsed the six copies of this block" while two remained inline, and a
  benchmark table in `cache_search.py` disagreed with this file's copy of the
  same measurements on **every** row (84 vs 165 MB, 20 vs 43 MB, 1.3 vs
  40 ms), having also labelled ranking cost as cold-query time.

  Deletions where the rule is legible from the code beneath; rewrites as
  `Invariant: … (guarded by tests/…)` where a test pins it, each citation
  verified by mutating the guarded line and watching that test fail; and
  present-tense trims where the warning is load-bearing and genuinely not
  inferable — Crossref's rate/identity lockstep, arXiv's version-in-the-cache-key,
  the single-flight shield, the converter process-group kill. Test docstrings
  are left alone: a regression test's purpose *is* the regression.
  `.claude/rules/python-design.md` gains a "don't transcribe history" clause so
  this doesn't grow back. ([#69])

- **Two passthrough wrappers that existed only as test seams are gone.**
  `tools/paper._fetch_work` had *no* test patching it at all, and
  `_app._fetch_crossref_work` had 17 — which is 17 tests reaching through a
  layer of production code to patch a one-line delegate. Tool modules now call
  `crossref.get_work` / `openalex.get_work` directly and tests patch the
  provider, so `.claude/rules/server.md` no longer has to explain why call
  sites route through `_app`. ([#69])

- **`search_cached_papers` opened the search index once, not twice.** It ran
  `search` and `unindexable` as two separate `asyncio.to_thread` hops, each
  refreshing the index with a full `os.scandir` walk of the corpus and opening
  its own connection. They now share one hop and one refresh. ([#69])

- **The keyword search index moved from a single JSON file to SQLite FTS5.**
  The old index held every document's full term-frequency map, in both a
  folded and an un-folded form, and parsed the whole thing into the heap on
  first use. Measured on a 3,732-paper corpus:

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
  than the JSON it replaces rather than larger.

  Two FTS tables, not one: `normalize` is a query-time flag in this API while
  diacritic folding is a build-time tokenizer option in FTS5, so the index
  carries a folded and an un-folded table and the flag selects between them.
  That keeps the parameter's meaning exactly rather than silently redefining
  it. Migration is automatic — the database is built on first search (~7s for
  3,700 papers) and the old `index.json` is deleted. ([#55])

- **Section-boundary computation is single-homed.** It existed in four
  places: `parse_sections`, `find_in_markdown` and `get_section_content` (the
  latter two byte-identical), and `cache_search._section_for_offset` — a
  fourth dialect over raw offsets that lacked the empty-section filter and so
  could name a section the reader's index had already dropped.
  `find_in_markdown`'s docstring already depended on two of them staying
  identical "because both apply the same recipe", an invariant guarded by a
  single test. All four now call `papers.section_boundaries`. Verified across
  1,693 cached papers: zero disagreements between the index and the
  reader. ([#54])

- **Uniform "not converted yet" error.** Four tools produced this condition in
  two different shapes — three jammed the recovery advice into the `error`
  string while `find_in_paper` split it into a `suggestion` key. Agents branch
  on `suggestion`, so the shape now comes from one builder. ([#51])
- **Removed the `CITATION_SOURCE` parameter type.** It described a `source`
  parameter that `get_paper_citations` does not have and nothing referenced it;
  `.claude/rules/server.md` documented the phantom parameter too. ([#51])
- **Deleted the duplicated `_first` / `_crossref_date`.** `_app.py`'s comment
  explains the shared version exists "so the two can't drift" — while
  `tools/paper.py` carried its own copies, and `server.md` documented a third
  date-key ordering. ([#51])
- **README brought back in line with the code.** It was last touched before the
  `providers/` + `tools/` refactor and claimed positive cache entries never
  expire, directly contradicting the per-provider TTL table in `CLAUDE.md`. Also
  adds `search_cached_papers`, `convert_paper(mode="fast")`,
  `download_pdf(allow_oa_url=True)`, and the seven undocumented environment
  variables. ([#51])
- **Every source module now routes to a rule file.** `oa_download.py`,
  `_fast_extract.py` and `_textnorm.py` appeared in no `paths:` frontmatter, so
  editing them loaded no module rule despite `CLAUDE.md` promising per-module
  detail lives in `.claude/rules/`. ([#51])

- **`get_works_batch` rejoins the shared getter protocol.** It had no
  single-flight at all, so two concurrent `get_papers_metadata` calls with
  overlapping DOI lists both issued full batch GETs; chunks are now coalesced
  on their sorted contents. The chunk loop was also a serial `for ... await`,
  issuing four requests one after another while three of OpenAlex's four
  concurrency slots sat idle — chunks and singleton fallbacks now run
  concurrently under the existing throttle. ([#49])
- **Leaf boilerplate consolidated.** `_parse_error_dict` (six byte-identical
  copies) and `_PARSE_ERRORS` (five) now delegate to `_http.parse_error_dict`
  and `_http.JSON_PARSE_ERRORS`; error messages are unchanged. Per-provider
  policy constants and `fetch` closures stay where they are — the seven clients
  are deliberately *not* collapsed into a generic class. ([#49])
- **One-time cache filename migration.** Because `safe_stem` replaces two
  earlier filename rules, cached PDFs and markdown whose identifier contains
  characters outside `[A-Za-z0-9._-]` are renamed forward at server startup
  (alongside the existing orphan-`.tmp` sweep). Ordinary arXiv IDs and DOIs are
  unaffected — on a 7,000-file cache only 20 files move, all of them Elsevier
  PII-style DOIs (`10.1016/s1359-6446(03)02831-9`) or freeform labels
  (`google:wordpiece:2012`). Without the sweep those papers would report "not
  converted yet" and re-run conversions that take tens of minutes. The sweep is
  idempotent and never overwrites an existing file. ([#48])

### Fixed

- **A new provider no longer has to be registered in two hand-maintained
  lists to be visible.** `_stats._PROVIDER_MODULES` and the reset fixture in
  `tests/conftest.py` each named the same eight modules; a provider missing
  from the first reported no in-flight count, and one missing from the second
  leaked throttle state between tests. Both now discover throttles by scanning
  imported modules, and `snapshot()` no longer imports a provider as a side
  effect of sampling it. In-flight is filed under the throttle's own
  `namespace` instead of a name derived from the module path, so a provider
  whose module name and cache namespace differ can no longer split into two
  rows. ([#81])

- **`DEBUG_REQUESTS` and `ENABLE_DEBUG_TOOLS` parsed truthiness separately.**
  Two copies of the same `in ("1", "true", "yes", "on")` tuple, either of which
  could drift; `_stats` also read `os.environ` directly rather than through
  `config`, so a `DEBUG_REQUESTS` set in `.env` depended on some other module
  having loaded it first. Both now route through `config.flag`, which also
  strips surrounding whitespace — a trailing space in a `.env` line is a typo,
  not a request to disable the flag. ([#81])

- **A full or read-only disk no longer escapes `download_pdf` as a raised
  `OSError`.** `stream_to_file` caught only `_http.HTTPX_ERRORS`, so an ENOSPC
  from the chunk write propagated past the `{error}` contract that every other
  tool upholds — in the one code path whose `MAX_PDF_BYTES` cap exists
  precisely to protect the disk. `cache.put` had already made this call for
  JSON writes and documented why. It now returns `{error, retryable: True}`:
  retryable, so a full disk also stays out of the negative cache rather than
  being recorded against the paper. `manual.import_local_pdf`'s post-copy size
  read moved inside its existing `try` for the same reason. ([#79])

- **`MAX_PDF_BYTES=-1` silently disabled the size cap.** The resolver treated
  any value `<= 0` as "disabled", so the `-1`-means-unlimited idiom from other
  tools — or a typo — removed the disk guard with no signal, and the documented
  disable vocabulary (`none` / `off` / `disabled` / `0`) did not say so.
  Negative values now fall back to the default. ([#79])

- **Transient HTTP errors now carry `retryable: True`, the flag two callers
  already branch on.** `_http.error_dict` set it for local backpressure alone;
  a 429, a 5xx, a timeout and a network error each announced themselves as
  "Transient — retry." in prose and carried no machine-readable flag at all.
  Two consumers read the key, not the prose, and both got it wrong:
  `oa_download` answered an OpenAlex timeout with "fetch the PDF by hand and
  call `import_paper`" — the advice its own comment says is wrong for a blip —
  and `get_paper_references`' both-sources-failed response told the agent that
  "retryable errors are flagged `'retryable': true`" while flagging nothing.
  Other 4xx stay deliberately unflagged: `retryable: False` is an explicit
  "definitive, safe to negative-cache" signal, and a paywalled 403 is not
  something we know that about. ([#78])

- **`_RETRYABLE_STATUSES` is now the single definition of "transient status".**
  `get_with_retry` read the allowlist while `error_dict` used a `500 <= status
  < 600` range, and the two disagreed in both directions: a 501 Not Implemented
  was advertised to the agent as transient though the retry helper itself
  declines to retry it, and a 408 / 425 — which it *does* retry — reached the
  agent unflagged, as a body snippet. Both now read the same frozenset, and a
  retryable 4xx reads "temporary rejection (HTTP 408)" rather than being
  mislabelled a server error. ([#78])

- **`Retry-After` on a 5xx now reaches the agent.** `get_with_retry` honours
  the header on every retryable status, but `error_dict` read it only inside
  the 429 branch — so a 503 maintenance window advertising `Retry-After: 300`
  was slept on internally and then discarded, and the agent got no
  `retry_after_seconds` at all. Both RFC 9110 forms are honoured, and the
  existing `_MAX_RETRY_AFTER_SECONDS` clamp applies. ([#78])

- **A `Throttle`'s `label` now actually reaches the agent on backpressure.**
  `LocalBackpressureError` carried it and `error_dict` ignored it, building the
  message from its own argument instead. Every provider's `label` matches its
  call-site literal, so behaviour is unchanged today — but the rules file
  promised `label` was the agent-facing name, and a new provider that set the
  two differently would have found it silently discarded. ([#78])

- **A non-positive `max_attempts` no longer escapes as `UnboundLocalError`.**
  `get_with_retry` skipped its loop entirely and fell through to an unbound
  `response`. The resulting `UnboundLocalError` is a `NameError`, so it is not
  in `HTTPX_ERRORS` and would have escaped the provider instead of becoming an
  `{error}` dict. It clamps to one attempt, so a misconfigured `Throttle`
  degrades to "no retries" rather than crashing the tool. ([#78])


- **The DOI property-test strategy could generate a non-DOI and assert it was
  one.** Its suffix alphabet blacklisted Unicode category `Zs` but not `Zl` /
  `Zp`, so it could emit U+2028 — which `str.strip()` and `\S` both treat as
  whitespace, making the "DOI" it built not DOI-shaped. The test failed only on
  a seed that happened to find it. ([#77])

- **A `doi=` BibTeX field could contain a resolver URL.** `generate_bibtex`
  stripped OpenAlex's DOI with a local `startswith("https://doi.org/")` test,
  so an older record served over plain http emitted
  `doi={http://doi.org/10.x/y}` — which every BibTeX style then renders as a
  doubled resolver link. The http spelling is real enough that the batch
  matcher already handled it. All three generators (OpenAlex, arXiv, bioRxiv)
  now route the provider's DOI through `_doi.normalize`, the module that
  exists so this is not decided twice. ([#77])

- **Shutdown's per-client close timeout was not a bound.** `aclose_all` wrapped
  each `client.aclose()` in `asyncio.wait_for`, which cancels the coroutine on
  timeout and then *awaits it* — so a teardown that keeps awaiting past
  cancellation (a TLS shutdown hanging on the peer, the case the timeout exists
  for) pinned the FastMCP lifespan for as long as it liked. The whole set is now
  bounded once by `asyncio.wait`, which returns when the timeout expires whether
  or not the closes cooperate; stragglers are cancelled and deliberately left
  unawaited, since the process is exiting and the kernel reaps the socket. The
  cancel loop runs in a `finally` because that is the one thing the old
  `asyncio.gather` did for free: independently created tasks outlive an outer
  cancellation of `asyncio.wait`, where gather propagates it to its children.
  ([#76])

- **`get_papers_metadata` no longer depends on an `assert` for a correctness
  guard.** The arXiv/bioRxiv fan-out asserted that its identifier resolved to a
  real provider; under `python -O` that assert is stripped, and a `None` source
  would have fallen into a hint lookup as an opaque `KeyError` instead of an
  error an agent can read. The path is unreachable through the dispatch loop
  either way — but it is now an explicit guard that passes through the
  unknown-identifier error `_fetch_source` already returns. ([#74])

- **Four defects a second, per-file audit of `.claude/rules/` turned up in the
  code itself.** Each surfaced as a rules claim that was wrong because the code
  was wrong. **Dispatch had forked the DOI regex**: `manual._DOI_RE` was
  byte-identical to `_doi._DOI_RE` and `resolve_metadata_source` routed on it,
  leaving `_doi.looks_like_doi` with no caller in `src/` — so the property test
  added earlier in this branch pinned a symbol nothing used, and editing either
  pattern would have desynced dispatch from caching. **`import_paper` skipped
  the sections lock on its PDF branch**, where `manual._invalidate_derived`
  unlinks the markdown in a worker thread, so a concurrent reader could lose the
  file between its `exists()` check and its read — `_reparse_sections_locked`'s
  docstring claims every unlinker holds that lock, and now they do.
  **`search_cached_papers` returned `canonical_id`s that chained nowhere**: every
  non-arXiv/bioRxiv/ACL DOI lands in the `manual` namespace, whose filename
  inversion passed publisher DOIs straight through as
  `10.1038_s41586-021-03819-2`; the registrant slash is decidable (the registrant
  is digits only) and percent-escapes are now decoded, which no namespace did.
  And **Crossref search does not warm what `get_paper_metadata` reads** — it
  warms the Crossref namespace while `resolve_metadata_source` sends plain DOIs
  to OpenAlex, so the "free cache hit" promise was wrong in `README.md` and in
  the `instructions=` string every agent loads. ([#73])

- **A test citation pointing at a class that does not exist, and five smaller
  doc defects.** `tests/test_manual.py::TestMarkdownImportSectionsIndex` was
  cited in `papers.py` and in `pipeline.md`; nothing by that name is in the
  repo. Also `get_with_retry`'s docstring saying "the sleep before attempt *n*"
  when the sleep runs after it, a comment naming `_INDEX_VERSION` for
  `_SCHEMA_VERSION`, `search_cached_papers` claiming zero-score hits are dropped
  when FTS5 never returns them, the `add-provider` skill pointing at
  `crossref.py` as a template that `python-design.md` calls a counter-example,
  and a stale "10x its search rate" left in a test docstring. ([#73])

- **A second pass over every `.claude/rules/` file, one reviewer per file,
  reading each section against the source it covers.** The first audit fixed
  claims; this one fixed the claims the first audit made *and* the shape of the
  files. Wrong claims corrected include: `cache.md` calling `cached_lookup` the
  only home for the force_refresh ordering when `openalex.get_works_batch`
  open-codes it; `http.md`'s backoff formula off by one against the test that
  pins it; `pdf-download.md` calling `cached_download` identical to
  `cached_lookup` when it deliberately skips the in-slot re-check under
  `force_refresh`; `providers.md` undercounting the unquoted request paths and
  attributing `_throttled_get` to a PDF-only provider; `python-design.md` citing
  `follow_published` as a flag that leaves the response shape unchanged, which
  is the one flag that doesn't; `server.md` naming a shared formatter the batch
  path bypasses; `utils.md` naming two wrapper functions that don't exist.
  Mechanism invariants were moved to the module that implements them
  (`max_pending` to `http.md`, the single-`SingleFlight` deadlock rule to
  `cache.md`), a circular `utils.md` ↔ `providers.md` cross-reference was cut,
  and the Crossref rate table that `crossref.py` and `tests/test_politeness.py`
  both point at now actually exists in `providers.md`. ([#73])

- **Test citations dropped from the rules layer, and the convention amended to
  match.** `python-design.md` prescribed `(Guarded by tests/…::test_y)` on every
  invariant; in practice a `::TestClass::test_method` node ID churns faster than
  the code it guards, and this layer had already shipped one pointing at a class
  that does not exist. The rules files now state the invariant and what breaks
  if you violate it. Citing a test in a *code* comment or docstring is still
  fine — mutate the guarded line and watch it fail first. ([#73])

- **The `.claude/rules/` layer audited section by section against the code it
  describes, and roughly fifty wrong claims corrected.** Each file was read
  beside every source file its `paths:` frontmatter covers. The claims that
  would have talked a reader into breaking working code:

  `cache.md` said every `_singleflight.do` call routes through `cached_lookup`
  and is therefore deep-copied — `openalex._fetch_chunk` calls it directly and
  skips the copy on purpose. `pipeline.md` said `store_markdown_and_index()` is
  the only place a sections-cache entry is assembled; there are three writers,
  and the paragraph's stated consequence contradicted the same file six lines
  down. `http.md` called the per-host map sweep "exact rather than heuristic",
  which describes one of `_prune_hosts`' two branches and argues the other —
  the one a test exists for — is dead code. `providers.md` said a DOI missing
  from a batch response is negative-cached like a singleton 404, omitting the
  `trustworthy` gate that keeps a truncated page from poisoning a live DOI for
  the full TTL. `python-design.md` cited hypothesis property tests for
  `lower_with_map` offsets and BibTeX escaping that do not exist, so an agent
  reading it would believe those invariants were already pinned. `server.md`
  described `_escape_doi` as handling four characters with no backslash
  mangling — it handles ten including `\` — and phrased the `source="auto"`
  page-1 restriction as advice when it is a hard error. `utils.md` called
  `config.get` the only accessor, which would have an agent remove the
  deliberate `os.environ` seam in `_stats`. `pdf-download.md` enumerated
  `stream_to_file`'s error vocabulary without the `retryable: True` empty-body
  branch, the exact case its allowlist exists to protect.

  The same audit corrected the source comments the files had drifted from:
  two referencing `cache.has`, deleted earlier in this branch; `crossref.py`'s
  "10x its search rate" (it is 3x); `arxiv.py`'s positive-TTL comment claiming
  the canonical key strips the version suffix, which the function twenty lines
  below explicitly does not; `get_with_retry`'s "any 5xx", against an explicit
  allowlist; a sections-lock comment calling an LRU a FIFO; `_stats`' wiring
  list and `snapshot()` / `get_server_stats` docstrings, all three of which
  omitted `cache_write_failures`; and three past-tense rationales rewritten as
  the invariant they were hiding. ([#73])

- **The rules files trimmed by ~11% overall, and re-scoped where the split was
  wrong.** The dominant cost was prose restating the docstring of the module
  the file loads for — a rules file loads *because* you opened that module, so
  its docstrings are already on screen. `oa_download.py` moved from
  `pipeline.md` to `pdf-download.md`, whose protocol it uses and whose guidance
  it could not previously see; `python-design.md` now also loads when a rules
  file itself is edited, since its three anti-rot conventions are what govern
  them. `http.md` had stated "policy lives in `providers.md`, not here" and
  then carried three paragraphs of provider policy; that is now consistent.
  ([#73])

- **`_doi`'s load-bearing ordering invariant is now guarded.** `normalize`
  strips the `doi:` prefix *before* the URL handling because
  `"doi:https://doi.org/10.x/y"` occurs in the wild, but the property test's
  spelling list covered the prefix and the URL only separately. Both nested
  forms are now in it, mutation-verified: reordering the two steps fails the
  test. ([#73])

- **A transient network failure during an open-access PDF download was cached
  as permanent for 24 hours.** `_is_definitive_failure` asked
  `retryable is not True` — a denylist — and its docstring claimed this
  "deliberately excludes a `retryable` transport error". It does not:
  `_http.error_dict` sets `retryable` on exactly one of its six branches
  (`LocalBackpressureError`). A timeout, a connection reset, a 503 and a 429
  all arrive carrying no `retryable` key at all, so every one of them was
  classified permanent and negative-cached, stranding the identifier behind a
  stale "no open-access copy exists" until the TTL expired or the agent passed
  `force_refresh`.

  The predicate is now an **allowlist** on an explicit `retryable: False` and
  lives in `_pdf_download`, beside the function whose error vocabulary it
  reads — keeping the classifier apart from the producer is what let the two
  drift. Pinned by a test that asserts those transport errors carry no flag,
  so the premise can't silently change again. ([#65])

- **`download_pdf`'s 404 now carries `retryable: False`.** It was the one
  definitive branch with no flag, so the classifier above could not see it and
  an agent could not tell a dead URL from a blipped one without parsing the
  message. *This is a response-shape change.* ([#65])

- **A PDF that 404s re-hit the upstream on every `download_pdf` call.** Only
  the open-access path negative-cached its download failures; arXiv, bioRxiv
  and ACL Anthology cached nothing, so a withdrawn paper or a wrong Anthology
  ID cost a request every single time an agent retried. All four now share
  `_pdf_download.cached_download` — the file-artifact sibling of
  `cache.cached_lookup` — with per-provider TTLs: 1h for the preprint servers,
  which render PDFs lazily, and 24h for the static CDNs. ([#68])

- **A raced unlink during an ACL download could raise `OSError` out of the
  tool.** Its two cache-hit branches were hand-copied blocks calling
  `dest.stat()` outside any `try`, so a concurrent deletion between the
  usability check and the stat escaped the module's uniform `{error}`
  contract. Both branches now come from the shared helper — which also makes
  the `anthology_id` / `pdf_url` provenance identical on the cached and fresh
  paths by construction rather than by two copies happening to agree. ([#68])

- **Every DOI in a failed OpenAlex batch chunk shared one error object.**
  `dict.fromkeys(chunk, _parse_error_dict())` put a single dict behind up to 50
  keys, against `parse_error_dict`'s documented "a new dict each call" and
  `cached_lookup`'s deep-copy discipline. Nothing was visibly broken — the sole
  consumer happens to shallow-copy before mutating — but that copy was
  load-bearing without saying so, and `get_works_batch` is public. ([#68])

- **An imported markdown paper with no headings was reported as having
  sections.** `get_paper_sections` answers `sections_detected: false` when the
  converted markdown had no headings at all, so an agent knows the single
  section it gets back is a synthetic `Preamble` and its title means nothing.
  `import_markdown` assembled its own sections-cache entry carrying only
  `sections` and `markdown_checksum` — and because the reader accepts any entry
  whose checksum matches, the missing flag was never recomputed and the tool
  fell back to reporting `true`. That is the reading `sections_note` exists to
  prevent, in the case where it is most likely: a hand-made plain-text
  conversion is exactly what has no headings.

  The entry now has one writer. `papers.store_markdown_and_index()` assembles
  every sections-cache entry, so a payload cannot be built with a key missing;
  `_finalize_markdown` keeps only the post-processing that is specific to
  converter output and delegates the rest. An entry lacking `sections_detected`
  is treated as stale and re-parsed rather than read with a guessed default —
  re-parsing is a file read and a regex pass, no subprocess and no network, so
  computing the true answer costs less than reporting a wrong one.

  Two smaller shape fixes ride along: `convert_paper` now reports
  `sections_detected` on cached calls as well as the first one, and
  `conversion_mode` gained the value `"imported"` for markdown that never ran
  through a converter (distinct from `null`, which means "converted before the
  field existed"). Imported markdown is stored **verbatim** — the image-path
  rewriting the converter path does is right for paths into a deleted
  extraction dir and wrong for an operator's own file. ([#66])

- **`unindexable_note` told the agent the wrong reason.** It said these papers
  are absent from the keyword index because "the tokeniser is ASCII-only, so
  non-Latin scripts yield no terms". Fixing the probe in [#58] made that false;
  the note it was indicting was never updated. It was also never true of the
  files that reach it — they are punctuation- or symbol-only and have no
  letters in *any* script — and it steered the agent toward `find_in_paper` on
  documents with nothing to find. The note is now built from the `reason`
  actually recorded per file, so `unreadable` (an I/O failure, fixed by
  re-importing) no longer gets blamed on encoding either.

  `search_cached_papers` also now documents the CJK limitation in its own
  docstring rather than only in the rules files. `unicode61` does not segment
  Han/Kana/Hangul, so such a paper is indexed but matches only whole
  whitespace-delimited runs — a sub-phrase query returns nothing, with no
  `unindexable` entry to explain it, because the paper *is* indexed. ([#66])

- **`import_paper` blocked the event loop for the length of the import.** The
  tool is `async` but called the synchronous import helpers inline, so copying
  a PDF (up to `MAX_PDF_BYTES`, 200 MB by default) or parsing a large markdown
  file stalled every concurrent tool call for the duration. Both now run in a
  worker thread, the boundary `get_paper_section` and `find_in_paper` already
  used. The markdown branch additionally holds the per-paper sections lock,
  which it never took despite replacing the same markdown + section-index pair
  that `convert_paper` and the `force_refresh` cascade mutate under it. ([#66])

- **`unindexable` reported papers that the index had, in fact, indexed.** The
  probe asked `_tokenize` (ASCII-only) plus an FTS5 `MATCH` for the five ASCII
  vowels, so a paper in Japanese, Cyrillic or Greek was recorded as
  `no_indexable_tokens` even though `unicode61` tokenises on Unicode character
  class and had indexed it perfectly well. Harmless only while the query side
  was equally ASCII-biased; once a non-Latin query could reach the index the
  report became actively misleading, telling the agent via `unindexable_note`
  to fall back to `find_in_paper` on a paper `search_cached_papers` would have
  found. The probe now tests for any Unicode letter or digit, so it catches
  what it was meant to — empty and punctuation-only files — and nothing else.
  The index schema version is bumped so rows carrying a stale flag are
  recomputed rather than lingering behind the `(mtime, size)` signal. ([#58])

  `unicode61` still does not segment CJK: a run delimited by whitespace or
  punctuation is a single token, so CJK papers are findable by whole runs but
  not by sub-phrases. That is the documented trade from the FTS5 move
  (`trigram` measured 842 MB against 165 MB) and is now pinned by a test.

- **A non-Latin query returned nothing, though the index held the term.**
  Moving to FTS5 ([#55]) left `search_cached_papers` gating on `_tokenize` —
  an ASCII-only regex from when this module built its own index. A query of
  purely non-Latin words tokenised to nothing and the search returned early,
  even though the document had indexed cleanly and a raw FTS5 `MATCH` against
  the very same index found it. No `unindexable` entry warned about it either,
  so the agent got a silent empty result. The gate is now the MATCH expression
  itself. Accented and non-Latin hits also resolve a `section_index` again
  rather than centring on the document head — `_tokenize` mangles "Gutiérrez"
  into `guti`/`rrez`, neither of which occurs in the text, so the hit came back
  unnavigable. ([#56])

- **Stopwords leaked into the FTS5 MATCH expression.** Building it from raw
  words ([#55]) dropped the stopword and single-character filter along with
  the ASCII one. FTS5 indexes the raw markdown under `unicode61`, which strips
  neither, so `search("the transformer")` OR-ed in a term matching essentially
  every document and returned papers with no connection to the query.
  `_query_words` keeps that filter; only the ASCII word-splitting is gone. ([#56])

- **`.claude/rules/pipeline.md` still documented the deleted JSON search
  index** — `index.json`, `_INDEX_MEMO`, `_index_sig`, `_load_index`,
  `tf_norm`, the copy-on-write memo and "shard per-namespace" as the next
  lever. None of those five symbols exist in the source any more. That file
  loads whenever the module is edited, so the next reader got a confident,
  detailed description of an architecture that had been replaced two PRs
  earlier — the same class of drift [#51] was written to fix. Rewritten
  against the FTS5 implementation, along with the stale BM25 constants
  (k1=1.5 → FTS5's 1.2) and the chain-by-title advice [#54] superseded.
  `tests/conftest.py` likewise still monkeypatched `_INDEX_MEMO` /
  `_INDEX_MEMO_SIG` with `raising=False`, which had quietly become a no-op.
  ([#56])

- **A full disk turned a successful fetch into an uncaught exception.**
  `cache.put` runs inside every provider's `fetch` closure *after* the network
  request already succeeded, so an `OSError` there (ENOSPC, read-only mount,
  quota) threw away data we had just paid an HTTP request for and propagated
  out of the tool instead of the `{error}` contract. `put` and `put_negative`
  now return whether the write landed and absorb `OSError`, so the caller
  serves its answer uncached; the failure is counted as
  `cache_write_failures` for the operator. Genuine programming errors still
  propagate. ([#56])
- **An empty conversion produced the literal error `out of range (0--1)`.**
  A converter can exit 0 having written an empty markdown file — a 0-page PDF,
  an image-only scan — and every section read then fell through to the range
  check and emitted that. It now says the markdown is empty and suggests the
  likely cause (no extractable text layer). ([#56])
- **The converter's stderr was captured inconsistently.** The command was run
  as `{cmd} 2>&1` with `stderr=PIPE`, which was worse than merely redundant:
  `;` and `&&` bind tighter than the redirect, so streams were merged only for
  a *single-command* converter. With `PDF_CONVERTER_VENV` set — making the
  command `source ... && {cmd}` — the redirect attached to whatever the last
  command happened to be. Whether a converter's error reached the agent
  depended on the shape of the operator's converter string. The redirect is
  gone; stderr is captured on its own pipe and appended last, so a converter
  that keeps logging after it fails can no longer push its own error out of
  the 500-character window. ([#56])
- **Which markdown file became the paper was nondeterministic.** MinerU emits
  several `.md` files per run, and both the exact-stem lookup and the
  fallback used `list(glob(...))[0]` — filesystem order. Both are now sorted,
  the fallback by depth then name, so a top-level output beats one nested in
  a subdirectory. ([#56])

- **Namespace-filtered search no longer changes a paper's score.** Corpus
  statistics were scoped to the filtered subset, so the same paper scored
  differently in a filtered and an unfiltered search. Term rarity is now
  computed over the whole index; `namespace` selects which documents come
  back, not how they are ranked. ([#55])
- **A hit whose term appears in every document is no longer reported as
  score 0.0.** Scores are rounded to 6 decimals rather than 3 — FTS5 returns
  a very small positive score when a term's IDF is degenerate, and rounding
  to 3 crushed it to zero, breaking the invariant that every returned hit
  scores above zero. ([#55])
- **An accented query matches an accented document again.** The move to FTS5
  briefly left the query and the documents tokenised by different tokenizers —
  SQLite indexed "Gutiérrez" as one token while the query still went through
  the module's ASCII-only regex, which split it into `guti OR rrez` and
  matched nothing. Queries are now tokenised by FTS5, the same way the corpus
  is. Folding remains opt-in via `normalize`, unchanged. ([#55])
- **Equal-scoring hits are ordered deterministically** by `(namespace,
  canonical_id)` again, rather than by insertion order into the index. ([#55])

- **The documented `search_cached_papers` → `get_paper_section` chain failed
  for repeated headings.** Corpus search returned the matched section's
  *title*, and the docstring told the agent to chain it into
  `get_paper_section` — which rejects a repeated title as an ambiguous match.
  Measured on a real 2,493-paper corpus, **271 (10.9%) have at least two
  sections sharing a title**, so the documented workflow dead-ended roughly
  one time in nine. Hits now carry `section_index` (and `char_offset`), and
  the docstring directs chaining through the index. ([#54])
- **A paper with no headings was indistinguishable from a one-section
  paper.** Converter output without markdown headings — `pdftotext`'s layout
  mode, notably — collapses to a single synthetic `"Preamble"` section, and
  `get_paper_sections` reported that identically to a genuine one-section
  paper. On a real corpus **every** single-section paper above 100 KB was this
  case: theses and long preprints, where blind paging is the worst available
  reading strategy. Responses now carry `sections_detected`, plus a
  `sections_note` pointing at `find_in_paper` and at re-running with
  `mode="full"`. Indices cached before this flag existed read as detected
  rather than raising a false alarm. ([#54])
- **A cancelled single-flight *follower* took down the leader.** The mirror of
  the fix below, and the likelier direction: cancelling a task cancels the
  future it is suspended on, and for a follower that is the *shared* future.
  One follower giving up therefore cancelled the slot for everyone — the
  leader then called `set_result` on a cancelled future and raised
  `InvalidStateError` into its own caller in place of a perfectly good result,
  while every remaining follower saw the cancellation and re-ran the factory,
  issuing a second outbound request for a key already in flight. Followers now
  await through `asyncio.shield`, so their cancellation stays their own, and
  `set_result` is guarded. The documented fan-out is four parallel calls for
  one paper, so a cancellation lands on a follower three times in four. ([#53])

- **`approx_tokens` disagreed between the two tools that report it.**
  `get_paper_sections` measured the *unstripped* line join, so every section's
  estimate was inflated by its surrounding blank lines and
  `total_approx_tokens` summed the inflated variant, while
  `get_paper_section` measured the stripped text it actually returns. An agent
  budgeting context from the index got a different number than the read
  produced. The index now measures what the reader receives. (Section indices
  already cached keep their old estimates until the paper is re-converted; the
  values are estimates, so no cache invalidation is forced.) ([#53])
- **A cancelled single-flight leader cancelled every follower with it.** The
  leader's task ending — an agent's tool call timing out, say — says nothing
  about the followers' lifetimes, but the shared future carried the
  `CancelledError` to all of them, failing unrelated concurrent calls for the
  same key. A follower that is not itself being cancelled now takes over and
  runs the factory. Genuine failures are still shared, unchanged.
  `SingleFlight` also used `asyncio.get_event_loop()` inside a coroutine
  (deprecated, and it fetches or creates a thread loop when none is running);
  it now uses `get_running_loop`. ([#53])
- **Pooled clients are closed concurrently on shutdown.** `aclose_all` closed
  them one at a time, each bounded by its own 5s timeout, so eight wedged
  sockets could take up to 40 seconds — exactly the lifespan-pinning the
  per-client timeout was added to prevent. Its `except (TimeoutError,
  Exception)` was also redundant (`Exception` already covers `TimeoutError`)
  and implied it handled `CancelledError`, which is a `BaseException` and was
  never caught; cancellation now propagates deliberately, so a cancelled
  shutdown is not reported as a clean one. ([#53])

- **A cancelled conversion orphaned the converter process.** `convert_pdf` and
  `_convert_fast` caught only `TimeoutError`, so on `asyncio.CancelledError`
  (client disconnect, tool-call cancellation, server shutdown) the `finally`
  removed the extraction directory and released the global conversion lock
  while the subprocess was never signalled — a MinerU run kept pinning CPU/GPU
  with its output directory deleted underneath it, and the child was never
  reaped. Both paths now tear the process group down and re-raise. ([#52])
- **A malformed `PDF_CONVERTER` / `PDF_FAST_CONVERTER` escaped as a raw
  exception.** `str.format` raises `KeyError` on an unknown placeholder,
  `IndexError` on a positional one, and `ValueError` on an unbalanced brace —
  none of which is an `OSError`, which is all the full path caught; the fast
  path had no `try` at all. Both builders now raise a named
  `ConverterTemplateError` naming the offending variable and the valid
  placeholders, and both call sites return the `{error, retryable: False}`
  contract. ([#52])
- **`find_in_paper` read cached markdown using the host locale.** It was the
  one read path in the pipeline without an explicit `encoding="utf-8"` — its
  siblings all had one — so under `LC_ALL=C` (containers, systemd units) it
  raised `UnicodeDecodeError` straight out of the tool. It also lacked the
  `FileNotFoundError` guard `get_paper_section` has, so a concurrent
  `force_refresh` cascade between the `exists()` check and the read raised
  instead of degrading to the "not converted" error. ([#52])
- **Papers the keyword index cannot use are now reported instead of vanishing.**
  The tokeniser is ASCII-only, so a paper in Chinese, Russian, Greek or Arabic —
  or one whose extracted text is mostly mathematical symbols — yields no terms,
  was dropped from the index, and became permanently invisible to
  `search_cached_papers` with no error and no diagnostic. Such files are now
  tracked with a reason and surfaced as `unindexable_count` / `unindexable` on
  the search response, pointing at `find_in_paper` as the way to read them. The
  index gained an optional key without a version bump, so existing indexes load
  unchanged and no rebuild is triggered; a tracked file is also no longer
  re-read and re-tokenised on every refresh. ([#52])

- **`download_pdf`'s docstring told the agent the opposite of what the code
  does.** It stated that re-downloading does *not* invalidate converted
  markdown and that `force_refresh` must be passed to `convert_paper` too —
  but the cascade has been implemented all along, and both `_app.py`'s server
  instructions and `.claude/rules/server.md` describe it correctly. The one
  string the model actually reads at call time was the only wrong one, and it
  induced a redundant multi-minute conversion. ([#51])
- **`get_paper_sections` documented a field that does not exist.** Section
  entries were described as `{index, title, preview, approx_tokens}`; there is
  no `preview` key anywhere in the source — the field is `h3s`. `convert_paper`
  described the same objects correctly. ([#51])
- **`force_refresh` never reached the Crossref fallback.**
  `get_paper_metadata(doi, force_refresh=True, fallback_crossref=True)` served
  a stale cached Crossref record. `_fetch_crossref_work` has always accepted
  and forwarded `force_refresh` — the graph tools pass it — but this call site
  dropped it, in the one path that exists specifically for brand-new
  DOIs. ([#51])
- **Multi-source errors lost their retry payload.** `_source_error` forwarded
  only `error` / `retryable` / `suggestion`, discarding `retry_after_seconds`,
  `backpressure`, `max_concurrency` and `not_found` — exactly the fields that
  let an agent tell a definitive miss from a transient one and act on it,
  which is what the helper's own docstring says it exists to preserve. ([#51])
- **`.env` was silently ignored for an installed wheel.** The path was resolved
  as `<package>/../../../.env`, which is the project root from a source
  checkout and meaningless from `site-packages` — so every environment variable
  was dropped in a mode the project explicitly supports (`pyproject.toml` ships
  a console script; `.env.example` tells operators to set `CACHE_DIR` "when
  running from an installed wheel"). Resolution now tries
  `ACADEMIC_TOOLS_ENV_FILE`, the project root, `$PWD/.env`, then
  `$XDG_CONFIG_HOME/academic-tools-mcp/.env`; source checkouts are
  unchanged. ([#51])

- **Crossref was requesting at polite-pool rates without a polite-pool
  identity.** The rate constants were hardcoded to the polite tier
  unconditionally, while the `User-Agent` carrying `CROSSREF_MAILTO` was set
  only when one was configured. So the documented default — an empty `.env`,
  which the README calls a supported starting point — requested at **2x the
  public-pool rate, 3x its concurrency, and 10x its search rate**, while
  identifying itself as `python-httpx/x.y`. The tier is now resolved from
  whether a contact address is present. Search is also paced separately
  (Crossref limits it far more tightly than singleton lookups); it previously
  shared the singles throttle, so the search limit was never enforced in
  either pool. ([#50])
- **Three providers and the open-access download path sent no `User-Agent` at
  all.** `biorxiv`, `opencitations`, `acl_anthology` and `oa_download` passed
  no headers, so they went out as `python-httpx/x.y` — the generic agent
  several upstreams throttle hardest. ACL was the least polite configuration
  in the tree: four parallel anonymous PDF pulls at zero gap from a nonprofit.
  The four agents that *were* sent advertised
  `https://github.com/academic-tools-mcp`, which does not exist, and a version
  hardcoded to `1.0`. All eight now share one `_useragent` builder emitting the
  real repository URL and the real package version. ([#50])
- **`Retry-After` in HTTP-date form was silently discarded.** RFC 9110 permits
  both a delay-seconds and an HTTP-date value, and Wikimedia- and
  Cloudflare-fronted endpoints emit dates. Only the numeric form was parsed, so
  a date fell back to our own backoff — as little as 1.0s against a server that
  had just asked for minutes — and `retry_after_seconds` was omitted from the
  error, leaving the agent no hint either. Both forms are now honoured, a naive
  date is read as UTC rather than local time, and non-finite values are
  rejected. ([#50])
- **`retry_after_seconds` reached the agent unclamped.** The internal retry path
  has always honoured a 600s ceiling, but the error dict surfaced the raw
  header, so a misconfigured `Retry-After: 86400` told the agent to wait a
  day. ([#50])
- **Observability counters were wrong in both directions.** `http_calls` was
  incremented once per throttle *slot*, but a slot issues up to
  `retry_attempts` real requests — under-reporting actual outbound volume by up
  to 3x for arXiv, which is exactly the number a politeness audit reads. And
  because `cached_lookup` checks the cache twice (outer, then again inside the
  single-flight slot), a single genuine miss registered **two** misses while a
  hit registered one, making the reported hit rate systematically wrong.
  Requests are now counted per outbound attempt, and `cache.get(count=False)`
  suppresses counting for re-checks and cache-warming probes. ([#50])

- **The same DOI could land under three different cache keys.** DOI
  normalization existed in six copies — four byte-identical — and only the two
  that had been improved (`manual`, `biorxiv`) handled `dx.doi.org` and a
  case-insensitive `doi:` prefix. So `https://dx.doi.org/10.x/y` and
  `DOI:10.x/y` were left unnormalized by OpenAlex, Crossref, OpenCitations and
  ACL, producing distinct cache entries *and* malformed upstream URLs
  (`/works/doi:https://dx.doi.org/...`). All six also sliced the `doi:` prefix
  without re-stripping, so a pasted `"doi: 10.x/y"` became `" 10.x/y"` and was
  reported to the agent as an unknown identifier. Normalization now lives once
  in `_doi`, carrying the union of what the six did. ([#49])
- **Malformed upstream JSON escaped the `{error}` contract on Crossref and
  bioRxiv.** Five of seven providers guarded the decoded payload with
  `isinstance`; these two were missed. A 200 whose body is `null` or a scalar
  made `"message" not in data` raise `TypeError` (Crossref) and
  `data.get("collection")` raise `AttributeError` (bioRxiv) — neither is in
  `_PARSE_ERRORS` or `HTTPX_ERRORS`, so both propagated out of the tool.
  bioRxiv's `raw.get("server", "").lower()` also raised on a present-but-null
  key, nine lines above a sibling field that already handled it correctly.
  A malformed bioRxiv payload is now distinguished from a legitimately empty
  one, so it stays retryable instead of being negative-cached as "not
  found". ([#49])
- **`get_works_batch` could negative-cache DOIs that exist.** Any requested DOI
  absent from `results` was cached as not-found for 24h — including one whose
  OpenAlex-stored DOI string merely differed from the request (the loop
  `continue`s past those), and with no check of `meta.count` against the number
  of records returned, so a truncated response poisoned the rest of the chunk.
  A missing DOI is now only treated as definitive when the response accounted
  for everything; otherwise it is returned `retryable`. Records returned under
  an unexpected DOI are cached under their own key instead of discarded. ([#49])
- **arXiv served the wrong version's metadata and PDF bytes.** The cache key
  stripped the version suffix (`2301.00001v2` → `2301.00001`) while the API
  request kept it, so whichever version was fetched first won the shared key.
  Every later version was a silent cache hit returning the first one's title,
  abstract, and authors — and `download_pdf` handed back the first one's bytes
  marked `cached: True`. `force_refresh` could not help: it invalidated that
  same shared key. The version is now part of the key, so `2301.00001v1`,
  `2301.00001v2`, and the bare `2301.00001` ("latest") are three distinct
  entries. `search_arxiv` warms both the versioned and bare keys, since search
  always returns the current version. ([#48])
- **A zero-length HTTP 200 installed a permanent 0-byte PDF.** With no chunks
  the streaming write loop never ran, the `%PDF-` sniff never fired, and the
  atomic rename installed an empty file reported as a successful download.
  Every downstream `dest.exists()` then treated it as cached forever and
  `convert_paper` handed it to the converter. `stream_to_file` now rejects an
  empty body, and the three native PDF providers plus `convert_paper` validate
  cached files through the shared `_pdf_download.is_usable_pdf` (non-zero size
  + `%PDF-` header) instead of a bare existence check — the guard that until
  now only the manual-import and open-access paths used. ([#48])
- **BibTeX author fields were never escaped**, so an author or organisation
  containing `&`, `%`, `$`, `#`, or `_` (`AT&T Labs`, `Sanofi-Aventis R&D`, and
  every OpenAlex org-authorship) produced a `.bib` that fails to compile with
  *"Misplaced alignment tab character &"*. Title, journal, and DOI were escaped
  from the start; the author path was not. DOIs interpolated into
  `howpublished={\url{...}}` were also raw — an unescaped `%` there comments
  out the rest of the file. `_escape_doi` now covers `{`, `}`, `\`, `$`, `~`,
  and `^` as well, in a single pass so a backslash's own escape braces are not
  re-escaped. ([#48])
- **Two imported papers could silently overwrite each other's PDF.** Derived
  paths used two different sanitizers: PDFs collapsed unsafe characters to `_`
  (so `"a b"` and `"a_b"` became the same `a_b.pdf`) while markdown replaced
  only `/` (so the same two kept separate `.md` files). The PDF and markdown
  caches therefore disagreed about identity. All three derived paths — PDF,
  markdown, and the sections key — now share one `papers.safe_stem`, which
  percent-encodes rather than collapses and so is no longer lossy. ([#48])
- **Tests can no longer write to the real cache or reach the network.** Two
  autouse fixtures were added to `tests/conftest.py`. The first points
  `cache._CACHE_ROOT` at each test's private `tmp_path`: 12 of the 28 test
  modules never patched it themselves, so a single missed monkeypatch wrote
  into the operator's real `.cache/` (which reaches tens of GB on a working
  install). The second blocks outbound socket connections to anything but
  loopback, so a stubbed-out client that is missed fails loudly instead of
  silently spending polite-pool budget against a live API. Tests that need a
  different cache layout still override the root; the 52 now-redundant
  per-test patches were removed. ([#47])
- **`papers`' module-level conversion state is reset between tests.**
  `_global_convert_lock`, `_current_conversion`, and `_section_locks` leaked
  across tests, and only one test module reset them locally. The lock binds to
  the running event loop the first time it is *contended*, so a stale lock was
  a latent "bound to a different event loop" failure — masked today only by
  `convert_pdf`'s `if _global_convert_lock.locked()` short-circuit, which means
  the lock is never actually contended. That accident is no longer
  load-bearing. ([#47])

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
[#47]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/47
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
[#67]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/67
[#68]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/68
[#69]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/69
[#73]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/73
[#74]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/74
[#76]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/76
[#77]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/77
[#78]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/78
[#79]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/79
[#81]: https://github.com/hunter-heidenreich/academic-tools-mcp/pull/81
