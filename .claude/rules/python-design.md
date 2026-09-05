---
paths:
  - "src/academic_tools_mcp/**/*.py"
---

# Python design contracts

Style (formatting, import order, line length, typing hygiene) is enforced by tooling — `ruff format`, `ruff check`, `mypy`, configured in `pyproject.toml`, and auto-applied on edit by `.claude/hooks/ruff-format.sh`. **Do not restate style rules here or hand-format code; let the tools do it.**

This file carries the things a linter *can't* check: the layering and single-responsibility contracts that keep this codebase coherent. Each is falsifiable against real code — when in doubt, open the cited exemplar and match it.

## Layering — tools never reach past their layer

- **No raw `httpx` outside an API-client module.** Every outbound request goes through the shared layer (`_http.py` → `_clients.py`). A tool or pipeline module that imports `httpx` directly is a layering violation. The retry, backpressure, `Retry-After`, and stats behaviour lives in `_http.py` — reuse it, never re-implement it per call site.
- **Server tools return slices, not whole objects.** A tool fetches the full cached provider object, then returns only the relevant fields (see the unified paper tools in `tools/paper.py`). An LLM agent should never receive a raw OpenAlex response. New tool → extract a lean slice.
- **Shared infrastructure is single-homed.** Caching (`cache.py`), the cached-getter protocol (`cache.cached_lookup`), throttling (`_throttle.Throttle`), single-flight (`_singleflight.py`), retry (`_http.get_with_retry`), counters (`_stats.py`), and config (`config.py`) each have exactly one home. Need that behaviour in a new provider? Route through the existing module — construct a `Throttle`, call `cached_lookup` — don't fork a local copy of the gating or the force_refresh→check→single-flight→re-check dance.

## Single responsibility — one job per unit

- **One paper tool per job, not per provider.** The four unified tools (`get_paper_metadata` / `_authors` / `_abstract` / `_bibtex`) take any identifier and dispatch internally via `manual.resolve_metadata_source()`. Don't branch on provider *inside* a tool, and don't add a fifth `get_<provider>_metadata` variant — extend the dispatcher instead. Responses tag `_source` / `_canonical_id` so callers branch on provider-specific fields downstream.
- **A new API provider mirrors an existing one.** `providers/arxiv.py` and `providers/crossref.py` are the canonical shapes: pooled `httpx.AsyncClient`, a `_throttle.Throttle` instance (gating via `Throttle.slot`/`.get`, see `providers/arxiv.py`) exposed through thin `_throttled_get`/`_request_slot` wrappers, each getter driven by `cache.cached_lookup` (single-flight by canonical id, force_refresh, 404 → negative cache inside the `fetch` closure), positive-cache TTL eviction, `_stats` counters. Same shape every time — a provider that invents its own concurrency or caching scheme is a bug, not a feature.
- **Narrow, named exceptions over broad behaviour.** The OA-download path only fetches the OA URL OpenAlex already surfaces (`openalex.best_pdf_url`, `providers/openalex.py`) — never a caller-supplied URL. Keep such trust boundaries in one small module (`oa_download.py`) rather than threading an `allow_arbitrary` flag through the download stack.

## DRY without over-abstraction

- **Reuse the primitive; don't re-derive it.** Throttling, retry, atomic cache writes, streaming PDF download (`_pdf_download.stream_to_file`) are written once. Adding a feature = composing these, not copying their internals.
- **But don't abstract across providers that merely look similar.** The shared *mechanism* (gating via `_throttle.Throttle`, the getter protocol via `cache.cached_lookup`) is single-homed; the per-provider *policy and quirks* stay in each module and must not be collapsed: arxiv `_MAX_CONCURRENT=1`, biorxiv's async `published_doi` + medRxiv fallback, arxiv's three not-found shapes, negative-TTL overrides, openalex's url-only `_throttled_get`. Pass those as constructor args / `fetch` closures — don't fold them into the shared code, and don't collapse the seven clients into one generic class to chase DRY.

## No mode flags that fork behaviour

Prefer a new function or module over a boolean parameter that makes one function do two unrelated things. `force_refresh`, `follow_published`, `normalize`, `require_pdf` are acceptable because each toggles one orthogonal axis with an unchanged default response shape — not because flags are free. A parameter that returns a *different shape* depending on its value is a smell; split it.

## Comments — outer context, not narration

Clear code is the artifact; a comment is not a substitute for a name that says what the thing does. Keep comments **brief** and reserve them for what the code cannot say about itself — the outer context a reader can't recover by reading down.

