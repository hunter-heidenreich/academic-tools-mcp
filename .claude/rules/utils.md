---
paths:
  - "src/academic_tools_mcp/_doi.py"
  - "src/academic_tools_mcp/_useragent.py"
  - "src/academic_tools_mcp/_textnorm.py"
  - "src/academic_tools_mcp/config.py"
---

# Shared utilities

## _doi.py

The single home for DOI normalization: `normalize` (bare form), `canonical` (cache-key form), `looks_like_doi` (shape test). Inbound normalization routes through it — `manual._normalize_identifier` and `manual.resolve_metadata_source`, plus each DOI provider's wrapper: `canonical_doi` in `openalex` / `crossref` / `opencitations`, `canonical_key` in `biorxiv` / `acl_anthology`. A provider's own URL shape layers *on top of* `_doi.normalize` (`biorxiv._normalize_doi` applies `_BIORXIV_URL_RE` after calling it), never instead of it. The tool layer calls the wrappers for *normalization*; the one direct `_doi` import above the providers is `tools/graph`, which needs the shape predicate and has no provider wrapper to borrow it from.

**Invariant: the `doi:` prefix is stripped before the URL handling, in a loop, and whitespace re-stripped after.** `"doi:https://doi.org/10.x/y"` and a pasted `"doi: 10.1234/x"` both occur in the wild; a copy that gets the order wrong lands one paper under several cache keys, some of which build malformed upstream URLs. The loop is what makes `normalize` **idempotent for every input** — a single pass leaves `"doi:doi:10.x/y"` keying separately from its own output.

**Invariant: a bare DOI is verbatim; the URL form cuts at `?`/`#`.** Both are legal DOI suffix characters, so truncating a bare DOI there would silently key a *different* paper; in a URL they are unresolvable unless percent-encoded, so a literal one is a query string. The asymmetry is the policy, not a gap; the property-based strategy excludes `?#` for exactly this reason.

**Never add a local copy for inbound normalization** — including a bare DOI-shape regex, which is a normalization decision in disguise: dispatch and caching must agree on what a DOI is. `REGISTRANT_PATTERN` is exported for the one consumer that needs the pattern rather than the function (`cache_search._MANUAL_DOI_STEM_RE`, inverting a stored filename stem); build from it instead of respelling `10\.\d{4,}`. One deliberate exception, on the *response* side: `openalex._canonical_from_response_doi` strips a `doi.org` URL unconditionally, where `canonical` strips only when the path is DOI-shaped, so a path it doesn't match would survive as a full URL and miss the key the batch asked for.

**Response-side DOIs get normalized too.** All three `bibtex` generators run the provider's DOI through `normalize` before emitting a `doi=` field: OpenAlex serves the DOI as a resolver URL and, for older records, over plain http, and a `doi=` field holding a URL renders as a doubled resolver link.

**The graph tools reject a non-DOI before any request.** `tools/graph._reject_non_doi` gates all four Crossref/OpenCitations tools on `looks_like_doi` — the same predicate the metadata dispatcher routes on. Forwarding an arXiv ID buys a 404 and then negative-caches a key that could never have resolved.

## _useragent.py

The single home for the outbound `User-Agent`: `build(mailto)`, `headers(mailto)`, `normalize_mailto(mailto)`, `package_version()`.

**The version comes from installed distribution metadata, never a literal, and the agent stays descriptive when no mailto is configured** — anonymous-but-identifiable still beats `python-httpx`. `package_version` is cached because every provider's `_get_client` rebuilds its headers per request, which would otherwise put an `importlib.metadata` `sys.path` scan on the hot path.

**The contact address is scrubbed to printable ASCII minus parens, and no caller may skip that.** It is the one operator-supplied string interpolated into a header: a CRLF injects a header and only fails at send time as a `RequestError`, so every request degrades to a misleading "network error" dict; a non-ASCII character raises `UnicodeEncodeError` *inside* `httpx` at client construction, which is not in `HTTPX_ERRORS` and so crashes uncaught. `config.get` strips too, but `normalize_mailto` also takes contacts from callers, so it may not lean on that.

**Invariant: `normalize_mailto` scrubs before stripping the `mailto:` prefix, and the strip is a loop** — the ordering `_doi.normalize` holds for `doi:`, for the same reason. Scrubbing can *reveal* a prefix (`mail(to:x` → `mailto:x`), so the other order is not idempotent. A contact that normalizes to empty is dropped entirely rather than emitting a bare `mailto:`.

**Every client module builds its headers through `headers()`; none respells the `{"User-Agent": ...}` dict.** Politeness coverage in `tests/test_politeness.py` discovers those modules by import scan — a module holding both `_get_client` and `_throttle` — for the reason `_stats.throttles` scans rather than reading a roster: a new provider is guarded the moment it exists.

## _textnorm.py

Diacritic folding for search: `papers.find_in_markdown`, `papers._match_section_title` and `cache_search` (both halves), `bibtex` key generation (`fold` only).

