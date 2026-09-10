---
paths:
  - "src/academic_tools_mcp/**/*.py"
  - ".claude/rules/*.md"
  - "CLAUDE.md"
---

# Python design contracts

Style — formatting, import order, line length, typing hygiene — is enforced by
`ruff` and `mypy`, configured in `pyproject.toml` and auto-applied on edit.
**Do not restate style rules here or hand-format code; let the tools do it.**

These are the contracts a linter can't check. Each is falsifiable — open the
cited exemplar and match it.

## Module names and where a file lives

Both rules are machine-checked by `tests/test_layering.py`, so unlike the rest of
this file, violating them fails CI rather than review — its module docstring and
`test_no_module_name_starts_with_an_underscore` carry the reasoning. The rules
themselves:

- **No module carries a leading underscore.** The package has no public surface
  to be private *from*. Symbol-level underscores are unaffected and still mean
  what `.claude/rules/store.md` says they mean.
- **A module is named for the one thing it owns**, as a lowercase noun with no
  separator (`http`, `throttle`, `cache`, `stems`, `bibtex`). Join two words only
  when the single noun would be ambiguous *inside its own directory* —
  `fast_extract`, `textnorm`. The directory supplies the qualifier, so a module
  never repeats its package's name.
- **A module name must not collide with a common local in the code that imports
  it.** That is a shadowing bug, not a style question: `util/doinorm.py` is not
  `doi.py` because `doi` is the parameter name in most of the providers and in
  `tools/graph`, so a module of that name resolves to the string, not the module,
  at nearly every call site that needs it.

**A module's name is independent of its cache `NAMESPACE`.** The namespace is an
on-disk directory under `.cache/` and a rename must never move it:
`providers/acl.py` still declares `NAMESPACE = "acl_anthology"`, and that
directory is live.

## A module never computes anything from its own depth

**`Path(__file__).parents[n]` and `__name__.rsplit('.', 1)` are the bug a file
move cannot fail loudly on.** Both encode *where the module currently sits* into
a value about something else, so relocating the file silently changes the answer
— no import error, no failing test unless one pins the real value. Three live
sites would each be a different silent breakage:

- **`store/cache.py` resolves `.cache/`.** A root off by one directory is an
  empty cache and a re-download of the whole corpus, with every artifact already
  on disk orphaned.
- **`util/config.py` resolves the source-checkout `.env` candidate.** Wrong here
  hides behind the *next* candidate, `Path.cwd() / ".env"`, so it works from the
  project root and silently stops applying operator config from anywhere else.
- **`net/stats.py` derives the package prefix its `sys.modules` scan filters on.**
  Narrowed to a subpackage, `throttles()` returns nothing for any provider,
  taking the reset seam and the `in_flight` sample with it — and reporting zero,
  not an error.

**Resolve by name.** `config.project_root()` walks up to the directory named for
the top-level package and is the single home for it — `store.cache` imports it
rather than keeping a second copy, because the two sit at different depths and a
`parents[n]` is correct for at most one of them. Derive a package prefix with
`__name__.split('.', 1)[0]`, the root at any depth.

**And pin the real value in a test** (`.claude/rules/testing.md`
§ Pinning a resolved path).

## Layer order

Lowest first, and **a module may import from its own layer or any lower one,
never a higher one**:

`leaf` (imports nothing from the package) → `net` → `store` → `download` →
`providers` → `content` → `app` → `tools` → `entry`.

**`_LAYERS` in `tests/test_layering.py` is the authority** — the table itself,
and the comments beside it explaining why `download/` splits across two ranks and
what makes membership of `leaf` checkable. Edit the classification there; a
module missing from it fails CI rather than silently escaping every check.

Three consequences worth stating on this side:

- **`app` never imports `tools`**, and no `tools/*` module imports another. A
  helper two tool modules need moves *down* into `app`, never sideways.
