---
paths:
  - "src/academic_tools_mcp/_pdf_download.py"
---

# PDF download and the cached-download protocol

## _pdf_download.py

Shared streaming-download helper used by `arxiv.download_pdf`, `biorxiv.download_pdf`, and `acl_anthology.download_pdf`. The slot acquisition is per-provider (different gap / concurrency caps), but the streaming + size-capping + atomic-rename logic is identical and lives here.

- **`stream_to_file`** — opens `client.stream("GET", ...)` inside the provider's slot, writes 64 KiB chunks to a sibling `.tmp` file via `mkstemp`, and `os.replace`s into place on success. Peak memory = one chunk, not the whole PDF.
- **MAX_PDF_BYTES cap** — `resolve_max_pdf_bytes()` reads `MAX_PDF_BYTES` env var (default 200_000_000; `none`/`off`/`disabled`/`0` disables). A download that would exceed the cap is aborted mid-stream with `{error, retryable: False, max_bytes}`; the partial temp is unlinked, dest is never created. Fires *during* the download so a misrouted URL can't fill the disk before any size check.
- **Cleanup** — every non-success path (404, transport error, size cap, exception) unlinks the temp file. The fd is closed manually if we never reached `os.fdopen` (early-return before the write loop). Success paths leave `tmp_path.unlink()` as a no-op because `os.replace` already moved the file.
- **`is_usable_pdf(path)`** — the freshness rule for an already-downloaded file: 0 bytes, or first bytes that aren't the `%PDF-` magic number, count as a **miss** rather than a hit. Every `dest.exists()` short-circuit in a provider must route through it — bare `exists()` serves a 0-byte file as `cached: True` forever and hands it to the converter.
- **404 → `{error, retryable: False}`.** The flag is what `is_definitive_failure` below reads to negative-cache it, and what lets an agent tell a dead URL from a blipped one.

### `cached_download` — the shared cached-download protocol

The file-on-disk sibling of `cache.cached_lookup`, named to match it. All four `download_pdf` implementations (`providers/arxiv`, `providers/biorxiv`, `providers/acl_anthology`, `oa_download`) route through it; each supplies only a `fetch` closure holding its own quirks (arXiv/bioRxiv awaiting their own `get_paper` first, OpenAlex resolution for the OA path).

1. `force_refresh` → invalidate the **negative** entry only, then always fetch. It never unlinks the PDF: `stream_to_file` replaces `dest` only on success, so a failed refresh leaves the caller with the copy they had.
2. Otherwise short-circuit on a usable cached PDF, **then** on a negative entry. That order is load-bearing — a `force_refresh` that 404s writes a negative entry while a good PDF is still on disk, and checking the file first is what keeps the next plain call serving it rather than the stale error.
3. Single-flight by `sf_key` (default `canonical`), re-checking both inside the slot. arxiv/biorxiv pass a **tuple** key (`("pdf", canonical)`) because their `fetch` awaits `get_paper`, which would otherwise await this very slot's future and deadlock.

**The positive half has no TTL, because it is not a cache record** — the file *is* the entry and `is_usable_pdf` (above) is its freshness rule. Only the negative half is JSON, which is why `neg_ttl` is a required argument: each provider states its own policy the way `positive_ttl` makes it state one for metadata.

**`is_definitive_failure` is an allowlist**, and all three conjuncts are load-bearing:

```python
"error" in result and result.get("retryable") is False and "max_bytes" not in result
```

`_http.error_dict` leaves `retryable` off every transient branch, so a denylist ("anything not marked retryable") would negative-cache timeouts, 5xx and 429s for the full TTL. The `max_bytes` sentinel is what exempts a `MAX_PDF_BYTES` abort — non-retryable, but a config choice a cap bump fixes rather than a fact about the paper. The docstring carries the full rationale; it lives beside `stream_to_file` because it classifies that function's error vocabulary.

**`extra_fields`** merges constant provenance into every *successful* payload, cached and fresh alike, so a decorating provider (ACL's `anthology_id` / `pdf_url`) cannot have its two branches disagree. Errors are returned undecorated. Each caller gets a deep copy — followers share the leader's object and `tools/pipeline` writes `cascaded_invalidated` into what it receives.

Verified once in `tests/test_pdf_download.py` (`TestCachedDownload`, `TestIsDefinitiveFailure`) rather than per provider.
