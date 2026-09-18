---
paths:
  - "tests/**/*.py"
---

# Testing — pytest, hypothesis, ZOMBIES

Async tests carry an explicit `@pytest.mark.asyncio` because `asyncio_mode =
"strict"` is pinned rather than inherited.

## Isolation

Five autouse fixtures in `tests/conftest.py` isolate every test: operator config
scrubbed, pooled-client and throttle state reset, cache root redirected to
`tmp_path`, conversion state cleared, and **real network blocked** — a test that
needs a response fakes the transport, never the internet.

The config scrub has a second half a fixture cannot do: `conftest` clears the roster
and pins `ACADEMIC_TOOLS_ENV_FILE` at **module scope, above its own package
imports**, because `server._DEBUG_TOOLS_ENABLED` and `cache.CACHE_ROOT` are captured
at import.

**Add a setting, add it to `_CONFIG_ENV_VARS`.** `test_conftest_guards` AST-scans
the package for `config.get` / `flag` / `number` keys and fails if the roster is
narrower.

**A test pinning a resolved path must derive its expectation from where the
*package* is installed, never by counting parents the way the code does** — spelled
the same way, it agrees with a broken value. `conftest` redirects `CACHE_ROOT` for
the whole suite, so these assertions are the only thing that makes a bad file move
fail.

The coverage floor lives in the CI step, deliberately not in `addopts`, so a
single-file run doesn't fail it.

## Layout mirrors `src/`

`tests/<pkg>/test_<module>.py` tests `src/academic_tools_mcp/<pkg>/<module>.py`, and
a property suite is the same name plus `_properties`. Two deliberate exceptions keep
the mirror honest rather than a lie: a test whose subject is a **top-level module**
lives at `tests/` root beside it, and a suite that **deliberately spans packages**
does too. Forcing either into a directory would name one owner for a test that has
several.

**Shared fakes and fixtures are not test modules** and live in `tests/helpers/`;
import them absolutely, so a file's depth is not part of its imports.

## Cover the ZOMBIES

- **Z/O/M/B** — empty, single-element, many, and the boundary itself: **exactly at
  a cap must pass, one past it must fail.**

  Two "many" cases are easy to write and exercise nothing: `get_works_batch` past
  `_BATCH_CHUNK_SIZE` chunks the *cache misses*, not the argument, so a warm cache
  tests none of it; and page 2 of a capped author list is a different path from
  page 1.
- **I/E/S** — the seam contract (what a `fetch` closure may return; `cached_lookup`
  and `cached_download` earn their tests once, not per provider), the error paths
  (transient vs. definitive, what gets negative-cached — most of the interesting
  behaviour here), and the happy path.

## Property-based tests

Use `hypothesis` where the invariant is stronger than any example, and **prefer it
over hand-rolled fuzzing loops**. Reach for it when you catch yourself writing a
fifth example of the same rule — BibTeX escaping is the standing candidate,
currently covered by examples only.

**Test docstrings are exempt from "don't transcribe history"** — a regression test's
purpose *is* the regression.
