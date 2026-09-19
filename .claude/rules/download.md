---
paths:
  - "src/academic_tools_mcp/download/*.py"
---

# PDF download and the cached-download protocol

## streaming.py

- **`manual.import_local_pdf` and `tools/pipeline.convert_paper` both gate on
  `is_usable_pdf`**, which is the only reason `papers.convert_pdf`'s bare
  `.exists()` is safe. Don't add a second ungated path.
- **Every new terminal branch in `stream_to_file` must carry an explicit `retryable`
  verdict**, or `is_definitive_failure` silently treats it as transient.
- **`stream_to_file`'s `except OSError` is what makes a local write failure
  `{error, retryable: True}`** — a disk operation added outside it is not covered.

### `cached_download` vs `cache.cached_lookup`

The file-on-disk sibling. Three divergences are deliberate — **don't "align" them**:

- **The in-slot re-check is skipped under `force_refresh`**, or a refresh would be a
  no-op whenever a usable PDF is already on disk. Concurrent forced callers still
  coalesce onto one fetch; what the skip buys is that the fetch actually re-streams.
- **The protocol writes the negative entry, not `fetch`.**
- **Check the artifact before the negative entry**, so a `force_refresh` that 404s
  keeps serving the good PDF rather than the fresh error beside it.

**Accepted limitation: a 403 paywall or a 410 on an OA URL is re-resolved and
re-fetched on every call**, the price of `is_definitive_failure` being an allowlist.

## openaccess.py

**Don't widen the trust boundary.** Do not add a parameter that accepts a URL, do
not widen resolution to a search or a redirect chase, and keep `require_pdf=True` —
this is the only caller that passes it, because it is the only path whose URL can
be a publisher landing page.

**`_IMPORT_SUGGESTION` attaches to known-definitive failures only**, at both
attachment sites — a denylist would catch a `MAX_PDF_BYTES` abort and a 0-byte 200
as well.

**The negative half lives in this module's own namespace while the artifact lands in
`manual`**: "no OA copy exists" is a verdict specific to this path, while the PDF is
shared with manual import and the rest of the pipeline.
