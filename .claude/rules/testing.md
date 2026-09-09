---
paths:
  - "tests/**/*.py"
---

# Testing — pytest, hypothesis, ZOMBIES

`pytest` is the runner; async tests carry an explicit `@pytest.mark.asyncio`
because `asyncio_mode = "strict"` is pinned in `pyproject.toml` rather than
inherited.

## Isolation

Five autouse fixtures in `tests/conftest.py` isolate every test: operator
configuration scrubbed from the environment, pooled-client and throttle state
reset, cache root redirected to `tmp_path`, conversion state cleared, and **real
network blocked** — a test that needs a response fakes the transport, never the
internet.

The config scrub has a second half a fixture cannot do: `conftest` clears the
roster and pins `ACADEMIC_TOOLS_ENV_FILE` at **module scope, above its own
package imports**, because `server._DEBUG_TOOLS_ENABLED`, `cache.CACHE_ROOT` and
crossref's pacing constants are captured at import.

**Add a setting, add it to `_CONFIG_ENV_VARS`.** `test_conftest_guards`
AST-scans the whole package for `config.get` / `flag` / `number` keys —
following one level of wrapper indirection, since `papers/convert._resolve_timeout`
passes its own parameter through — and fails if the roster is narrower, if a key
is beyond what it can resolve, or if the scan stops reaching every subpackage.

The coverage floor lives in the CI step, deliberately not in `addopts`, so a
single-file run doesn't fail it.

## Pinning a resolved path

`tests/conftest.py` redirects `cache.CACHE_ROOT` to `tmp_path` for the whole
suite, so **nothing else exercises the real resolution** — the assertions in
`tests/store/test_cache.py` and `tests/util/test_config.py` are the only thing
that makes a bad file move fail (`.claude/rules/python-design.md` § A module
never computes anything from its own depth).

**A test must derive its expectation from where the *package* is installed,
never by counting parents the way the code does** — spelled the same way, it
agrees with a broken value.

## Layout mirrors `src/`

`tests/<pkg>/test_<module>.py` tests
`src/academic_tools_mcp/<pkg>/<module>.py`, and a property suite is the same name
plus `_properties`.

Two deliberate exceptions, and they are what keeps the mirror honest rather than
a lie:

- A test whose subject is a **top-level module** lives at `tests/` root beside it
  (`test_bibtex`, `test_corpus`, `test_manual`, `test_server`, `test_fast_extract`).
- A suite that **deliberately spans packages** lives at the root too
  (`test_politeness`, `test_layering`, `test_failure_modes`, `test_conftest_guards`,
  `test_section_navigation`, `test_pipeline_robustness`, `test_cross_module`).

Forcing those into a directory would name one owner for a test that has several.

**Shared fakes and fixtures are not test modules** and live in `tests/helpers/`;
import them absolutely (`from tests.helpers.download_fakes import ...`), never
relatively, so a file's depth is not part of its imports.

## Cover the ZOMBIES

- **Z/O/M/B** — empty (`top_k <= 0` → `[]`, a paper with no sections),
  single-element, many, and the boundary itself: **exactly at a cap must pass,
  one past it must fail.** `stream_to_file` aborts on
  `written + len(chunk) > max_bytes`, so a PDF of exactly `MAX_PDF_BYTES`
  succeeds — test both sides.

  Two "many" cases are easy to write and exercise nothing: `get_works_batch` past
  `_BATCH_CHUNK_SIZE` chunks the *cache misses*, not the argument, so a warm cache
  tests none of it; and page 2 of an `AUTHORS_PAGE_SIZE`-capped author list is a
  different path from page 1.
- **I/E/S** — the seam contract (what a `fetch` closure may return;
  `cached_lookup` / `cached_download` earn their tests once, not per provider),
  the error paths (transient vs. definitive, what gets negative-cached, what stays
  retryable — most of the interesting behaviour here), and the happy path.

## Property-based tests

Use `hypothesis` where the invariant is stronger than any example:
`tests/util/test_doinorm_properties.py` pins that every spelling of one DOI
collapses to one cache key and that `doinorm.canonical` is idempotent.

Reach for it when you catch yourself writing a fifth example of the same rule —
BibTeX escaping is the standing candidate, currently covered by examples only.
**Prefer it over hand-rolled fuzzing loops.**

## Test docstrings are exempt from "don't transcribe history"

A regression test's purpose *is* the regression (`.claude/rules/python-design.md`
§ Writing the docs).
