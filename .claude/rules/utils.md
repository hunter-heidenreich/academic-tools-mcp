---
paths:
  - "src/academic_tools_mcp/_doi.py"
  - "src/academic_tools_mcp/_useragent.py"
  - "src/academic_tools_mcp/_textnorm.py"
  - "src/academic_tools_mcp/config.py"
---

# Shared utilities

## _doi.py

The single home for DOI normalization: `normalize` (bare form), `canonical` (cache-key form), `looks_like_doi` (shape test). Inbound normalization routes through it — `manual._normalize_identifier` and `manual.resolve_metadata_source`, plus each DOI provider's wrapper: `canonical_doi` in `openalex` / `crossref` / `opencitations`, `canonical_key` in `biorxiv` / `acl_anthology`. A provider's own URL shape layers *on top of* `_doi.normalize` (`biorxiv._normalize_doi` applies `_BIORXIV_URL_RE` after calling it), never instead of it. The tool layer calls the wrappers, never `_doi` directly.

**Invariant: the `doi:` prefix is stripped before the URL handling, and whitespace re-stripped after.** `"doi:https://doi.org/10.x/y"` and a pasted `"doi: 10.1234/x"` both occur in the wild; a copy that gets the order wrong lands one paper under several cache keys, some of which build malformed upstream URLs.

**Never add a local copy for inbound normalization** — including a bare DOI-shape regex, which is a normalization decision in disguise: dispatch and caching must agree on what a DOI is. One deliberate exception, on the *response* side: `openalex._canonical_from_response_doi` strips a `doi.org` URL unconditionally, where `canonical` strips only when the path is DOI-shaped, so a path it doesn't match would survive as a full URL and miss the key the batch asked for.

## _useragent.py

The single home for the outbound `User-Agent`: `build(mailto)`, `headers(mailto)`, `package_version()`.

**The version comes from installed distribution metadata, never a literal, and the agent stays descriptive when no mailto is configured** — anonymous-but-identifiable still beats `python-httpx`. Politeness coverage is parametrized over a client list in `tests/test_politeness.py`; a new client must be appended to it or it is unguarded.

## _textnorm.py

Diacritic folding for search: `papers.find_in_markdown` and `cache_search` (both halves), `bibtex` key generation (`fold` only).

**Any consumer that needs offsets back into the original text takes them from `fold_with_map` / `lower_with_map` — never a hand-rolled `str.lower()`, and not even on the `fold=False` path.** Neither transform is length-preserving (`ﬁ` → `fi`; U+0130 `İ` lowercases to two chars), so the transform runs per *original* character and attributes every produced char to exactly one original index. An unmapped offset drifts the snippet window and the section attribution off the real match.

## config.py

`get(key)` is the accessor for every runtime setting — the roster, defaults and semantics live in `README.md` § Configuration. Config never arrives as a tool parameter.

- **Where a setting is read decides whether an operator needs a restart.** `get` re-reads `os.environ` on every call, so a read at the point of use (`_pdf_download.resolve_max_pdf_bytes`, the `*_MAILTO` header builders) picks up an exported change immediately, while a value captured at import (`server._DEBUG_TOOLS_ENABLED`, crossref's `_resolve_policy()` constants) is fixed for the process. Default to reading at the point of use; capture at import only when you want the startup snapshot.
- **`get` reads an empty string as unset**, so a present-but-blank `CROSSREF_MAILTO=` behaves exactly like omitting the line. Not cosmetic: it drops Crossref to the public tier, lowering concurrency *and* both request rates — see `.claude/rules/providers.md` § Crossref.
- **The `.env` *file* is resolved once at import**, first existing candidate wins (the order is in the module docstring; `ENV_FILE` records the winner). A project-root-only rule would point inside the virtualenv from `site-packages` and silently disable every env var for an installed wheel; an unreadable candidate is skipped rather than fatal. Editing the file needs a restart. **Real environment variables always win** regardless — `load_dotenv` runs without `override`.
