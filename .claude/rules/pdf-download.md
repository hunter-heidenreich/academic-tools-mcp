---
paths:
  - "src/academic_tools_mcp/_pdf_download.py"
  - "src/academic_tools_mcp/oa_download.py"
---

# PDF download and the cached-download protocol

## _pdf_download.py

Shared streaming-download helper behind all four `download_pdf` implementations (`providers/arxiv`, `providers/biorxiv`, `providers/acl_anthology`, `oa_download`). Slot acquisition is per-provider policy; streaming, size-capping, PDF sniffing and atomic rename are identical and live here.

- **Gate every cached-PDF check on `is_usable_pdf` / `cached_hit`, never `Path.exists()`** — a 0-byte file or an HTML landing page saved under `.pdf` must count as a miss. New call sites included: `manual.is_usable_pdf` is an alias, and `tools/pipeline.convert_paper` gates on it so `papers.convert_pdf`'s bare `.exists()` is only ever reached behind that gate. Don't add a second ungated path.
- **Every new terminal branch in `stream_to_file` must carry an explicit `retryable` verdict**, or `is_definitive_failure` silently treats it as transient. Today: 404 and `require_pdf` rejections → `retryable: False`; a cap abort adds `max_bytes`; a 0-byte 200 → `retryable: True`, deliberately kept out of the negative cache as a blip rather than a fact about the paper (the `%PDF-` sniff can't catch it — with no chunks the loop body never runs).
- **`require_pdf` is the OA path's trust boundary.** It sniffs the magic bytes on the first chunk, before a single byte reaches the temp file, so a publisher's HTML interstitial is rejected rather than cached as a paper.
- **`MAX_PDF_BYTES`** is resolved by `resolve_max_pdf_bytes()` (its docstring owns the disable vocabulary). A download that would exceed the cap is aborted *during* the stream, so a misrouted URL can't fill the disk before any size check.

### `cached_download` — the shared cached-download protocol

The file-on-disk sibling of `cache.cached_lookup` (`.claude/rules/cache.md`): same force_refresh → check → single-flight → in-slot re-check → `fetch` skeleton. Where the two differ, the difference is deliberate — don't "align" them.

- **The in-slot re-check is skipped under `force_refresh`.** `cached_lookup` re-checks unconditionally so concurrent forced callers share one fetch; here that would make a refresh a no-op whenever a cached PDF exists, so concurrent forced callers each re-stream.
- **The protocol writes the negative entry, not `fetch`.** Opposite of `cached_lookup`, where the closure owns its own caching. A `fetch` here returns a plain result dict and never touches `cache`; `is_definitive_failure` decides.
- **`force_refresh` never unlinks the PDF** — it drops only the negative entry, and `stream_to_file` replaces `dest` only on success, so a failed refresh leaves the caller the copy they had.
- **Check the artifact before the negative entry.** A `force_refresh` that 404s leaves a good PDF on disk beside a fresh negative entry; the file-first order is what keeps the next plain call serving the PDF rather than the stale error.
- **There is no positive TTL, because the file is the entry** and `is_usable_pdf` is its freshness rule. Only the negative half is JSON — hence `neg_ttl` is required, not defaulted: each provider states its own policy the way `positive_ttl` makes it state one for metadata.

arxiv/biorxiv pass a **tuple** `sf_key` (`("pdf", canonical)`) because their `fetch` awaits `get_paper`, which shares the module's one `SingleFlight` and would otherwise await this very slot's future and deadlock. `acl_anthology` omits it — PDF-only, so there is no re-entrant getter to collide with.

**`is_definitive_failure` is an allowlist**, and all three conjuncts are load-bearing. `_http.error_dict` sets `retryable` on `LocalBackpressureError` alone, so timeouts, transport errors, 5xx, 429 **and every non-404 4xx** arrive with no such key — a denylist ("anything not marked retryable") would negative-cache all of them for the full TTL. The corollary the OA path lives with: a 403 paywall or a 410 on an OA URL is re-resolved and re-fetched on every call; only `stream_to_file`'s explicit 404 branch and its `require_pdf` rejections are cacheable. The `max_bytes` sentinel exempts a `MAX_PDF_BYTES` abort: non-retryable, but a config choice a cap bump fixes rather than a fact about the paper.

**`extra_fields`** merges constant provenance into every *successful* payload, cached and fresh alike, so a decorating provider (ACL's `anthology_id` / `pdf_url`) cannot have its two branches disagree. Errors are returned undecorated. Each caller gets a deep copy — followers share the leader's object and `tools/pipeline` writes `cascaded_invalidated` into what it receives.

## oa_download.py

**Trust boundary: the URL comes from `openalex.best_pdf_url` or nowhere** (`best_oa_location.pdf_url` → `primary_location.pdf_url` → `open_access.oa_url`). Do not add a parameter that accepts a URL, do not widen resolution to a search or a redirect chase, and keep `require_pdf=True` on the `stream_to_file` call — this is the only caller that passes it, because it is the only path whose URL can be a publisher landing page.

**`_IMPORT_SUGGESTION` attaches to definitive failures only.** A retryable OpenAlex lookup error passes through untouched — telling an agent to go fetch the PDF by hand when it should just retry is the failure this guards against.

The negative half lives in this module's own namespace while the artifact lands in `manual` (`manual.resolve_target`), because "no OA copy exists" is a verdict specific to this path while the PDF is shared with manual import and the rest of the pipeline.

Throttling is `per_host=True` with a global `max_concurrent`; the mechanism and the reason `max_concurrent` must not go per-host are in `.claude/rules/http.md` § `per_host`, and the constants carry their own justification in this module.
