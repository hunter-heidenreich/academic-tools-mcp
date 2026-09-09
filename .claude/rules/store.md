---
paths:
  - "src/academic_tools_mcp/store/*.py"
---

# Cache and the cached-getter protocol

**Per-function behaviour lives in the docstrings.** This file covers only what no
single docstring can: the cross-module contracts, the accepted races, and the
edits that look safe and aren't.

## store/cache.py

**Atomicity, not durability.** `fsync` is deliberately skipped, survivable only
because every read self-heals through `_read_entry`. **A new read path must hold
up its half of that bargain**, and must name its encoding explicitly (UTF-8) so
records survive an `LC_ALL=C` host.

**`atomic.write_text` is the single write seam**, so patching it fakes a full
disk for both writers at once. `_unlink_quietly` needs no counter of its own:
every path that reaches a failed unlink goes on to `put` in the same directory,
which fails and ticks `cache_write_failures`.

**Counters partition, they don't overlap.** A read that finds nothing counts
nothing — which is why `openalex._fetch_chunk` books its own misses (it bypasses
`cached_lookup`) and why `papers`' sections reads move no counter at all. PDF
downloads are outside this series entirely.

**Cross-module surface — renames here break callers.** `cache_dir` and
`CACHE_ROOT` are public because the path builders live elsewhere
(`stems.pdf_path` / `markdown_path`, `corpus`). Anything reached from another
module gets a public name; a leading underscore here means genuinely internal.

**The orphan `.tmp` sweep rests on one invariant: a temp file's mtime is its own
write time.** `atomic.copy` therefore calls `os.utime` after `shutil.copystat` —
a copy that inherits an old source's mtime hands the sweep a live temp that reads
as an orphan.

**Boundaries fall on the safe side.** An entry aged exactly `max_age_seconds`
still hits (`age > max_age`), a negative entry at exactly its expiry is still
live (`expires_at < now`), and a `.tmp` exactly at the cutoff *is* swept
(`mtime > cutoff`).

Per-provider TTL policy lives in `.claude/rules/providers.md`; the operator-facing
table is `README.md` § Caching.

### `cached_lookup` — the shared cached-getter protocol

**Two paths deliberately re-implement its ordering; a change here must be
mirrored in both.** `openalex.get_works_batch` open-codes the outer
invalidate / positive / negative check per DOI and coalesces a whole chunk
through `_fetch_chunk`; `streaming.cached_download` is the file-on-disk sibling
(`.claude/rules/download.md`). Anything else that wants this ordering calls
`cached_lookup`.

**Pass a tuple `sf_key` whenever one identifier has more than one sub-fetch** —
`("work", canonical)`, `("author", canonical)`, opencitations' `(kind, canonical)`.
Each provider module owns *one* `SingleFlight` shared by all of its keys, so an
un-namespaced key collides across entities, and a `fetch` that awaits another
getter on the same instance self-deadlocks unless its key differs. This is the
one home for that rule.

**Known narrow race (unguarded, accepted).** Between a `force_refresh`'s
`invalidate` and its in-slot re-check the entry can be rewritten — by a
concurrent `get_works_batch` chunk under a different `sf_key`, or by a
non-refresh leader the refresher queued behind — and the refresher then serves it
without fetching. A property of the protocol, so it applies to every getter, not
to any one provider.

## store/singleflight.py

Cancellation is the subtle part, and neither direction may leak.

- **A cancelled leader must not fail its followers.** The leader's task ending
  says nothing about the followers' lifetimes, so the `CancelledError` set on the
  shared future is not theirs to honour. `_self_is_cancelling` —
  `Task.cancelling()`, the 3.11 cancel/uncancel protocol — is what tells a
  genuine follower from one that is itself cancelling.
- **A cancelled follower must not reach the leader**, which is what
  `asyncio.shield` is for. Cancelling a task cancels the future it is suspended
  on, and for a follower that is the *shared* future: unshielded, one follower
  giving up cancels the slot out from under everybody, the leader's `set_result`
  raises `InvalidStateError` into its own caller in place of a good result, and
  every remaining follower re-runs the factory.
- **`_MAX_FOLLOW_ATTEMPTS` bounds failed *follows*, not takeovers** — a caller
  that wins the slot returns and never comes back round the loop. Exhausting it
  means leaders were cancelled that many times in a row, and the caller then runs
  the factory itself, unslotted, rather than spinning.

The leader registers its future before its first await, and nothing suspends
between `do`'s check and that insert. **Keep both halves await-free.**

## store/stems.py

### Naming

- **`urllib.parse.quote` is the sole authority on the output alphabet.** Don't
  reintroduce a hand-written keep-set beside it, because the two drift: `quote`
  passes the whole RFC 3986 unreserved set (`~` and `_` included) and a keep-set
  that under-counts it makes `_MIGRATED_STEM_RE` wrong.
- **`safe_stem` is not idempotent** (`a%20b` → `a%2520b`), so anything
  re-deriving a stem must gate on `_MIGRATED_STEM_RE` first.
  **Invariant: `_MIGRATED_STEM_RE` is exactly `safe_stem`'s output alphabet** — a
  character the writer emits but the gate rejects makes startup re-encode a
  correct name (`notes~draft%202024` → `notes~draft%25202024`) and orphan the
  file it just renamed.
- **A caller holding a *stem* off disk rather than an identifier takes
  `markdown_path_for_stem` / `sections_key_for_stem`** — re-sanitizing a stored
  stem encodes its own escapes.
- **No startup sweep may raise**, and `list_dir` is the shared gate for all
  three: `migrate_legacy_stems`, `manual.migrate_misrouted_arxiv`,
  `acl.migrate_legacy_pdf_stems`.

### Checksums

**A writer checksums the string it parsed (`checksum_text`), never the file it
just wrote.** The two are separated by a window another writer fits through, and
an index stamped with the *other* document's checksum matches disk forever — so
`_reparse_sections_locked` accepts it and never re-parses. This is what makes the
entry correct by construction rather than by lock discipline: a losing writer's
entry simply mismatches and self-heals.

The on-disk half of that comparison is a *test* oracle —
`tests/helpers/checksums.py`, deliberately not importable from `src/`. Re-adding
it to `stems` so a writer can reach it is the bug the paragraph above describes.
