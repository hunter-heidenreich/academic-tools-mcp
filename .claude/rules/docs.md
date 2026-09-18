---
paths:
  - ".claude/rules/**/*.md"
  - "CLAUDE.md"
  - "README.md"
  - "CHANGELOG.md"
---

# Writing the docs

**The source comment is the home.** A `.claude/rules/` file states only what cannot
sit next to a line of code: cross-module contracts, upstream API facts, accepted
limitations, and prohibitions — in practice, four kinds of content:

1. **Rejected alternatives** — the refactor that looks right and isn't.
2. **Upstream facts** no code line carries.
3. **Rosters spanning modules** — every writer of a counter, every reader of a flag.
4. **Accepted limitations.**

A rules file loads *because* its source file was read, so restating that file's
docstrings buys nothing and costs context every session. **Anything a docstring
already says gets deleted from the rules file, not summarised there.**

Change a contract — a new gating model, a new dispatch path — and update the
matching doc in the same change, plus a `CHANGELOG.md` `[Unreleased]` bullet if
the change is user-facing.

## Four conventions that keep the docs from rotting

- **Cite symbols, never line numbers.** `manual.resolve_metadata_source()` stays
  correct across every edit; `manual.py:74` is wrong at the next import.
- **Don't transcribe constants.** Name the constant and explain the policy —
  `_BATCH_CHUNK_SIZE`, not `50`. A copied number goes stale silently while the
  prose around it stays true. This binds upstream rate and pricing tables too.
- **Don't transcribe history.** State the invariant the code holds *now* and what
  breaks if you violate it. `CHANGELOG.md` and `git log -S` are the homes for "why
  it changed". Test docstrings are exempt — a regression test's purpose *is* the
  regression.
- **Don't cite a rules file from source, tests, or another rules file.** A
  `.claude/rules/…` pointer is only followable when that file happens to be
  loaded, and it breaks whenever the layer is reorganised. Name the invariant
  precisely enough to grep for, or restate the one sentence that matters. **One
  exception**: where a *test* is the authority for a rule rather than evidence for
  it — `tests/test_layering.py`'s `_LAYERS` is the layer order, there is no other
  copy — name the file and the constant, never a node ID.

## Scope

**One file per topic, scoped by `paths:` to the code it governs, under 200 lines.**
A file that loads for a directory says only what binds every module in it; a
per-module fact goes in a per-module file.

Form: **a bold imperative claim, then at most one sentence of consequence.** Cut
the third sentence, the counter-argument, and the history.
