---
paths:
  - "src/academic_tools_mcp/**/*.py"
  - ".claude/rules/*.md"
  - "CLAUDE.md"
---

# Python design contracts

Style (formatting, import order, line length, typing hygiene) is enforced by tooling — `ruff format`, `ruff check`, `mypy`, configured in `pyproject.toml`, and auto-applied on edit by `.claude/hooks/ruff-format.sh`. **Do not restate style rules here or hand-format code; let the tools do it.**

These are the contracts a linter can't check. Each is falsifiable — open the cited exemplar and match it.

## Layering — tools never reach past their layer

- **`httpx` lives in the shared layer and the provider clients, nowhere else.** Only `_clients.py` / `_http.py` / `_throttle.py` / `_pdf_download.py` and `providers/*.py` may import it (two providers don't need to); a tool, pipeline or content module that does is a layering violation. `oa_download.py` is the proof it isn't needed: a full download path through `_clients.get_client()` → `Throttle.get` → `_http.get_with_retry`, with no `import httpx`.
- **Server tools return slices, not whole objects** — an LLM agent never receives a raw provider response.
- **Shared infrastructure is single-homed.** Caching (`cache.py`), the cached-getter protocol (`cache.cached_lookup`), the cached-download protocol (`_pdf_download.cached_download`), throttling (`_throttle.Throttle`), single-flight (`_singleflight.py`), retry (`_http.get_with_retry`), counters (`_stats.py`), config (`config.py`), DOI normalization (`_doi.normalize` / `canonical` / `looks_like_doi`), and the outbound User-Agent (`_useragent.headers`) each have exactly one home. Need that behaviour in a new module? Route through the existing one — construct a `Throttle`, call `cached_lookup` — don't fork a local copy of the gating, the force_refresh→check→single-flight→re-check dance, or a DOI regex.

## Single responsibility — one job per unit

- **One paper tool per job, not per provider.** The four unified tools (`get_paper_metadata` / `_authors` / `_abstract` / `_bibtex`) dispatch internally via `manual.resolve_metadata_source()`. Don't branch on provider *inside* a tool, and don't add a fifth `get_<provider>_metadata` — extend the dispatcher.
- **A new API provider mirrors an existing one.** `providers/biorxiv.py` is the fullest instance of the shape — `_throttled_get` and `_request_slot` wrappers over one module-level `Throttle`, a `canonical_key` → `_fetch()` closure → `cache.cached_lookup` getter, and a `download_pdf` through `_pdf_download.cached_download`. `.claude/rules/providers.md` § Common shape is the checklist; a provider that invents its own concurrency or caching scheme is a bug, not a feature. Use the `add-provider` skill.
- **Keep a trust boundary in its own small module.** `oa_download.py` exists so the "fetch only what OpenAlex surfaced" rule has one enforcement site — the alternative is an `allow_arbitrary` flag threaded through the whole download stack. Widen a boundary by adding a module, never by adding a parameter.

## DRY without over-abstraction

**Don't abstract across providers that merely look similar.** The shared *mechanism* (gating via `_throttle.Throttle`, the getter protocol via `cache.cached_lookup`) is single-homed; the per-provider *policy and quirks* stay in each module and must not be collapsed: arxiv's single-connection `_MAX_CONCURRENT`, biorxiv's late-appearing `published_doi` + medRxiv fallback. Pass those as constructor args / `fetch` closures — don't fold them into the shared code, and don't collapse the seven clients into one generic class to chase DRY.

## No mode flags that fork behaviour

Prefer a new function or module over a boolean parameter that makes one function do two unrelated things. `force_refresh` and `normalize` are acceptable: each toggles one orthogonal axis and the response shape is unchanged either way. `follow_published` is the standing exception, not a precedent — when the chain succeeds it swaps the whole key set (`_format_openalex_via_biorxiv` returns the OpenAlex fields plus `preprint_doi`, not the bioRxiv ones), which is why the tool docstring has to enumerate both shapes and why `get_papers_metadata` refuses to support it. Don't add a second one: a parameter that returns a *different shape* depending on its value belongs in its own tool.

## Comments — outer context, not narration

Keep comments **brief** and reserve them for what the code cannot say about itself.

- **Do state the non-obvious**: why this ordering is load-bearing, which upstream quirk forces a branch, what an unusual constant is protecting against, which invariant a block holds. In `_pdf_download`, "artifact before negative entry" earns its paragraph in `cached_download`'s docstring; `stream_to_file`'s `os.replace` line needs nothing.
- **Prefer one line to a paragraph.** A comment that needs several sentences is usually describing a design decision — those belong in the matching `.claude/rules/` file, where they load once for the whole module rather than at one call site.
- Docstrings are the exception to brevity: a `@mcp.tool` docstring *is* the agent-facing API description (see `.claude/rules/server.md`), so it carries parameters and response keys in full.

## Testing — pytest, hypothesis, ZOMBIES

`pytest` is the runner; async tests carry an explicit `@pytest.mark.asyncio` because `asyncio_mode = "strict"` is pinned in `pyproject.toml` rather than inherited. Four autouse fixtures in `tests/conftest.py` isolate every test: pooled-client and throttle state reset, cache root redirected to `tmp_path`, conversion state cleared, and **real network blocked** — a test that needs a response fakes the transport, never the internet. The coverage floor lives in the CI step, deliberately not in `addopts`, so a single-file run doesn't fail it.

**Cover the ZOMBIES** when deciding what to test:

- **Z/O/M/B** — empty (`top_k<=0` → `[]`, a paper with no sections), single-element, many (`get_works_batch` past `_BATCH_CHUNK_SIZE` *cache misses* — it chunks the misses, not the argument, so a warm cache exercises nothing; page 2 of an `AUTHORS_PAGE_SIZE`-capped author list is a different path from page 1), and the boundary itself: exactly at a cap must *pass*, one past it must fail. `stream_to_file` aborts on `written + len(chunk) > max_bytes`, so a PDF of exactly `MAX_PDF_BYTES` succeeds — test both sides.
- **I/E/S** — the seam contract (what a `fetch` closure may return; `cached_lookup` / `cached_download` earn their tests once, not per provider), the error paths (transient vs. definitive, what gets negative-cached, what stays retryable — most of the interesting behaviour here), and the happy path.

**Property-based tests use `hypothesis`** where the invariant is stronger than any example: `tests/test_doi_properties.py` pins that every spelling of one DOI collapses to one cache key and that `_doi.canonical` is idempotent. Reach for it when you catch yourself writing a fifth example of the same rule — offset maps (`lower_with_map`) and BibTeX escaping are the standing candidates, currently covered by examples only. Prefer it over hand-rolled fuzzing loops.

## Writing the docs: when you change a contract, update its home

These contracts are also documented for humans in `CLAUDE.md` and the sibling `.claude/rules/` file for the module you touched. If you change one — a new gating model, a new dispatch path — update the matching doc in the same change, and add the `CHANGELOG.md` `[Unreleased]` bullet if the change is user-facing.

Four conventions keep those docs from rotting:

- **Cite symbols, never line numbers.** `manual.resolve_metadata_source()` stays correct across every edit; `manual.py:74` is wrong the next time anyone adds an import.
- **Don't transcribe constants.** Name the constant and explain the policy — `_BATCH_CHUNK_SIZE`, not `50` — and let the reader grep the value. A copied number goes stale silently while the prose around it stays true.
- **Don't transcribe history.** State the invariant the code holds *now* and what breaks if you violate it — not what the code used to be. `CHANGELOG.md` and `git log -S "<phrase>"` are the homes for "why it changed"; these rules files may carry a one-clause warning where it stops a specific regression, because they are read *before* an edit, when a warning can still act. Past-tense prose outlives the code it describes and then misleads. Rewrite `# X used to happen, which broke Y` as `# Invariant: Y`. Test docstrings are exempt — a regression test's purpose *is* the regression.
- **Don't cite tests in these rules files.** Name the invariant precisely enough to grep for; a `::TestClass::test_method` node ID is a symbol that churns faster than the code it guards, and a citation nobody re-verifies is worse than none — this layer has already shipped one pointing at a class that does not exist. Where a *code* comment or docstring cites a test, mutate the guarded line and watch it fail before you commit.
