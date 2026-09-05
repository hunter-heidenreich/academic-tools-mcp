---
paths:
  - "src/academic_tools_mcp/_doi.py"
  - "src/academic_tools_mcp/_useragent.py"
  - "src/academic_tools_mcp/_textnorm.py"
  - "src/academic_tools_mcp/config.py"
---

# Shared utilities

## _doi.py

The single home for DOI normalization: `normalize` (bare form), `canonical` (cache-key form), `looks_like_doi` (shape test). Every module that touches a DOI routes through it — providers, `manual`, `bibtex`, `cache`, and the tool layer.

`normalize` must handle `doi.org` / `dx.doi.org` URLs over either scheme and a case-insensitive `doi:` prefix, must strip that prefix **before** the URL handling (`"doi:https://doi.org/10.x/y"` occurs in the wild), and must re-strip whitespace after removing it — a pasted `"doi: 10.1234/x"` normalizes rather than being reported as an unknown identifier. A second copy that misses any of these lands one paper under several cache keys, some of which build malformed upstream URLs.

**Never add a local copy.** Per-provider *policy* — which prefix a URL path needs, whether an ID is an Anthology ID — stays in the provider; only the normalization is shared.

## _useragent.py

The single home for the outbound `User-Agent`: `build(mailto)`, `headers(mailto)`, and `package_version()`. **Every client passes headers** — the seven providers and `oa_download`.

The shape (`name/version (+url; mailto:...)`) is what Wikimedia's User-Agent policy, Crossref's polite pool, and OpenAlex's polite pool all ask for. Going out as the default `python-httpx/x.y` is the agent several upstreams throttle hardest, and it leaves an operator no way to reach us. The version comes from installed distribution metadata — never a hardcoded literal.

The descriptive agent is returned whether or not a contact address is configured — anonymous-but-identifiable still beats `python-httpx`.

## _textnorm.py

Unicode folding for diacritic-insensitive search, used by `papers.find_in_markdown`, `cache_search`, and `bibtex` key generation.

- `fold(text)` — NFKD-decompose and drop combining marks, for tokenisation and key generation where character positions don't matter.
- `fold_with_map(text)` / `lower_with_map(text, *, fold=False)` — the transformed string **plus an index map back to original offsets**, so a match found in transformed text can be sliced out of the original.

**The offset map is the whole point, and it is not optional even without folding.** Neither transform is length-preserving: a ligature folds 1→N (`ﬁ` → `fi`), and `str.lower()` does too (U+0130 `İ` → two chars). Lowercasing a transformed string after the fact desynchronises its offsets, which is why the transform runs per *original* character and attributes every produced char to exactly one original index. A consumer that lowercases by hand instead will drift its snippet windows and section attribution off the real match.

## config.py

Environment configuration. `get(key)` is the only accessor — every module reads config through it (`CACHE_DIR`, `MAX_PDF_BYTES`, `PDF_CONVERTER`, `ENABLE_DEBUG_TOOLS`, the per-provider `*_MAILTO` / `*_API_KEY`), never from tool parameters.

- **`.env` resolution is a four-candidate search**, first existing file wins: `ACADEMIC_TOOLS_ENV_FILE` (explicit override) → project root relative to this file (source checkout) → `$PWD/.env` → `$XDG_CONFIG_HOME` (or `~/.config`) `/academic-tools-mcp/.env`. A project-root-only rule points inside the virtualenv from `site-packages` and silently disables every env var for an installed wheel — the exact deployment `.env.example` tells operators to set `CACHE_DIR` for. An unreadable candidate (permissions, dangling symlink) is skipped rather than fatal.
- **Real environment variables always win** — `load_dotenv` runs without `override`, so an exported value takes effect regardless of any file.
- **`get` reads an empty string as unset** (`os.environ.get(key) or None`), so a present-but-blank `CROSSREF_MAILTO=` behaves exactly like omitting the line. Not cosmetic: it drops crossref out of the polite pool, taking `_MAX_CONCURRENT` from 3 to 1.
- **`ENV_FILE`** holds whichever candidate won, resolved once at import and exposed so an operator can see which file was used.
