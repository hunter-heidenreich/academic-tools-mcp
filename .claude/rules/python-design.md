---
paths:
  - "src/academic_tools_mcp/**/*.py"
---

# Python design contracts

Style (formatting, import order, line length, typing hygiene) is enforced by
tooling — `ruff format`, `ruff check`, `mypy`, configured in `pyproject.toml`,
and auto-applied on edit by `.claude/hooks/ruff-format.sh`. **Do not restate
style rules here or hand-format code; let the tools do it.**

This file carries the things a linter *can't* check: the layering and
single-responsibility contracts that keep this codebase coherent. Each is
falsifiable against real code — when in doubt, open the cited exemplar and match
it.

## Layering — tools never reach past their layer

- **No raw `httpx` outside an API-client module.** Every outbound request goes
  through the shared layer (`_http.py` → `_clients.py`). A tool or pipeline
  module that imports `httpx` directly is a layering violation. The retry,
  backpressure, `Retry-After`, and stats behaviour lives in `_http.py` — reuse
  it, never re-implement it per call site.
- **Server tools return slices, not whole objects.** A tool fetches the full
  cached provider object, then returns only the relevant fields (see the unified
  paper tools in `tools/paper.py`). An LLM agent should never receive a raw
  OpenAlex response. New tool → extract a lean slice.
- **Shared infrastructure is single-homed.** Caching (`cache.py`), the
  cached-getter protocol (`cache.cached_lookup`), throttling (`_throttle.Throttle`),
  single-flight (`_singleflight.py`), retry (`_http.get_with_retry`), counters
  (`_stats.py`), and config (`config.py`) each have exactly one home. Need that
  behaviour in a new provider? Route through the existing module — construct a
  `Throttle`, call `cached_lookup` — don't fork a local copy of the gating or the
  force_refresh→check→single-flight→re-check dance.

## Single responsibility — one job per unit

- **One paper tool per job, not per provider.** The four unified tools
  (`get_paper_metadata` / `_authors` / `_abstract` / `_bibtex`) take any
  identifier and dispatch internally via `manual.resolve_metadata_source()`.
  Don't branch on provider *inside* a tool, and don't add a fifth
  `get_<provider>_metadata` variant — extend the dispatcher instead. Responses tag `_source` / `_canonical_id` so
  callers branch on provider-specific fields downstream.
- **A new API provider mirrors an existing one.** `providers/arxiv.py` and
  `providers/crossref.py` are the canonical shapes: pooled `httpx.AsyncClient`, a
  `_throttle.Throttle` instance (gating via `Throttle.slot`/`.get`, see
  `providers/arxiv.py`) exposed through thin `_throttled_get`/`_request_slot`
  wrappers, each getter driven by `cache.cached_lookup` (single-flight by canonical
  id, force_refresh, 404 → negative cache inside the `fetch` closure), positive-cache
  TTL eviction, `_stats` counters. Same shape every time — a provider that invents
  its own concurrency or caching scheme is a bug, not a feature.
- **Narrow, named exceptions over broad behaviour.** The OA-download path only
  fetches the OA URL OpenAlex already surfaces (`openalex.best_pdf_url`,
  `providers/openalex.py`) — never a caller-supplied URL. Keep such trust boundaries in
  one small module (`oa_download.py`) rather than threading an `allow_arbitrary`
  flag through the download stack.

## DRY without over-abstraction

- **Reuse the primitive; don't re-derive it.** Throttling, retry, atomic cache
  writes, streaming PDF download (`_pdf_download.stream_to_file`) are written
  once. Adding a feature = composing these, not copying their internals.
- **But don't abstract across providers that merely look similar.** The shared
  *mechanism* (gating via `_throttle.Throttle`, the getter protocol via
  `cache.cached_lookup`) is single-homed; the per-provider *policy and quirks*
  stay in each module and must not be collapsed: arxiv `_MAX_CONCURRENT=1`, biorxiv's
  async `published_doi` + medRxiv fallback, arxiv's three not-found shapes,
  negative-TTL overrides, openalex's url-only `_throttled_get`. Pass those as
  constructor args / `fetch` closures — don't fold them into the shared code, and
  don't collapse the seven clients into one generic class to chase DRY.

## No mode flags that fork behaviour

Prefer a new function or module over a boolean parameter that makes one function
do two unrelated things. `force_refresh`, `follow_published`, `normalize`,
`require_pdf` are acceptable because each toggles one orthogonal axis with an
unchanged default response shape — not because flags are free. A parameter that
returns a *different shape* depending on its value is a smell; split it.

## When you change a contract, update its home

These contracts are also documented for humans in `CLAUDE.md` and the sibling
rules (`infrastructure.md`, `providers.md`, `pipeline.md`, `server.md`). If you
change one — a new gating model, a new dispatch path — update the matching doc in
the same change, and add the `CHANGELOG.md` `[Unreleased]` bullet if the change
is user-facing.

Two conventions keep those docs from rotting:

- **Cite symbols, never line numbers.** `manual.resolve_metadata_source()` stays
  correct across every edit; `manual.py:74` was already wrong by ten lines.
- **Don't transcribe constants.** Name the constant and explain the policy;
  let the reader grep the value. Every stale fact these files have accumulated
  was a number copied out of the code — a duplicated concurrency cap drifted
  while the prose around it stayed true.
