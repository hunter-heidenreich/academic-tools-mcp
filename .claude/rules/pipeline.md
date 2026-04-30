---
paths:
  - "src/academic_tools_mcp/papers.py"
  - "src/academic_tools_mcp/manual.py"
  - "src/academic_tools_mcp/cache_search.py"
---

# PDF + content pipeline

## papers.py

Converter-agnostic PDF-to-markdown pipeline and section-level access.

- `_build_converter_command()` reads `PDF_CONVERTER` (named backend or custom command template) and `PDF_CONVERTER_VENV` (optional venv to activate) from env. Built-in backends: `mineru` (default), `marker`.
- `_resolve_convert_timeout()` reads `PDF_CONVERT_TIMEOUT` (default 1800s = 30 min; `none`/`off`/`disabled`/`0` disables; garbage falls back to default).
- `convert_pdf()` shells out under a **global single-conversion lock** (`_global_convert_lock`) — at most one PDF→markdown subprocess across the whole server. A second concurrent caller gets `{busy: True, retryable: True, in_progress: {namespace, canonical, elapsed_seconds}, pdf_size_mb}` immediately rather than queueing. Spawned with `start_new_session=True` so a timeout can `os.killpg(SIGKILL)` the whole process tree (the converter, not just the bash wrapper). Stores markdown under `.cache/<namespace>/markdown/`.
- Already-converted papers' cached-path early-return is NOT subject to the global lock — agents can keep reading sections of converted papers while a different one is converting.
- `parse_sections()` splits by H2 headings with H3 previews (adaptive — detects H1 vs H2 documents). `get_section_content()` retrieves individual sections by index or title substring. Section indices cached under `.cache/<namespace>/sections/`.
- **Per-paper sections lock** (`_section_locks` keyed by `(namespace, canonical)`) serialises concurrent re-parse attempts on the same paper. The lock dict is an `OrderedDict` capped at `_SECTION_LOCKS_MAX=1024` with FIFO eviction; currently-held locks are skipped on eviction so mutual exclusion can't be silently dropped out from under a writer.

## manual.py

Manual PDF/markdown import for local files, plus the two identifier dispatchers.

- **Provider-aware routing for PDF storage** — `_resolve_target()` detects identifier type (arXiv ID, bioRxiv DOI, ACL DOI) and stores PDFs/markdown directly in that provider's cache namespace, so native pipeline tools find them with no duplicates. Unrecognised identifiers fall back to the `manual` namespace.
- **Metadata dispatch** — `_resolve_metadata_source()` returns `"arxiv" | "biorxiv" | "openalex" | None`. ACL DOIs and generic DOIs route to OpenAlex (ACL has no metadata API); unknown identifiers return `None` so tools can surface a clear error.
- Supports `~/` expansion for local paths.
- Module deliberately does **not** download arbitrary URLs — agents fetch non-native PDFs themselves and hand the local file to `import_paper`.
- No API, no auth, no rate limits.

Manual imports intentionally have no BibTeX generation — the manual pipeline has no structured metadata. When the identifier is a DOI, chain into `get_paper_bibtex` (which dispatches to OpenAlex for arbitrary DOIs).

## cache_search.py

BM25 keyword search across every cached markdown file (`.cache/<namespace>/markdown/*.md`). Standard Robertson BM25 (k1=1.5, b=0.75); tokeniser preserves intra-word hyphens / dots so `self-attention` and `BM25` survive intact, drops a tiny stopword list (the classic 50 minus content-bearing terms like "all", "no", "not", "very" that matter in academic prose).

For each top hit, returns the document title (first H1/H2), a ~200-char snippet centred on the position with the most distinct query terms cooccurring nearby, and the H2 section the snippet falls under so an agent can chain into `get_paper_section(canonical_id, section)`.

Filename → canonical-ID inversion is per-namespace (`10.1101_X` → `10.1101/X`, `10.18653_v1_X` → `10.18653/v1/X`, `hep-th_9901001` → `hep-th/9901001`); arXiv new-style IDs and manual identifiers pass through unchanged.

No persistent index — the corpus is small enough (tens to hundreds of papers for a personal MCP) that a fresh scan + tokenise + score on every call runs in well under 100ms. Wrapped in `asyncio.to_thread` at the tool layer so a large-corpus search doesn't pin the event loop on concurrent HTTP fetches. Pure keyword recall — won't surface "scaled dot-product attention" for a "self-attention" query — natural follow-up if recall ever bites is sentence-transformer embedding rerank.
