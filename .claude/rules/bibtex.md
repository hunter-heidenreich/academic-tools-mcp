---
paths:
  - "src/academic_tools_mcp/bibtex.py"
---

# BibTeX generation

**Every value reaching a field is escaped, including the ones that look numeric.**
Each escaper documents its own split; this is the module-wide rule none of them can
state, and Crossref's freeform `volume` / `issue` / `pages` are why it matters.

**An arXiv DOI is recognised through the provider's grammar, never by splitting on
`/`.** The id of an old-style work *contains* a slash
(`10.48550/arXiv.hep-th/9901001`), and a DOI merely containing "arxiv" in its
suffix is not an arXiv DOI.

**Never merge `_TYPE_MAP` with `_CROSSREF_TYPE_MAP`.** Their keys are two different
upstream vocabularies; anything unlisted falls through to `@misc`.

**Don't grow `_PARTICLES` to chase the long tail.** In real OpenAlex records a
capitalized `Du`, `Den`, `Bin`, `E.` or `I.` is a Chinese given name or an initial,
not a particle, and the lowercase spellings (`da Costa`, `ter Braak`) are already
handled by `_is_particle`'s case rule.

**An ACL key is generated, never the Anthology's hyphenated `bibkey`.**

**OpenAlex nulls are load-bearing** — it emits `"author": null` /
`"authorships": null` rather than dropping the key, so no `.get(k, default)` alone
is trusted. The same rule binds `tools/paper.py`, which reads the same objects.

**A Crossref record's field shapes are not uniform**, and each one's accessor is
`providers/crossref`'s: `title` and `container-title` are lists of *strings*,
`institution` a list of *objects*.

## Scope

**Cross-entry key disambiguation is out of scope.** These functions are stateless —
one paper per call — so two papers sharing author+year+title-word collide on one
key. A caller concatenating many entries into a single `.bib` must deduplicate
keys itself.
