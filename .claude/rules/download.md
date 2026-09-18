---
paths:
  - "src/academic_tools_mcp/download/*.py"
---

# PDF download and the cached-download protocol

## streaming.py

- **Gate every cached-PDF check on `is_usable_pdf` / `cached_hit`, never
  `Path.exists()`** — a 0-byte file or an HTML landing page saved under `.pdf` must
  count as a miss. `manual.import_local_pdf` and `tools/pipeline.convert_paper` both
  gate this way, which is the only reason `papers.convert_pdf`'s bare `.exists()` is
  safe. Don't add a second ungated path, and don't hand-roll the check-then-`stat`
  pair — **`cached_hit` owns the `stat`.**
- **Every new terminal branch in `stream_to_file` must carry an explicit `retryable`
  verdict**, or `is_definitive_failure` silently treats it as transient.
- **`stream_to_file`'s `except OSError` is the guarantee that a local write failure
  is `{error, retryable: True}` rather than a raised exception.** A disk operation
  added inside it inherits that; one added outside does not.

### `cached_download` vs `cache.cached_lookup`

The file-on-disk sibling. Three divergences are deliberate — **don't "align" them**:

- **The in-slot re-check is skipped under `force_refresh`**, or a refresh would be a
  no-op whenever a usable PDF is already on disk. Concurrent forced callers still
  coalesce onto one fetch; what the skip buys is that the fetch actually re-streams.
- **The protocol writes the negative entry, not `fetch`.**
- **Check the artifact before the negative entry**, so a `force_refresh` that 404s
  keeps serving the good PDF rather than the fresh error beside it.

**`is_definitive_failure` is an allowlist**, and the corollary the OA path lives
with is worth stating: a 403 paywall or a 410 on an OA URL is re-resolved and
re-fetched on every call. **This is the one home for the allowlist-not-denylist
argument** — `http.error_dict` leaves every non-404 4xx unflagged, so a denylist
would negative-cache a transient paywall for the full TTL.

## openaccess.py

**Trust boundary: the URL comes from `openalex.best_pdf_url` or nowhere.** Do not
add a parameter that accepts a URL, do not widen resolution to a search or a
redirect chase, and keep `require_pdf=True` — this is the only caller that passes
it, because it is the only path whose URL can be a publisher landing page.

**`_IMPORT_SUGGESTION` attaches to known-definitive failures only, and both
attachment sites gate as an allowlist.** A denylist would tell an agent to hand-fetch
a 403 it should simply retry, and would catch a `MAX_PDF_BYTES` abort and a 0-byte
200 too.

**The negative half lives in this module's own namespace while the artifact lands in
`manual`**: "no OA copy exists" is a verdict specific to this path, while the PDF is
shared with manual import and the rest of the pipeline.
