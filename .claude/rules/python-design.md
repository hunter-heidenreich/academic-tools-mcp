---
paths:
  - "src/academic_tools_mcp/**/*.py"
---

# Python design contracts

## Module names and where a file lives

Machine-checked by `tests/test_layering.py`, so these fail CI rather than review:

- **No module carries a leading underscore.** The package has no public surface to
  be private *from*. Symbol-level underscores are unaffected.
- **A module is named for the one thing it owns**, a lowercase noun with no
  separator (`http`, `throttle`, `cache`, `stems`). Join two words only when the
  single noun would be ambiguous *inside its own directory* (`fast_extract`,
  `textnorm`). A module never repeats its package's name.
- **A module name must not collide with a common local at its call sites.** That
  is a shadowing bug, not a style question: `util/doinorm.py` is not `doi.py`
  because `doi` is the parameter name almost everywhere it is imported.

**A module's name is independent of its cache `NAMESPACE`.** The namespace is a
live directory under `.cache/`; a rename must never move it.

## A module never computes anything from its own depth

**`Path(__file__).parents[n]` and `__name__.rsplit('.', 1)` encode where a module
currently sits into a value about something else**, so a file move silently
changes the answer — no import error, no failing test. Three live sites would each
break differently, and each says so in a comment: `store/cache.py`,
`util/config.py`, `net/stats.py`.

**Resolve by name.** `config.project_root()` walks up to the top-level package
directory and is the single home for it; derive a package prefix with
`__name__.split('.', 1)[0]`. **And pin the real value in a test.**

## Layer order

Lowest first. **A module may import from its own layer or any lower one, never a
higher one:**

`leaf` (imports nothing from the package) → `net` → `store` → `download` →
`providers` → `content` → `app` → `tools` → `entry`.

**`_LAYERS` in `tests/test_layering.py` is the authority** — edit the
classification there; a module missing from it fails CI. Three consequences:

- **`app` never imports `tools`**, and no `tools/*` module imports another. A
  helper two tool modules need moves *down* into `app`.
- **`httpx` lives in the network layer and the HTTP clients, nowhere else.**
  `tools/pipeline.py` drives the whole download → convert → sections pipeline with
  no `import httpx`.
- **A type-only import is not a layer edge.** `stats` annotates a `Throttle` under
  `if TYPE_CHECKING:` while `throttle` imports `stats` for real — a legitimate pair.

## Single-homed infrastructure

Each of these has exactly one home. Need the behaviour in a new module? Route
through the existing one — don't fork a local copy of the gating, the
force_refresh→check→single-flight→re-check dance, or a DOI regex.

| Concern | Home |
|---|---|
| Caching, cached-getter protocol | `cache.cached_lookup` |
| Artifact naming, checksums | `store/stems.py` |
| Atomic writes | `store/atomic.py` |
| Single-flight | `store/singleflight.py` |
| Cached-download protocol | `protocol.cached_download` |
| Throttling | `throttle.Throttle` |
| Retry | `http.get_with_retry` |
| Agent-facing error vocabulary | `http.error_dict` / `response_error_dict` / `parse_error_dict` / `not_found` |
| Counters | `net/stats.py` |
| Config | `util/config.py` |
| DOI / ORCID normalization | `doinorm` / `orcidnorm` `normalize` · `canonical` · `looks_like_*` |
| Outbound User-Agent | `useragent.headers` |
| Page arithmetic | `app.page_bounds` |
| Markdown read outside the lock | `app.read_markdown` |

`stems` shows why the *layer* matters, not just the count: it holds `safe_stem`
and the path builders, so three providers can name a PDF without importing the
converter and its subprocess machinery.

## One job per unit

- **One paper tool per job, not per provider.** The four unified tools dispatch
  internally via `manual.resolve_metadata_source()`. Don't branch on provider
  *inside* a tool, and don't add a fifth `get_<provider>_metadata` — extend the
  dispatcher.
- **Keep a trust boundary in its own small module**, as `download/openaccess.py`
  does. **Widen a boundary by adding a module, never by adding a parameter.**
- **Server tools return slices, not whole objects.** An agent never receives a raw
  provider response.

## No mode flags that fork behaviour

**A parameter that returns a *different shape* depending on its value belongs in
its own tool.** `follow_published` is the standing exception, not a precedent — it
swaps the whole key set on success, which is why `get_papers_metadata` refuses it.

## Comments

**A design decision belongs in the matching rules file, not in a comment — and
don't cite that file from the source.** It auto-loads whenever the module is
touched, so the pointer tells a reader nothing it doesn't already have.