- **`httpx` lives in the network layer and the HTTP clients, nowhere else** —
  `_MAY_IMPORT_HTTPX` plus every `providers/*`. A tool, pipeline or content
  module that imports it is a layering violation: `tools/pipeline.py` drives the
  whole download → convert → sections pipeline with no `import httpx`.
- **A type-only import is not a layer edge.** `stats` annotates a `Throttle`
  under `if TYPE_CHECKING:` while `throttle` imports `stats` for real; that is a
  legitimate pair, not a cycle.

## Single-homed infrastructure

Each of these has exactly one home. Need the behaviour in a new module? Route
through the existing one — construct a `Throttle`, call `cached_lookup` — don't
fork a local copy of the gating, the force_refresh→check→single-flight→re-check
dance, or a DOI regex. **Server tools return slices, not whole objects**: an LLM
agent never receives a raw provider response.

| Concern | Home | Detail |
|---|---|---|
| Caching, cached-getter protocol | `store/cache.py`, `cache.cached_lookup` | `store.md` |
| Artifact naming, checksums | `store/stems.py` | `store.md` |
| Atomic writes | `store/atomic.py` | `store.md` |
| Single-flight | `store/singleflight.py` | `store.md` |
| Cached-download protocol | `streaming.cached_download` | `download.md` |
| Throttling | `throttle.Throttle` | `net.md` |
| Retry | `http.get_with_retry` | `net.md` |
| Agent-facing error vocabulary | `http.error_dict` / `parse_error_dict` / `not_found` | `net.md` |
| Counters | `net/stats.py` | `net.md` |
| Config | `util/config.py` | `util.md` |
| DOI normalization | `doinorm.normalize` / `canonical` / `looks_like_doi` | `util.md` |
| ORCID normalization | `orcidnorm.normalize` / `canonical` / `looks_like_orcid` | `util.md` |
| Outbound User-Agent | `useragent.headers` | `util.md` |
| Page arithmetic | `app.page_bounds` | `server.md` |
| Markdown read outside the lock | `app.read_markdown` | `server.md` |

`stems` shows why the *layer* matters, not just the count: it holds `safe_stem`
and the path builders, so three providers can name a PDF without importing the
PDF converter and its subprocess machinery.

## Single responsibility — one job per unit

- **One paper tool per job, not per provider.** The four unified tools
  (`get_paper_metadata` / `_authors` / `_abstract` / `_bibtex`) dispatch
  internally via `manual.resolve_metadata_source()`. Don't branch on provider
  *inside* a tool, and don't add a fifth `get_<provider>_metadata` — extend the
  dispatcher.
- **A new API provider mirrors an existing one.** `providers/biorxiv.py` is the
  fullest instance of the shape; a provider that invents its own concurrency or
  caching scheme is a bug, not a feature. Use the `add-provider` skill.
- **Keep a trust boundary in its own small module.** `download/openaccess.py`
  exists so the "fetch only what OpenAlex surfaced" rule has one enforcement site
  — the alternative is an `allow_arbitrary` flag threaded through the whole
  download stack. **Widen a boundary by adding a module, never by adding a
  parameter.**

## DRY without over-abstraction

**Don't abstract across providers that merely look similar.** The shared
*mechanism* is single-homed; the per-provider *policy and quirks* stay in each
module and must not be collapsed: arxiv's single-connection `_MAX_CONCURRENT`,
biorxiv's late-appearing `published_doi` + medRxiv fallback. Pass those as
constructor args / `fetch` closures — don't fold them into the shared code, and
don't collapse the seven clients into one generic class to chase DRY.

