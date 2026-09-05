---
paths:
  - "src/academic_tools_mcp/_pdf_download.py"
  - "src/academic_tools_mcp/oa_download.py"
---

# PDF download and the cached-download protocol

Shared streaming-download helper behind all four `download_pdf` implementations (`providers/arxiv`, `providers/biorxiv`, `providers/acl_anthology`, `oa_download`). Slot acquisition is per-provider policy; streaming, size-capping, PDF sniffing and atomic rename are identical and live here.

- **`stream_to_file`** — opens `client.stream("GET", ...)` inside the provider's slot and streams into a sibling `mkstemp` temp, `os.replace`ing into place on success. The temp is unlinked on every failure path.
- **MAX_PDF_BYTES cap** — `resolve_max_pdf_bytes()` reads `MAX_PDF_BYTES`; unset/empty/garbage falls back to the default, and `none`/`off`/`disabled`/any value ≤ 0 disables. A download that would exceed the cap is aborted *during* the stream, so a misrouted URL can't fill the disk before any size check.
- **`is_usable_pdf(path)`** — the freshness rule for an already-downloaded file: 0 bytes, or first bytes that aren't the `%PDF-` magic number, count as a **miss**. A cached PDF's existence check is `is_usable_pdf` / `cached_hit`, never `Path.exists()` — a 0-byte file or an HTML landing page saved under `.pdf` must count as a miss, or it is served as `cached: True` forever and handed to the converter.
- **Error vocabulary.** 404 and `require_pdf` rejections → `{error, retryable: False}`; a cap abort adds `max_bytes`; a 0-byte 200 → `retryable: True`, deliberately kept out of the negative cache as a blip rather than a fact about the paper (the `%PDF-` sniff can't catch it — with no chunks the loop body never runs). `require_pdf` is the OA path's trust boundary: it sniffs the magic bytes before the rename so a publisher's HTML interstitial is rejected rather than cached as a paper.

### `cached_download` — the shared cached-download protocol

The file-on-disk sibling of `cache.cached_lookup` (`.claude/rules/cache.md`) — same force_refresh → check → single-flight → in-slot re-check → `fetch` ordering, with three deltas. Each caller supplies only a `fetch` closure holding its own quirks (arXiv/bioRxiv awaiting their own `get_paper` first, OpenAlex resolution for the OA path).

- **`force_refresh` invalidates the negative entry only.** It never unlinks the PDF: `stream_to_file` replaces `dest` only on success, so a failed refresh leaves the caller with the copy they had.
- **Artifact before negative entry.** A `force_refresh` that 404s writes a negative entry while a good PDF is still on disk; checking the file first is what keeps the next plain call serving it rather than the stale error.
- **The positive half has no TTL, because it is not a cache record** — the file *is* the entry and `is_usable_pdf` is its freshness rule. Only the negative half is JSON, which is why `neg_ttl` is a required argument: each provider states its own policy the way `positive_ttl` makes it state one for metadata.

arxiv/biorxiv pass a **tuple** `sf_key` (`("pdf", canonical)`) because their `fetch` awaits `get_paper`, which would otherwise await this very slot's future and deadlock.

**`is_definitive_failure` is an allowlist**, and all three conjuncts are load-bearing:

```python
"error" in result and result.get("retryable") is False and "max_bytes" not in result
```

`_http.error_dict` marks only `LocalBackpressureError` with `retryable`, so timeouts, 5xx and 429s arrive with no such key — a denylist ("anything not marked retryable") would negative-cache every one of them for the full TTL. The `max_bytes` sentinel exempts a `MAX_PDF_BYTES` abort: non-retryable, but a config choice a cap bump fixes rather than a fact about the paper.

**`extra_fields`** merges constant provenance into every *successful* payload, cached and fresh alike, so a decorating provider (ACL's `anthology_id` / `pdf_url`) cannot have its two branches disagree. Errors are returned undecorated. Each caller gets a deep copy — followers share the leader's object and `tools/pipeline` writes `cascaded_invalidated` into what it receives.

Verified once in `tests/test_pdf_download.py` (`TestCachedDownload`, `TestIsDefinitiveFailure`) rather than per provider.

## oa_download.py

Fetches **only** the open-access URL OpenAlex already surfaces (`best_oa_location.pdf_url` → `primary_location.pdf_url` → `open_access.oa_url`, via `openalex.best_pdf_url`), never a caller-supplied one. Do not add a parameter that accepts a URL, and do not widen the resolution to a search or a redirect chase.

**The gap is per host, the concurrency cap is global.** OA URLs come from OpenAlex, and a reference walk through one journal resolves many DOIs to the *same* publisher domain — "every URL is a different host" is an assumption, not a fact. `max_concurrent` stays global because it bounds our own egress (sockets, fds, in-flight streams — `stream_to_file` holds the slot for the whole download); per-host would let a 20-publisher walk open 40 parallel streams.

The negative half lives in this module's own namespace while the artifact lands in `manual`, because "no OA copy exists" is a verdict specific to this path.
