# Known Issues & Friction Report

This document inventories friction points, latent bugs, fragile code paths, and
intentional-constraint trade-offs in the academic-tools-mcp tool set. It is
written for an agent working *inside this repo* who wants to understand and fix
the core toolset.

**Provenance.** The report was compiled two ways and cross-checked: (1) from the
lived operational record of a heavy downstream consumer (a research knowledge
base that drives hundreds of paper ingests through these tools), which surfaced
the *symptoms* and the *workarounds* operators actually adopted; and (2) from a
direct source audit of this repo, which located the *cause* of each symptom and
verified or refuted it against the code. Every code claim below carries a
`file:line` reference checked against the tree as of this writing. A short
**Verified non-issues** section at the end records claims that looked like bugs
but turned out to be correct-by-design, so they don't get re-investigated.

The findings are grouped by severity-of-action, not by subsystem:

1. **Confirmed bugs** — fix candidates with concrete repros and fix sketches.
2. **Fragilities** — latent or low-severity, fix when convenient.
3. **Intentional constraints** — the dominant friction sources; not bugs, but
   the places where an enhancement would remove the most operator pain.
4. **Upstream metadata quality** — provider data issues, not fixable in-tool but
   worth knowing.
5. **Verified non-issues** — do not chase these.

---

## Triage table

| # | Finding | Where | Class | Severity |
|---|---------|-------|-------|----------|
| 1 | ~~`force_refresh` deletes cached PDF *before* re-download; failed refetch loses the only copy~~ **(RESOLVED)** | `arxiv.py:393`, `biorxiv.py:321`, `acl_anthology.py:170` | Bug | Medium |
| 2 | ~~Temp extraction dir leaks on every conversion *failure* path~~ **(RESOLVED)** | `papers.py:553`, early returns vs. `:669` | Bug | Low |
| 3 | ~~`get_with_retry` clamps `Retry-After` to 30s; docstring claims 5-min waits are respected~~ **(RESOLVED)** | `_http.py:156`, `:215` | Bug (doc/code mismatch) | Low |
| 4 | ~~ACL old-style IDs are case-sensitive in the URL; no normalization~~ **(RESOLVED)** | `acl_anthology.py:111`, `:128` | Fragility | Medium |
| 5 | ~~`find_in_paper` truncates silently at `max_results` with no "more exist" flag~~ **(RESOLVED)** | `papers.py:335` | Fragility | Low |
| 6 | ~~`import_markdown` caches sections without a checksum (inconsistent with `convert_pdf`)~~ **(RESOLVED)** | `manual.py:290` | Fragility | Low |
| 7 | Section-lock eviction can spin O(N) over a full held map | `papers.py:210` | Fragility | Very low |
| 8 | ~~`download_pdf` supports only 3 providers — the dominant operator friction~~ **(RESOLVED via opt-in OA-URL path)** | `server.py:920` | Intentional | (enhancement) |
| 9 | Single global conversion lock + coarse 30-min timeout serializes batches | `papers.py:45`, `:37` | Intentional | (enhancement) |
| 10 | No provider fallback when OpenAlex 404s a valid DOI | `manual.py:74` | Intentional | (enhancement) |
| 11 | Literal/ASCII-boundary search misses diacritics & non-Latin scripts | `papers.py:324`, `cache_search.py:137` | Intentional | (limitation) |
| 12 | BM25 rescans the whole corpus on every call (O(N)) | `cache_search.py:258` | Intentional | (scaling limit) |

---

## 1. Confirmed bugs

### 1.1 `force_refresh=True` destroys the cached PDF before the re-download can succeed — **RESOLVED**

