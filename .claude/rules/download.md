---
paths:
  - "src/academic_tools_mcp/download/*.py"
---

# PDF download and the cached-download protocol

**Per-function behaviour lives in the docstrings.** This file covers only what no
single docstring can: the cross-module contracts, the trust boundary, and the
edits that look safe and aren't.

## download/streaming.py

`stream_to_file`'s `timeout` has no default deliberately: every provider names
its own `_PDF_TIMEOUT_SECONDS`, so a new one has to state a policy rather than
inherit a silent 60s.

- **Gate every cached-PDF check on `is_usable_pdf` / `cached_hit`, never
  `Path.exists()`** — a 0-byte file or an HTML landing page saved under `.pdf`
  must count as a miss. New call sites included: `manual.import_local_pdf` and
  `tools/pipeline.convert_paper` both gate on them, so `papers.convert_pdf`'s
  bare `.exists()` is only ever reached behind that gate. Don't add a second
  ungated path, and don't hand-roll the check-then-`stat` pair — **`cached_hit`
  owns the `stat`**, so a file unlinked between the usability check and the size
  read is a miss the caller re-downloads rather than an `OSError` out of an MCP tool.
- **Every new terminal branch in `stream_to_file` must carry an explicit
  `retryable` verdict**, or `is_definitive_failure` silently treats it as
  transient.
- **A local write failure is an `{error, retryable: True}` counted as
  `cache_write_failures`, never a raised `OSError`.** `stream_to_file` takes
  `namespace` alongside `provider_label` for exactly that reason — the same split
  as `Throttle`: the label reaches the agent, the namespace files the failure in
  the row already holding this provider's cache counters. `retryable` keeps a
  full disk out of the negative cache, where it would strand the paper.
  **`stream_to_file`'s `except OSError` is that guarantee — a new disk operation
  added inside it inherits it, one added outside does not.**
- **`MAX_PDF_BYTES` passes `on_nonpositive="default"`; the two `PDF_*_TIMEOUT`s
  pass `"disable"`. Don't copy one policy onto the other.** A *negative* cap
  falls back to the default rather than reading as an "unlimited" idiom, because
  silently dropping the disk guard is the opposite of what a mistyped cap should do.

### `cached_download` — the shared cached-download protocol

The file-on-disk sibling of `cache.cached_lookup` (`.claude/rules/store.md`).
Where the two differ, the difference is deliberate — don't "align" them:

- **The in-slot re-check is skipped under `force_refresh`.** `cached_lookup`
  re-checks unconditionally; here that would make a refresh a no-op whenever a
  usable PDF is already on disk — exactly the case `force_refresh` exists to fix.
  Concurrent forced callers still coalesce onto one fetch (same `sf_key`); what
  the skip buys is that the one fetch actually re-streams instead of returning
  the file it was asked to replace.
- **The protocol writes the negative entry, not `fetch`** — opposite of
  `cached_lookup`, where the closure owns its own caching.
- **Check the artifact before the negative entry.** A `force_refresh` that 404s
  leaves a good PDF on disk beside a fresh negative entry; the file-first order
  is what keeps the next plain call serving the PDF rather than the stale error.

**`is_definitive_failure` is an allowlist**, and the corollary the OA path lives
with is the part worth stating: a 403 paywall or a 410 on an OA URL is
re-resolved and re-fetched on every call. Only `stream_to_file`'s explicit 404
branch and its `require_pdf` rejections are cacheable. This is the one home for
the allowlist-not-denylist argument — `http.error_dict` leaves **every** non-404
4xx unflagged, so a denylist would negative-cache a transient paywall for the
full TTL.

`extra_fields` is deep-copied per caller because `tools/pipeline` writes
`cascaded_invalidated` into what it receives.

## download/openaccess.py

**Trust boundary: the URL comes from `openalex.best_pdf_url` or nowhere.** Do not
add a parameter that accepts a URL, do not widen resolution to a search or a
redirect chase, and keep `require_pdf=True` on the `stream_to_file` call — this
is the only caller that passes it, because it is the only path whose URL can be a
publisher landing page.

**`_IMPORT_SUGGESTION` attaches to known-definitive failures only, and both
attachment sites gate the same way**: the resolution branch on
`not_found is True or retryable is False`, the download branch on
`is_definitive_failure` itself. That polarity is load-bearing — a denylist would
tell an agent to go fetch the PDF by hand on a 403 it should simply retry, and
would catch a `MAX_PDF_BYTES` abort (raise the cap) and a 0-byte 200 (a blip) too.

The negative half lives in this module's own namespace while the artifact lands
in `manual`, because "no OA copy exists" is a verdict specific to this path while
the PDF is shared with manual import and the rest of the pipeline.

Throttling is `per_host=True` with a global `max_concurrent` —
`.claude/rules/net.md` § `per_host` says why the latter must not follow.
