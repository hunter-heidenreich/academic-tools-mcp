---
paths:
  - ".claude/rules/**/*.md"
  - "CLAUDE.md"
---

# Writing the docs

**The source comment is the home.** A `.claude/rules/` file states only what cannot
sit next to a line of code, in practice four kinds of content:

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

## Three conventions that keep the docs from rotting

- **Don't transcribe constants, or counts.** Name the constant and explain the
  policy — `_BATCH_CHUNK_SIZE`, not `50`; "the autouse fixtures", not "five autouse
  fixtures". A copied number goes stale silently while the prose around it stays
  true. This binds upstream rate and pricing tables too.
- **Don't transcribe history.** State the invariant the code holds *now* and what
  breaks if you violate it. `CHANGELOG.md` and `git log -S` are the homes for "why
  it changed".
- **Don't cite a rules file from source, tests, or another rules file.** A
  `.claude/rules/…` pointer is only followable when that file happens to be
  loaded, and it breaks whenever the layer is reorganised. Name the invariant
  precisely enough to grep for, or restate the one sentence that matters. **One
  exception**: where a *test* is the authority for a rule rather than evidence for
  it — `tests/test_layering.py`'s `_LAYERS` is the layer order, there is no other
  copy — name the file and the constant, never a node ID. This file is the second
  exception, for the superset rule below, which cannot be stated without naming the
  two supersets. Both exceptions are named allowlists in `tests/test_rules_layer.py`.

## Scope

**One file per topic, scoped by `paths:` to the code it governs, under 200 lines.**
A file that loads for a directory says only what binds every module in it; a
per-module fact goes in a per-module file.

**Two files never co-load unless one's `paths:` is a superset of the other's.**
`python-design.md` covers every source file and `providers/common.md` every
provider, so a fact stated there is never repeated below it. Every other pair is
disjoint — a fact needed at two such points is *not* a duplication, and the file
whose code owns the invariant keeps the reasoning while the other keeps the bold
claim alone.

`README.md` and `CHANGELOG.md` are operator-facing and deliberately outside this
file's scope — a concrete number is the point there, which is why the rules layer
delegates its constant tables to `README.md` rather than copying them.

Form: **a bold imperative claim, then at most one sentence of consequence.** Cut
the third sentence, the counter-argument, and the history.