**Resolution.** Fixed by dropping the up-front `dest.unlink()` in all three
callers and gating the cached-return short-circuit on `not force_refresh`, so a
`force_refresh` call falls through to the download and lets
`_pdf_download.stream_to_file`'s atomic `os.replace(tmp, dest)` overwrite the
file *only once the new bytes are in hand*. The old file now survives every
failure path (404, transport error, `MAX_PDF_BYTES` abort). No change to the
shared helper was needed — the temp-then-`os.replace` flow already overwrote
atomically. ACL's inner single-flight re-check is also gated on `not
force_refresh` so a cached copy doesn't make the refresh a no-op. Regression
coverage in `tests/test_force_refresh_pdf.py`. Original analysis retained below.

**Where.** All three PDF providers shared the pattern:

```python
# arxiv.py:393, biorxiv.py:321, acl_anthology.py:170
if force_refresh and dest.exists():
    dest.unlink()
```

The unlink happens *up front*, before the network fetch is attempted. The fetch
then goes through `_pdf_download.stream_to_file`, which writes to a sibling
`*.tmp` and atomic-renames into `dest` on success. That atomic-rename protects
against a *crash mid-download*, but it does not protect against the up-front
unlink: if the re-download returns an error (404, transport failure, Cloudflare
challenge, `MAX_PDF_BYTES` abort), the previously-good cached PDF is already
gone, with no recovery path.

**Why it matters.** `force_refresh` is the documented escape hatch for "the
cached file is wrong / stale" (e.g. ACL re-issuing a camera-ready). An operator
reaching for it on a flaky network can end up *worse off* than before: they had
a usable-if-stale PDF, and now they have nothing. This is especially sharp for
providers that intermittently 404 or sit behind Cloudflare, where a refresh is
exactly when a transient failure is most likely.

**Fix sketch.** Don't unlink until the new bytes are in hand. Two options:

- Pass a `force` flag into `stream_to_file` and have it skip the early
  `if dest.exists(): return cached` short-circuit, but keep the
  download-to-temp-then-`os.replace(tmp, dest)` flow. The `os.replace` already
  overwrites atomically, so the old file survives until the new one is fully
  written. The only change needed is to *stop* unlinking `dest` in the three
  callers and instead let the existence-check be bypassed under `force_refresh`.
- Or, minimally: in each caller, rename `dest` to a `.bak` before fetching and
  restore it on the error branch.

The first is cleaner and removes three duplicated unlink sites.

---

### 1.2 Temp extraction directory leaks on conversion failure — **RESOLVED**

**Resolution.** `extract_dir` is now bound (to `None`) before `convert_pdf`'s
`try` and cleaned up in the existing `finally` that already resets
`_current_conversion`, so `shutil.rmtree(extract_dir, ignore_errors=True)` runs on
*every* exit path — success and all four failure paths (spawn error, timeout,
non-zero exit, no-markdown). The success-path-only `rmtree` was removed and the
inline `import shutil` lifted to module scope. The deterministic-name `rm -rf`
self-heal in the bash wrapper stays as harmless belt-and-suspenders. Regression
coverage in `tests/test_papers.py::TestConvertPdfTempDirCleanup`. Original
analysis retained below.

**Where.** `papers.py:553` builds a deterministic extraction dir:

```python
extract_dir = Path(f"/tmp/pdf-convert-{canonical.replace('/', '_')}")
```

On the **success** path this is cleaned up at `papers.py:669`:

```python
import shutil
shutil.rmtree(extract_dir, ignore_errors=True)
```

But every **failure** path returns *before* reaching that line:

- spawn failure (`:574`)
- timeout (`:602`)
- non-zero exit (`:622`)
- no markdown produced (`:637`)

So a conversion that times out or crashes leaves `/tmp/pdf-convert-<canonical>`
behind. It is partially self-healing — the next conversion of the *same* paper
starts with `rm -rf {quoted_extract}` (`:566`) — but a paper that fails once and
is never retried leaks permanently, and a long-lived server accumulates these.

**Why it matters.** Low severity (it's `/tmp`, and same-paper retries clean it),
but on CPU-only MinerU runs the failure paths (timeout especially) are *common*,
and the leaked dirs can be large (extracted images). On a long-running server
this is real disk pressure.

**Fix sketch.** Wrap the whole convert body so cleanup runs on all exits.
Either move the `shutil.rmtree(extract_dir, ignore_errors=True)` into a `finally`
alongside the existing `_current_conversion = None` reset at `:677`, or use
`tempfile.mkdtemp()` + a `try/finally`. Note the deterministic-name behavior is
load-bearing for the `rm -rf` self-heal at `:566`; if you switch to `mkdtemp`,
that self-heal becomes unnecessary (and can be dropped) since each run gets a
fresh dir.

---

### 1.3 `get_with_retry` doc/code mismatch on `Retry-After` clamping — **RESOLVED**

**Resolution.** The arbitrary `backoff_seconds * 30` (~30s) cap was replaced with an
explicit `_MAX_RETRY_AFTER_SECONDS = 600.0` (10-minute) ceiling, and the docstring
rewritten to match: the sleep is now `min(max(Retry-After, backoff_seconds),
_MAX_RETRY_AFTER_SECONDS)`, so a genuine multi-minute server cooldown is honoured while a
misconfigured huge `Retry-After` still can't pin the throttle. Code and docs now agree.
Original analysis retained below.

**Where.** `_http.py:190` set the cap and `:209-211` applied it:

```python
cap = backoff_seconds * 30           # = 30.0 at the default backoff_seconds=1.0
...
retry_after = _retry_after_seconds(response) or 0.0
sleep_for = min(max(retry_after, backoff_seconds), cap)
await asyncio.sleep(sleep_for)
```

The docstring at `:174-178` says:

> The actual sleep is `max(Retry-After, backoff_seconds)` so a server that asks
> us to wait 5 minutes is respected, but a missing or zero header doesn't drop
> us below the provider's own throttle gap.

That is **not** what the code does. With the default `backoff_seconds=1.0`, the
cap is 30s, so a server sending `Retry-After: 300` is clamped to 30s, not
respected. The docstring's "5 minutes is respected" claim is false at every
realistic backoff value.

**Why it matters.** Mostly a correctness-of-documentation bug, but it has a real
edge: a provider under sustained load that asks for a long cooldown gets retried
aggressively (every 30s) instead of being honored, which can prolong a 429
episode. The Brain's operational record shows arXiv 429s that "outlasted
repeated multi-minute backoffs" — consistent with this clamp defeating long
`Retry-After` hints, though arXiv may simply not send them.

**Fix sketch.** Decide which behavior is intended and align the other side. If
the 30s cap is deliberate (defend against a misconfigured `Retry-After: 86400`),
update the docstring to say so and explain the trade-off. If honoring genuine
multi-minute cooldowns matters more, raise the cap (e.g. `backoff_seconds * 300`
or an absolute `min(retry_after, 600)`) and keep a sanity ceiling. The
`max_attempts=2` default means at most one sleep, so a higher cap is bounded.

---

## 2. Fragilities

### 2.1 ACL Anthology old-style IDs are case-sensitive and not normalized — **RESOLVED**

**Resolution.** `doi_to_anthology_id` now routes the extracted suffix through a
new `_normalize_anthology_id` helper that uppercases old-format IDs (matched by
`_OLD_FORMAT_ID_RE = ^[A-Za-z]\d{2}-\d+$`, e.g. `p16-1160` → `P16-1160`) and
leaves new-format IDs (`2023.acl-long.1`) untouched. Old-format IDs are letter +
digits only, so `.upper()` is safe. Because `pdf_url`, `pdf_path`, and
`_pdf_filename` all derive from `doi_to_anthology_id`'s output, the single
chokepoint fix corrects both the CDN URL and the cache filename; the lowercased
cache key via `_canonical_key` is unchanged. A Crossref-lowercased DOI now fetches
instead of 404ing. Regression coverage in `tests/test_acl_anthology.py`
(`TestNormalizeAnthologyId` plus old-format cases in `TestDoiToAnthologyId` and a
`TestPdfUrl` round-trip). Original analysis retained below.

**Where.** `acl_anthology.py:111-120` extracts the ID verbatim from the DOI, and
`:128-130` builds the PDF URL by direct interpolation:

```python
def doi_to_anthology_id(doi: str) -> str | None:
    bare = _normalize_doi(doi)
    if not bare.startswith(_ACL_DOI_PREFIX):
        return None
    return bare[len(_ACL_DOI_PREFIX):]      # case preserved as-is

