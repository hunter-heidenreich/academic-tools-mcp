---
paths:
  - "src/academic_tools_mcp/_doi.py"
  - "src/academic_tools_mcp/_useragent.py"
  - "src/academic_tools_mcp/_textnorm.py"
  - "src/academic_tools_mcp/config.py"
---

# Shared utilities

## _doi.py

The single home for DOI normalization: `normalize` (bare form), `canonical` (cache-key form), `looks_like_doi` (shape test). Every module that normalizes a DOI routes through it: `manual.normalize_identifier`, and the five DOI providers through their thin `_normalize_doi` / `canonical_doi` wrappers. The tool layer calls those wrappers, never `_doi` directly.

**Invariant: the `doi:` prefix is stripped before the URL handling**, since `"doi:https://doi.org/10.x/y"` occurs in the wild, and whitespace is re-stripped after removing it so a pasted `"doi: 10.1234/x"` normalizes rather than being reported as an unknown identifier. A second copy that misses either lands one paper under several cache keys, some of which build malformed upstream URLs. Guarded by `tests/test_doi_properties.py`, whose spelling list includes the nested `doi:https://doi.org/...` form.

**Never add a local copy** for inbound normalization. The one deliberate exception is `openalex._canonical_from_response_doi`, on the *response* side: `canonical` strips a `doi.org` URL only when the path matches its DOI shape, so an unrecognised registrant would survive as a full URL and fail to match the key the batch asked for — that side must strip unconditionally. Per-provider *policy* — which prefix a URL path needs, whether an ID is an Anthology ID — stays in the provider.

## _useragent.py

The single home for the outbound `User-Agent`: `build(mailto)`, `headers(mailto)`, `package_version()`. Why every client must pass headers, and the shape upstreams ask for, are in `.claude/rules/providers.md` § Common shape — don't restate them here.

Two things live only here: the version comes from installed distribution metadata, never a literal, and the descriptive agent is returned with or without a contact address — anonymous-but-identifiable still beats `python-httpx`. `tests/test_politeness.py::TestEveryProviderIdentifiesItself` pins both across every client.

## _textnorm.py

Unicode folding for diacritic-insensitive search, used by `papers.find_in_markdown`, `cache_search`, and `bibtex` key generation.

- `fold(text)` — NFKD-decompose and drop combining marks, for tokenisation and key generation where character positions don't matter.
- `fold_with_map(text)` / `lower_with_map(text, *, fold=False)` — the transformed string **plus an index map back to original offsets**, so a match found in transformed text can be sliced out of the original.

**The offset map is the whole point, and it is not optional even without folding.** Neither transform is length-preserving: a ligature folds 1→N (`ﬁ` → `fi`), and `str.lower()` does too (U+0130 `İ` → two chars). The transform therefore runs per *original* character and attributes every produced char to exactly one original index. A consumer that lowercases by hand instead drifts its snippet windows and section attribution off the real match. `tests/test_textnorm.py::TestLowerWithMap`.

## config.py

`get(key)` is the accessor for every runtime setting (`CACHE_DIR`, `MAX_PDF_BYTES`, `PDF_CONVERTER`, `ENABLE_DEBUG_TOOLS`, the per-provider `*_MAILTO` / `*_API_KEY`); config never arrives as a tool parameter. One deliberate exception: `_stats` reads `DEBUG_REQUESTS` from `os.environ` directly so a test can monkeypatch it per-case.

- **`.env` resolution is a four-candidate search**, first existing file wins: `ACADEMIC_TOOLS_ENV_FILE` (explicit override) → project root relative to this file (source checkout) → `$PWD/.env` → `$XDG_CONFIG_HOME` (or `~/.config`) `/academic-tools-mcp/.env`. A project-root-only rule points inside the virtualenv from `site-packages` and silently disables every env var for an installed wheel — the exact deployment `.env.example` tells operators to set `CACHE_DIR` for. An unreadable candidate (permissions, dangling symlink) is skipped rather than fatal.
- **Real environment variables always win** — `load_dotenv` runs without `override`, so an exported value takes effect regardless of any file.
- **`get` reads an empty string as unset**, so a present-but-blank `CROSSREF_MAILTO=` behaves exactly like omitting the line. Not cosmetic: it drops Crossref to the public tier, lowering concurrency *and* both request rates — see `.claude/rules/providers.md` § Crossref.
- **Resolved once at import** (`ENV_FILE` records the winner), so a `.env` edit needs a server restart.