**The standing proposal this rule exists to refuse: a shared "JSON singleton GET"
helper** generalizing `openalex._fetch_singleton` to also serve
`crossref.get_work`, `wikipedia.get_summary` and `opencitations._fetch_direction`.
Their `_fetch` bodies do share a skeleton — `addresses_a_record` →
404-before-`raise_for_status` → `put_negative` → shape guard → `cache.put` — but
the skeleton is not what those lines *are*. Roughly half of them are comments
naming what **this** provider's shortened path returns (crossref's `/works`
collection clears its own shape ladder; wikipedia's `/page` answers 200 with a
dict; opencitations' empty list is a real answer), and those neither move into a
helper nor survive without the branch they annotate. Two further tells: the
extraction step needs a `dict | None` sentinel protocol invented for the
occasion, splitting each guard from the comprehension or projection it protects;
and `arxiv` (XML, three not-found shapes) and `biorxiv` (two servers) cannot use
it — the two the `add-provider` skill names as the exemplar shape. The genuinely
shared pieces already have homes in `http` and `cache`; what is left is about
five lines. **Do the smaller thing instead**: give each provider's shape guard a
name (`crossref._message_of`, `wikipedia._summary_of`, `opencitations._edges_of`,
`biorxiv._collection_of`), which is where the clarity actually was. Revisit only
if a *fifth* JSON-singleton provider lands.

## No mode flags that fork behaviour

Prefer a new function or module over a boolean parameter that makes one function
do two unrelated things. `force_refresh` and `normalize` are acceptable: each
toggles one orthogonal axis and the response shape is unchanged either way.
`follow_published` is the standing exception, not a precedent — when the chain
succeeds it swaps the whole key set, which is why the tool docstring has to
enumerate both shapes and why `get_papers_metadata` refuses to support it. **A
parameter that returns a *different shape* depending on its value belongs in its
own tool.**

## Comments — outer context, not narration

Keep comments **brief** and reserve them for what the code cannot say about
itself.

- **Do state the non-obvious**: why this ordering is load-bearing, which upstream
  quirk forces a branch, what an unusual constant is protecting against, which
  invariant a block holds.
- **Prefer one line to a paragraph.** A comment that needs several sentences is
  usually describing a design decision — those belong in the matching
  `.claude/rules/` file, where they load once for the whole module rather than at
  one call site. **Don't cite that file from the source.** It auto-loads from its
  own `paths:` frontmatter whenever the module is touched, so a pointer to it is
  a line that tells the reader nothing they don't already have.
- Docstrings are the exception to brevity: a `@mcp.tool` docstring *is* the
  agent-facing API description (`.claude/rules/server.md`), so it carries
  parameters and response keys in full.

## Writing the docs: when you change a contract, update its home

**The source comment is the home.** A `.claude/rules/` file states only what
cannot sit next to a line of code: cross-module contracts, upstream API facts,
accepted limitations, and prohibitions. A rules file loads *because* its source
file was touched — the session is about to read that file anyway, so restating
its docstrings buys nothing and costs context every session. Anything a docstring
already says gets deleted from the rules file, not summarised there.

If you change a contract — a new gating model, a new dispatch path — update the
matching doc in the same change, and add the `CHANGELOG.md` `[Unreleased]` bullet
if the change is user-facing.

Four conventions keep those docs from rotting:

- **Cite symbols, never line numbers.** `manual.resolve_metadata_source()` stays
  correct across every edit; `manual.py:74` is wrong the next time anyone adds an
  import.
- **Don't transcribe constants.** Name the constant and explain the policy —
  `_BATCH_CHUNK_SIZE`, not `50` — and let the reader grep the value. A copied
  number goes stale silently while the prose around it stays true.
- **Don't transcribe history.** State the invariant the code holds *now* and what
  breaks if you violate it — not what the code used to be. `CHANGELOG.md` and
  `git log -S "<phrase>"` are the homes for "why it changed". Rewrite
  `# X used to happen, which broke Y` as `# Invariant: Y`. Test docstrings are
  exempt — a regression test's purpose *is* the regression.
- **Don't cite tests in these rules files.** Name the invariant precisely enough
  to grep for; a `::TestClass::test_method` node ID is a symbol that churns faster
  than the code it guards. **One exception, and it is narrow**: where a test *is*
  the authority for a rule rather than evidence for it —
  `tests/test_layering.py`'s `_LAYERS` is the layer order, there is no other copy
  — name the file and the constant, never a node ID. A rule that fails CI has to
  say where to edit it.