def pdf_url(anthology_id: str) -> str:
    return f"https://aclanthology.org/{anthology_id}.pdf"
```

`aclanthology.org` serves old-format papers under a case-sensitive path:
`P16-1160.pdf` resolves, `p16-1160.pdf` 404s. Crossref returns these DOIs
lowercased (`10.18653/v1/p16-1160`), so a caller who got the DOI from Crossref
and hands it straight to `download_pdf` gets a 404. The operator workaround has
been to manually uppercase the venue prefix before calling — a paper cut that
recurs on any batch with pre-ACL-2020 papers.

Note `_canonical_key` (`:123-125`) deliberately lowercases for the *cache key*,
which is fine — but the *URL* is built from the un-lowercased `aid`, so the cache
key and the fetch URL diverge in case, and only the URL's case matters to the
CDN.

**Fix sketch.** Old-format ACL IDs have the shape `<LETTER><digit>-<digits>`
(e.g. `P16-1160`, `W04-1013`). New-format IDs are `YYYY.venue-track.n` and are
lowercase. Normalize in `doi_to_anthology_id`: if the suffix matches the
old-format regex, uppercase the leading venue letter(s). Keep new-format
untouched. Add a test with `10.18653/v1/p16-1160` → URL `.../P16-1160.pdf`.

### 2.2 `find_in_paper` truncates silently at `max_results` — **RESOLVED**

**Resolution.** `find_in_markdown` now returns `(hits, truncated)`, setting
`truncated=True` at the `max_results` early-return (a genuine extra match exists
at that point) and `False` otherwise — no full-document rescan. The `find_in_paper`
tool surfaces the flag as a `truncated` field in its response and documents it.
An agent gathering every mention of X now knows when the result set was capped.
Regression coverage in `tests/test_audit_features.py`. Original analysis retained
below.

**Where.** `papers.py:335` (inside `find_in_markdown`):

```python
if len(hits) >= max_results:
    return hits