**Any consumer that needs offsets back into the original text takes them from `fold_with_map` / `lower_with_map` — never a hand-rolled `str.lower()`, and not even on the `fold=False` path.** Neither transform is length-preserving (`ﬁ` → `fi`; U+0130 `İ` lowercases to two chars), so the index map is built per *original* character and attributes every produced char to exactly one original index. An unmapped offset drifts the snippet window and the section attribution off the real match.

**The string is the whole-string transform; only the map is per-character.** `str.lower()` is context-*sensitive* at a word-final Greek sigma (`"ΟΔΥΣΣΕΥΣ".lower()` ends in `ς`), so a per-character lowercase would emit a string the whole-string tokeniser never produces and a Greek hit would come back with no snippet and no section. Reuniting the two halves rests on one property: per-character and whole-string transforms always agree in *length*, because every context-sensitive case mapping Python implements is one-to-one. Pinned by `hypothesis`, not asserted at runtime.

**Turning a transformed span back into an original slice goes through `original_span`, never two `index_map[...]` lookups.** A span that ends inside one original character's expansion (the `f` of a folded `ﬁ`) has both ends resolving to the same index and slices to nothing; `original_span` widens the end past the last contributing character while still preferring `index_map[end]`, the entry that swallows combining marks dropped after the span.

**The per-character loop is skipped for runs of ASCII** — NFKD is the identity there, no ASCII char is combining, and ASCII lowercasing is one-to-one, so the run maps to itself index-for-index. Worth ~4x on a paper-sized document, which these run over per section and per search winner.

## config.py

`get(key)` is the accessor for every runtime setting, `flag(key)` the accessor for the boolean ones, `number(key, default, cast=, on_nonpositive=)` for the ones an operator can turn off — the roster, defaults and semantics live in `README.md` § Configuration. Config never arrives as a tool parameter.

- **`flag` is the single home for env-var truthiness** (`_TRUE_VALUES`, case-insensitive). A call site that spells its own `in ("1", "true", …)` is how one flag comes to accept `yes` and another not to; both current callers (`server._DEBUG_TOOLS_ENABLED`, `_stats.debug_requests_enabled`) route through it.
- **`number` is the single home for the *disable* vocabulary** (`_DISABLE_VALUES`), for the same reason — two modules spelling their own `{"none", "off", …}` is how one came to accept `disabled` and the other a bare `-1`. The remaining divergence is `on_nonpositive`, and it is deliberate on both sides: `"default"` for `MAX_PDF_BYTES` (a negative cap is a typo, and honouring it drops the disk guard), `"disable"` for the two `PDF_*_TIMEOUT`s (a non-positive timeout is a second disable idiom). Pass it explicitly; a new caller that wants a third policy needs a reason, not a default. A non-finite *float* (`nan`, `inf`) falls back to the default — `nan` compares false against every bound, so it reaches the caller as a limit nothing satisfies. **The guard is `isinstance(value, float)`, not a bare `math.isfinite`**: an `int` is finite by construction, and `math.isfinite(10**400)` raises `OverflowError` converting to a float, which would turn an absurd-but-harmless `MAX_PDF_BYTES` into a crash on every download.
- **`get` strips surrounding whitespace and reads the empty result as unset**, so a caller never re-strips. Whitespace *inside* a value survives — converter templates and paths need it. **Invariant: `flag` and `number` route through `get` rather than `os.environ`**, so the three accessors cannot disagree about what "set" means. A padded `CROSSREF_MAILTO` reaching `in_polite_pool` as truthy while `normalize_mailto` dropped it is what that buys.
- **Where a setting is read decides whether an operator needs a restart.** `get` re-reads `os.environ` on every call, so a read at the point of use (`_pdf_download.resolve_max_pdf_bytes`, the `*_MAILTO` header builders) picks up an exported change immediately, while a value captured at import (`server._DEBUG_TOOLS_ENABLED`, crossref's `_resolve_policy()` constants) is fixed for the process. Default to reading at the point of use; capture at import only when you want the startup snapshot.
- **A blank setting is not a set one.** A present-but-blank or whitespace-only `CROSSREF_MAILTO=` behaves exactly like omitting the line. Not cosmetic: it drops Crossref to the public tier, lowering concurrency *and* both request rates — see `.claude/rules/providers.md` § Crossref.
- **The `.env` *file* is resolved once at import**, first existing candidate wins (the order is in the module docstring; `ENV_FILE` records the winner and `_stats.snapshot` reports it). A project-root-only rule would point inside the virtualenv from `site-packages` and silently disable every env var for an installed wheel. **`ACADEMIC_TOOLS_ENV_FILE` is authoritative** — set means it is the only candidate, so a typo'd path is "no `.env`" rather than a silent fallback to a different operator's config. Editing the file needs a restart. **Real environment variables always win** regardless — `load_dotenv` runs without `override`.
- **No candidate may abort the import.** `Path.cwd()` raises on a deleted working directory, `expanduser()` on an unresolvable home, `is_file()` on a directory we can't traverse, and `load_dotenv` raises `UnicodeDecodeError` — not an `OSError` — on a `.env` that isn't UTF-8. Each is skipped so the next candidate is still tried. `OSError` alone does not cover them: a `UnicodeDecodeError` escaping `_load_env` kills the console script before it starts.
