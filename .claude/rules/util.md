---
paths:
  - "src/academic_tools_mcp/util/*.py"
---

# Shared utilities

These modules' docstrings are exhaustive. This file covers only who must route
through what.

## doinorm.py

**Never add a local copy for inbound normalization** — including a bare DOI-shape
regex, which is a normalization decision in disguise: dispatch and caching must
agree on what a DOI is. The indirection above this module is for the *public* name
only: the tool layer imports a provider symbol (`canonical_doi`, `canonical_key`),
which is why only `biorxiv` still keeps a private `_normalize_doi` — it layers a
URL shape *on top of* `normalize`. **Don't reintroduce the other four.**

**One deliberate exception, on the *response* side:**
`openalex._canonical_from_response_doi` strips a `doi.org` URL unconditionally,
where `canonical` strips only when the path is DOI-shaped — a path it doesn't
match would survive as a full URL and miss the key the batch asked for.

**Every DOI-only tool rejects a non-DOI before any request**, through
`app.reject_non_doi` on the way in via `app.resolve_doi_identifier`. Forwarding an
arXiv ID buys a 404 and then negative-caches a key that could never have resolved.

## useragent.py

**The contact address is scrubbed to printable ASCII minus parens, and no caller
may skip that.** Both failure modes are bad and neither is visible at the call
site: a CRLF injects a header and fails only at send time as a `RequestError`, so
every request degrades to a misleading "network error" dict; a non-ASCII character
raises `UnicodeEncodeError` *inside* `httpx` at client construction, which is **not
in `HTTPX_ERRORS`** and so crashes uncaught. `config.get` strips too, but
`normalize_mailto` also takes contacts from callers, so it may not lean on that.

**Every client module builds its headers through `headers()`; none respells the
`{"User-Agent": ...}` dict.** `tests/test_politeness.py` discovers those modules by
import scan, so a new provider is guarded the moment it exists.

## textnorm.py

**Any consumer needing offsets back into the original text takes them from
`fold_with_map` / `lower_with_map`** — never a hand-rolled `str.lower()`, and not
even on the `fold=False` path, since neither transform is length-preserving.
Consumers: `papers.find_in_markdown` and `corpus` (both halves). `bibtex` key
generation, `papers._match_section_title` and `paperswithcode._slugify` take plain
`fold`, needing no offsets back.

## config.py

The roster, defaults and semantics of every setting live in `README.md`
§ Configuration. **Config never arrives as a tool parameter.**

- **`flag` and `number` are the single homes for the truthiness and *disable*
  vocabularies**, and both route through `get` rather than `os.environ`. A call site
  spelling its own `in ("1", "true", …)` is how one flag comes to accept `yes` and
  another not to.
- **Where a setting is read decides whether an operator needs a restart.** Read at
  the point of use (`streaming.resolve_max_pdf_bytes`, the `*_MAILTO` header
  builders) and an exported change applies immediately; capture at import
  (`server._DEBUG_TOOLS_ENABLED`, the headers `clients.get_client` bakes in) and it
  is fixed for the process. **Default to reading at the point of use.**
