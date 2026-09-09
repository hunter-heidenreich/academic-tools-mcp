---
paths:
  - "src/academic_tools_mcp/util/*.py"
---

# Shared utilities

**These modules' docstrings are exhaustive** — `normalize`, `normalize_mailto`,
`lower_with_map`, `original_span`, `number` and `config`'s module docstring each
carry their own invariants and the reasoning behind them. This file covers only
the cross-module rules: who must route through what, and what an operator sees.

## util/doinorm.py

**The single home for DOI normalization**, and the indirection above it is for
the *public* name only. The tool layer imports a provider symbol
(`canonical_doi`, `canonical_key`) rather than `doinorm` directly, because
`tools/paper` and `manual._ROUTES` need one. It never applied to a *private*
`_normalize_doi` alias — which is why only `biorxiv` still has one, the module
that layers a URL shape *on top of* `normalize`. **Don't reintroduce the other
four.** `tools/graph` is the one direct `doinorm` import above the providers: it
needs the shape predicate and has no provider wrapper to borrow it from.

**Never add a local copy for inbound normalization** — including a bare
DOI-shape regex, which is a normalization decision in disguise: dispatch and
caching must agree on what a DOI is. `REGISTRANT_PATTERN`, `biorxiv.DOI_PREFIX`
and `acl.ACL_DOI_PREFIX` are exported for the one consumer that needs the
*pattern* rather than the function (`corpus`, inverting a stored filename stem);
build from them instead of respelling `10\.\d{4,}`.

One deliberate exception, on the *response* side:
`openalex._canonical_from_response_doi` strips a `doi.org` URL unconditionally,
where `canonical` strips only when the path is DOI-shaped — a path it doesn't
match would survive as a full URL and miss the key the batch asked for.

**The graph tools reject a non-DOI before any request.** `_reject_non_doi` gates
all four Crossref/OpenCitations tools on `looks_like_doi`, the same predicate the
metadata dispatcher routes on. Forwarding an arXiv ID buys a 404 and then
negative-caches a key that could never have resolved.

## util/useragent.py

**The contact address is scrubbed to printable ASCII minus parens, and no caller
may skip that.** It is the one operator-supplied string interpolated into a
header, and both failure modes are bad: a CRLF injects a header and only fails at
send time as a `RequestError`, so every request degrades to a misleading "network
error" dict; a non-ASCII character raises `UnicodeEncodeError` *inside* `httpx`
at client construction, which is **not in `HTTPX_ERRORS`** and so crashes
uncaught. `config.get` strips too, but `normalize_mailto` also takes contacts
from callers, so it may not lean on that.

**Every client module builds its headers through `headers()`; none respells the
`{"User-Agent": ...}` dict.** `tests/test_politeness.py` discovers those modules
by import scan — a module holding both `_get_client` and `_throttle` — for the
reason `stats.throttles` scans rather than reading a roster: a new provider is
guarded the moment it exists.

## util/textnorm.py

**Any consumer that needs offsets back into the original text takes them from
`fold_with_map` / `lower_with_map` — never a hand-rolled `str.lower()`, and not
even on the `fold=False` path.** Neither transform is length-preserving, so an
unmapped offset drifts the snippet window and the section attribution off the
real match. Consumers: `papers.find_in_markdown`, `papers._match_section_title`,
`corpus` (both halves), and `bibtex` key generation (`fold` only).

**Turning a transformed span back into an original slice goes through
`original_span`, never two `index_map[...]` lookups.** A span ending inside one
original character's expansion (the `f` of a folded `ﬁ`) has both ends resolving
to the same index and slices to nothing.

## util/config.py

The roster, defaults and semantics of every setting live in `README.md`
§ Configuration. **Config never arrives as a tool parameter.**

- **`flag` and `number` are the single homes for the truthiness and *disable*
  vocabularies**, and both route through `get` rather than `os.environ`, so the
  three accessors cannot disagree about what "set" means. A call site spelling
  its own `in ("1", "true", …)` is how one flag comes to accept `yes` and another
  not to.
- **`on_nonpositive` is the one deliberate divergence between `number`'s
  callers**: `"default"` for `MAX_PDF_BYTES` (a negative cap is a typo, and
  honouring it drops the disk guard), `"disable"` for the two `PDF_*_TIMEOUT`s (a
  non-positive timeout is a second disable idiom). Pass it explicitly; a new
  caller that wants a third policy needs a reason, not a default.
- **Where a setting is read decides whether an operator needs a restart.** `get`
  re-reads `os.environ` per call, so a read at the point of use
  (`streaming.resolve_max_pdf_bytes`, the `*_MAILTO` header builders) picks up an
  exported change immediately, while a value captured at import
  (`server._DEBUG_TOOLS_ENABLED`, crossref's `_resolve_policy()` constants) is
  fixed for the process. **Default to reading at the point of use**; capture at
  import only when you want the startup snapshot.
- **A blank setting is not a set one.** A present-but-blank or whitespace-only
  `CROSSREF_MAILTO=` behaves exactly like omitting the line — and that is not
  cosmetic: it drops Crossref to the public tier, lowering concurrency *and* both
  request rates (`.claude/rules/providers.md` § crossref.py).
- **`ACADEMIC_TOOLS_ENV_FILE` is authoritative**: set means it is the only
  candidate, so a typo'd path is "no `.env`" rather than a silent fallback to a
  different operator's config. Editing the file needs a restart; real environment
  variables always win regardless.