```

When a query has more matches than `max_results` (default 20, caller-overridable
via the `_FIND_MAX_RESULTS` annotation at `server.py:1562`), the function returns
the first N with no signal that more exist. The tool response
(`{query, paper_identifier, result_count, results}`) carries `result_count` =
the number *returned*, which an agent can easily mistake for the total.

**Why it matters.** An agent doing exhaustive evidence-gathering ("find every
mention of dataset X") can silently miss matches and conclude it has them all.

**Fix sketch.** Add a `truncated: bool` to the returned dict (true when the
match loop hit the cap before exhausting the section list), or return a
`total_matches` count by finishing the scan but only materializing snippets for
the first N. The first is cheaper and sufficient as a correctness signal.

### 2.3 `import_markdown` caches sections without a checksum — **RESOLVED**

**Resolution.** `import_markdown` now stores
`{"sections": ..., "markdown_checksum": papers._markdown_checksum(md_path)}`,
mirroring `convert_pdf`. A later `convert_paper` / section read on an imported
paper now passes the `stored_checksum is not None` guard and trusts the cache
instead of re-parsing the markdown every call. Regression coverage in
`tests/test_manual.py::TestImportMarkdown`. Original analysis retained below.

**Where.** `manual.py:290`:

```python
sections_data = {"sections": sections}      # no "markdown_checksum"
cache.put(namespace, "sections", papers._sections_key(canonical), sections_data)
```

`convert_pdf` stores `{"sections": ..., "markdown_checksum": ...}` (`papers.py:662`)
and uses the checksum to decide whether a cached section index is still valid.
An imported markdown skips that, so the cached sections entry has no checksum.

**Why it matters.** Mostly harmless, but it makes the two intake paths
inconsistent, and it means a later `convert_paper` call on an imported paper
always falls through the `stored_checksum is not None` guard (`papers.py:500`)
and re-parses the markdown on every call instead of trusting the cache. Wasted
work, not wrong answers.

**Fix sketch.** Compute and store the checksum in `import_markdown` the same way
`convert_pdf` does: `"markdown_checksum": papers._markdown_checksum(md_path)`
after the `md_path.write_text(markdown)` at `:286`.

### 2.4 Section-lock eviction can scan a fully-held map

**Where.** `papers.py:200-213`. When `_section_locks` exceeds `_SECTION_LOCKS_MAX`
(1024) and the front lock is held, the code re-checks `all(l.locked() ...)` over
the whole map each iteration to decide whether to bail. In the pathological case
of many held locks this is O(N) per eviction attempt.

**Why it matters.** Very low — the comment at `:208` already notes this is
"extremely unlikely" (it requires 1000+ concurrently-held per-paper locks). Flagged
only for completeness. Leave it unless profiling ever implicates it.

---

## 3. Intentional constraints (the real friction sources)

These are not bugs. They are deliberate design choices, and each is defensible.
They are listed here because they are where operators spend the most effort
working *around* the tool, so they are the highest-value targets if the goal is
to reduce friction rather than fix correctness.

### 3.1 `download_pdf` supports only arXiv, bioRxiv/medRxiv, and ACL — **RESOLVED (opt-in)**

**Resolution.** Implemented the "middle path" from the enhancement sketch below.
`download_pdf(identifier, allow_oa_url=True)` now fetches a generic publisher DOI from
the open-access PDF URL OpenAlex already surfaces (`best_oa_location.pdf_url` →
`primary_location.pdf_url` → `open_access.oa_url`, via the new `openalex.best_pdf_url`).
Only the OpenAlex-surfaced URL is fetched — never a caller-supplied one — so the server
stays metadata-gated rather than a general scraper. The fetch goes through a new
provider-shaped `oa_download.py` module (own pooled client + `_request_slot`, conservative
`_MAX_CONCURRENT=2`) and validates the response is a real PDF via an opt-in
`stream_to_file(require_pdf=True)` guard (`%PDF-` magic bytes + advisory Content-Type),
rejecting HTML landing/paywall pages. The PDF lands in the `manual` namespace so
`convert_paper` and the `force_refresh` cascade treat it like any imported paper.
`allow_oa_url` defaults to `False`, so the hard refusal (now also hinting at the opt-in)
is unchanged by default. `get_paper_metadata` additionally surfaces a `pdf_url` field.
Coverage in `tests/test_oa_download.py`. Original analysis retained below.

**Where.** `server.py:920-934` returned a hard refusal for anything else:

```python
"error": (
    f"Cannot auto-download PDF for identifier: {identifier!r}. "
    "Direct download is only supported for arXiv IDs, "
    "bioRxiv/medRxiv DOIs (10.1101/...), and ACL Anthology DOIs "
    "(10.18653/v1/...)."
),
```

This is the single most-felt friction point. Generic publisher DOIs (MIT Press
`10.1162/...` for Computational Linguistics/TACL, Nature, IEEE, Frontiers, etc.)
are refused at the tool level, and the operator must fetch the PDF in a browser
and round-trip it through `import_paper`. For a consumer ingesting mostly
*published* (not preprint) literature, this is the common case, not the
exception.

**The deliberate rationale** (per `CLAUDE.md`) is sound: the server must not
fetch arbitrary URLs, both to avoid hammering publishers and to avoid becoming a
general-purpose scraper. But there is a middle path worth considering.

**Enhancement sketch.** `get_paper_metadata` already surfaces an open-access URL
(`oa_url`) for many DOIs via OpenAlex. A gated `download_pdf(..., allow_oa_url=True)`
that fetches *only* a publisher/repository OA URL returned by the metadata layer
(not an arbitrary caller-supplied URL) would cover a large fraction of the
"published, but legally free" cases without becoming a scraper. Gold/hybrid-OA
PDFs on publisher domains (e.g. `nature.com/articles/<doi>.pdf`) often fetch
cleanly. Keep it opt-in and keep the refusal as the default.

### 3.2 Single global conversion lock + coarse 30-minute timeout

**Where.** `papers.py:45` (`_global_convert_lock`), `:541` (check-then-acquire),
`:37` (`_DEFAULT_PDF_CONVERT_TIMEOUT = 1800.0`).

At most one PDF→markdown conversion runs server-wide; a second caller gets a
structured `busy` error and is expected to retry (`:65-99`). This is the right
call for a CPU/GPU-bound converter — parallel conversions just thrash. But it
means a batch ingest *serializes*, and a single large PDF that legitimately
exceeds 30 minutes (the operator record has multiple: a ~big NMT paper hung ~40
min) fails the whole conversion as non-retryable.

The operator workaround that emerged is a **pdftotext bypass**: locate the
cached PDF, run `pdftotext -layout` to get plain text, and `import_paper` it as
pre-converted `.md`, skipping `convert_paper` entirely. This is reliable and
fast but loses table/equation structure that MinerU/Marker would have preserved.

**Enhancement sketch.** Consider exposing a first-class lightweight extraction
fallback — e.g. a `convert_paper(..., mode="fast")` that shells out to
`pdftotext -layout` (or `pymupdf`) and runs *outside* the global lock (it is
cheap and not GPU-bound). It would not match MinerU quality, but it would turn
the current manual bypass into a supported degraded path for the timeout case,
and it would let batch ingests get *something* for every paper without
serializing on the heavy converter. The `timed_out: true` error at `:609` is a
natural trigger point to suggest it.

### 3.3 No provider fallback on OpenAlex 404

**Where.** `manual.py:74-98` (`_resolve_metadata_source`). Identifier *shape*
alone picks the provider: arXiv shape → arXiv, bioRxiv DOI → bioRxiv, any other
DOI → OpenAlex. There is no fallback: if OpenAlex hasn't indexed a valid DOI
(new papers, niche venues, non-English pre-2010), the lookup fails hard, even
though Crossref might have the record.

**Why it's intentional.** Spraying every provider per lookup would blow rate
budgets and muddy the error contract. Fair.

**Enhancement sketch.** A narrow, opt-in fallback — `get_paper_metadata(...,
fallback_crossref=True)` that tries Crossref *only* when OpenAlex returns a
404 (not on transient errors) — would catch the "valid DOI, not yet in OpenAlex"
case without a general spray. Crossref coverage of recent DOIs is often ahead of
OpenAlex's indexing lag.

### 3.4 Search is literal + ASCII word-boundaries (diacritics/non-Latin)

**Where.** `papers.py:324` (`re.escape(query)` — literal substring, no real
regex) and `:326` (`\b…\b`); `cache_search.py:137` (same `\b` boundaries).

`\b` is ASCII-oriented, and there is no Unicode normalization. Searching `cafe`
won't match `café`; searching `Gutierrez` won't match `Gutiérrez`; whole-word
matching is unreliable on non-Latin scripts. For a consumer working on
multilingual/typological NLP (where author names and terms are diacritic-heavy),
this bites.

**Why it's a reasonable default.** Literal substring is predictable and the
common case is ASCII. But the limitation is invisible — a 0-hit result reads as
"not in the paper" when it may be "spelled with a diacritic."

**Enhancement sketch.** Offer an optional `normalize=True` that NFKD-folds and
strips combining marks on both query and text before matching. Document the
ASCII-boundary caveat in the tool description so 0-hit results aren't
over-trusted.

### 3.5 BM25 corpus rescanned on every `search_cached_papers` call

**Where.** `cache_search.py:258-377`. Every call walks all cached markdown,
tokenizes, and scores in pure Python. The docstring (`:15-19`) is candid that
this is fine "for tens to hundreds of papers" and flags embedding-rerank as the
natural follow-up. `_MAX_TOP_K = 50` caps output.

**Why it's fine today.** At personal-MCP scale it's <100ms and avoids any
index-staleness bugs. Flagged only so the next person knows the cliff: at ~10k
cached papers a fresh full-corpus scan + tokenize per call will approach tool
timeouts. The fix (persistent inverted index or embeddings) is real work and not
yet warranted.

---

## 4. Upstream metadata quality (not fixable in-tool, but known)

These surface through `get_paper_metadata` / `get_paper_authors` and are
properties of the upstream providers, not bugs here. Operators correct them by
hand under a "published version is authoritative" rule. Listed so they aren't
mistaken for tool defects:

- **Author diacritics dropped/mangled** by OpenAlex (`Alan Aspuru-Guzik` for
  `Alán Aspuru-Guzik`, etc.).
- **Current vs. paper-time institution.** OpenAlex reports an author's *present*
  affiliation, not their affiliation at publication time.
- **Preprint vs. published author-count divergence.** arXiv and the published
  DOI can list different author sets/counts for the same work; `follow_published`
  helps but only chains one direction and only when OpenAlex has indexed the
  journal version (and it is not available in the batch `get_papers_metadata`).

A small in-tool improvement that *would* help: when `follow_published` falls back
to preprint metadata because OpenAlex hasn't indexed the journal version, the
response currently does so silently. Adding a `followed_published: false` (or a
`note`) field would let a careful consumer know it's looking at preprint-era
metadata.

---

## 5. Verified non-issues (do not chase)

These were flagged during the audit as suspected problems and then **refuted**
against the source. Recorded so they aren't re-investigated:

- **`get_paper_citations_count` does enrich its error with a suggestion.**
  `server.py:1492` calls `_enrich_error(data, "Check the DOI format...")`. An
  earlier audit claimed it was missing; it is present.
- **The `/tmp` extraction dir *is* cleaned on the success path.**
  `papers.py:669` runs `shutil.rmtree(...)`. The leak is real but is confined to
  the *failure* paths only (see finding 1.2) — not "never cleaned."
- **Same-process, same-paper conversion races cannot happen.** The global
  conversion lock (`papers.py:45`) serializes all conversions server-wide, so the
  deterministic `/tmp/pdf-convert-<canonical>` dir cannot be raced by two
  conversions within one process. (Two *separate* server processes pointed at the
  same `/tmp` could still collide — but that is not the typical deployment.)
- **Section pagination is properly bounded.** `get_paper_section`'s `max_chars`
  has a hard cap enforced both by a Pydantic `le=_SECTION_HARNESS_CAP`
  (`server.py:123`) and the `anthropic/maxResultSizeChars` meta on the tool
  (`:1116`). This is handled, not a gap.

---

## Suggested fix order

If picking these up, a sensible sequence:

1. ~~**Finding 1.1** (force_refresh data loss) — highest-value correctness fix,
   localized to three call sites plus the shared stream helper.~~ **DONE.**
2. ~~**Finding 1.2** (temp-dir leak) — one `finally` block in `papers.py`.~~ **DONE.**
3. ~~**Finding 2.1** (ACL case normalization) — removes a recurring operator paper
   cut; small, testable.~~ **DONE.**
4. ~~**Findings 2.2 / 2.3** — cheap correctness/consistency cleanups.~~ **DONE.**
   ~~**Finding 1.3** (`Retry-After` clamp doc/code mismatch).~~ **DONE.**
5. ~~**Finding 3.1** (OA-URL download path) — the enhancement that most reduces
   day-to-day friction.~~ **DONE (opt-in `allow_oa_url=True`).** **Finding 3.2**
   (fast-extract fallback) — still open if there's appetite for more surface area.