- **Don't narrate.** If the comment restates the line under it, delete it or rename the thing. `# increment the counter` above `count += 1` is noise; the code is already clear, and clear code is what makes a file readable.
- **Do state the non-obvious**: why this ordering is load-bearing, which upstream quirk forces a branch, what an unusual constant is protecting against, which invariant a block holds. `cached_download`'s "check the file *then* the negative entry" ordering is worth a line; the `os.replace` under it is not.
- **Prefer one line to a paragraph.** A comment that needs several sentences is usually describing a design decision — those belong in the matching `.claude/rules/` file, where they load once for the whole module rather than at one call site.
- Docstrings are the exception to brevity: a `@mcp.tool` docstring *is* the agent-facing API description (see `.claude/rules/server.md`), so it carries parameters and response keys in full.

## Testing — pytest, hypothesis, ZOMBIES

`pytest` is the runner; async tests carry an explicit `@pytest.mark.asyncio` because `asyncio_mode = "strict"` is pinned in `pyproject.toml` rather than inherited. Four autouse fixtures in `tests/conftest.py` isolate every test: pooled-client and throttle state reset, cache root redirected to `tmp_path`, conversion state cleared, and **real network blocked** — a test that needs a response fakes the transport, never the internet. The coverage floor lives in the CI step, deliberately not in `addopts`, so a single-file run doesn't fail it.

**Cover the ZOMBIES** when deciding what to test — the checklist is what turns "I wrote a test" into "I covered the behaviour":

- **Z**ero — empty input, empty result. `top_k<=0` → `[]`, an empty MATCH expression, a paper with no sections.
- **O**ne — the single-element case, where off-by-ones hide.
- **M**any — batches, pagination, fan-out. `get_works_batch` at 51 DOIs crosses a chunk boundary; page 2 of a 25-cap author list is a different path from page 1.
- **B**oundaries — the cap itself, the value one past it, the TTL edge, `MAX_PDF_BYTES` exactly reached.
- **I**nterfaces — the contract at the seam: what a `fetch` closure may return, what shape a tool promises. This is where the shared protocols (`cached_lookup`, `cached_download`) earn their tests once instead of per provider.
- **E**xceptions — the error paths, which in this codebase are most of the interesting behaviour: transient vs. definitive, what gets negative-cached, what stays retryable.
- **S**imple scenarios / simple solutions — the happy path, stated plainly, and no more machinery in the test than the behaviour needs.

**Property-based tests use `hypothesis`** where the invariant is stronger than any example — normalisation round-trips (`_doi.canonical` is idempotent), offset maps (`lower_with_map`'s indices always land inside the original string), BibTeX escaping (output always compiles, for any input text). Reach for it when you catch yourself writing a fifth example of the same rule. Prefer it over hand-rolled fuzzing loops.

## When you change a contract, update its home

These contracts are also documented for humans in `CLAUDE.md` and the sibling rules (`cache.md`, `http.md`, `pdf-download.md`, `utils.md`, `providers.md`, `pipeline.md`, `search.md`, `server.md`). If you change one — a new gating model, a new dispatch path — update the matching doc in the same change, and add the `CHANGELOG.md` `[Unreleased]` bullet if the change is user-facing.

Three conventions keep those docs from rotting:

- **Cite symbols, never line numbers.** `manual.resolve_metadata_source()` stays correct across every edit; `manual.py:74` was already wrong by ten lines.
- **Don't transcribe constants.** Name the constant and explain the policy; let the reader grep the value. Every stale fact these files have accumulated was a number copied out of the code — a duplicated concurrency cap drifted while the prose around it stayed true.
- **Don't transcribe history.** A comment states the invariant the code holds *now*, and where a test pins it — not what the code used to be. `CHANGELOG.md` and `git log -S "<phrase>"` are the homes for "why it changed"; these rules files may carry a one-clause warning where it stops a specific regression, because they are read *before* an edit, when a warning can still act. Past-tense prose outlives the code it describes and then misleads — a comment claiming a refactor "collapsed the six copies of this block" survives the two that get added back, and an error message naming a cause the engine no longer has sends the agent to fix the wrong thing.

  Rewrite `# X used to happen, which broke Y` as `# Invariant: Y. (Guarded by tests/test_z.py::test_y)` — and **verify the citation** by mutating the guarded line and watching that test fail. An unfalsified citation is the same defect in a new costume. If no test pins it, say so: `# Invariant: … (unguarded)` is honest and greppable. Test docstrings are exempt — a regression test's purpose *is* the regression.
