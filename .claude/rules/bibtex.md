---
paths:
  - "src/academic_tools_mcp/bibtex.py"
---

# BibTeX generation

**Each helper's own docstring covers what it does.** This file covers the
output-correctness contracts that span them — the rules that keep a generated
entry compiling.

Three entry points, one per provider shape (`generate_bibtex` /
`generate_arxiv_bibtex` / `generate_biorxiv_bibtex`); entry-type selection per
source is in `get_paper_bibtex`'s docstring. Author formatting is parameterised
by a `name_of` accessor, so OpenAlex's nested `author.display_name` and
arXiv/bioRxiv's flat `name` reuse one code path.

## Escaping and keys

- **Citation keys are ASCII `[a-z0-9]`.** `_key_token` gates the word components
  and `_key_year` gates the year — digits or nothing, so a null or malformed
  upstream year drops out instead of printing `None` into the key. **A new key
  component routes through one of the two.**
- **Every value reaching a field is escaped, including the ones that look
  numeric**: `biblio`'s volume / issue / pages arrive from Crossref as freeform
  strings and occasionally as numbers.
- **Three escapers, and the split is deliberate.** `_escape_bibtex` treats field
  text as literal (braces stripped, whitespace runs collapsed — an Atom-wrapped
  `journal_ref` would otherwise split the one-field-per-line layout).
  `_escape_doi` escapes braces rather than stripping them, because a DOI must
  stay resolvable; it also guards the identifier-shaped `eprint` and
  `primaryclass`. A URL inside `\url{}` takes neither — url.sty gives it verbatim
  catcodes, so `_url_field` percent-encodes the fatal characters instead, since a
  backslash escape would land in the link target.
- **Escaping is single-pass, never chained `str.replace`**: escaping `\` first
  would emit braces a later brace-pass re-escapes.
- **Titles are double-braced**, so no `.bst` can case-fold `NaCl` to `nacl`.

## Vocabularies

- **Invariant: `_TYPE_MAP`'s keys are OpenAlex's `type` vocabulary, not
  Crossref's.** A conference paper is `conference-paper`; `proceedings-article`,
  `posted-content` and `monograph` are Crossref spellings and must not be added
  as keys. Re-derive the list from `api.openalex.org/works?group_by=type` when
  adding a type, and let anything unlisted fall through to `@misc`. The
  preprint-only `eprint` / `howpublished` block keys on the *work type*, not on
  `@misc` — datasets and software land in `@misc` too.
- **Surname particles have two detectors, and the split is deliberate.**
  `_PARTICLES` holds only the particles publishers *capitalize*; `_is_particle`'s
  case rule — BibTeX's own "a lowercase word before the last one is the von part"
  — covers the rest, gated on the surname being capitalized so an all-lowercase
  display name doesn't collapse into one particle run. **Don't grow the wordlist
  to chase the long tail**: in real OpenAlex records a capitalized `Du`, `Den`,
  `Bin`, `E.` or `I.` is a Chinese given name or an initial, not a particle, and
  the lowercase spellings (`da Costa`, `ter Braak`) are already handled.
- **`_TITLE_SKIP` is closed-class only** — English plus the articles,
  prepositions and conjunctions of the major publication languages, because
  OpenAlex carries the original-language title.

## Provider quirks

- **An arXiv DOI is recognised through the provider's grammar**
  (`_arxiv_eprint_from_doi` → `arxiv.is_arxiv_id` / `normalize_arxiv_id`), never
  by splitting on `/`: the id of an old-style work *contains* a slash
  (`10.48550/arXiv.hep-th/9901001`), and a DOI merely containing "arxiv" in its
  suffix is not an arXiv DOI. Any other preprint gets a `howpublished` URL built
  from the **normalized** DOI, falling back to the OpenAlex landing page, and
  omitted when there is neither — never a bare `\url{}`.
- **OpenAlex nulls are load-bearing.** It emits `"author": null` /
  `"display_name": null` / `"authorships": null` rather than dropping the key, so
  every read is `or`-defaulted and no `.get(k, default)` alone is trusted. The
  same rule binds `tools/paper.py`, which reads the same objects
  (`.claude/rules/server.md`).
- **Organisational authors are brace-wrapped** so BibTeX treats them atomically
  instead of splitting off a fake surname.

## Scope

**Cross-entry key disambiguation is out of scope.** These functions are stateless
— one paper per call — and cannot see sibling entries, so two papers sharing
author+year+title-word collide on one key. A caller concatenating many entries
into a single `.bib` must deduplicate keys itself.
