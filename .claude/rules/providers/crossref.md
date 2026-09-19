---
paths:
  - "src/academic_tools_mcp/providers/crossref.py"
---

# crossref.py

**Gating runs one way: build public, widen on proof.** The rate constants and the
`_observe_pool` mechanism carry their own comments; what they don't say is that
**`_resolve_policy()` is a ceiling, not a rate in force** — `in_confirmed_polite_pool()`
reports the latter — and that starting polite on a config guess is unrecoverable,
since a `Semaphore` gains permits but cannot shed them.

**`CROSSREF_MAILTO` reaches `_build_headers` / `_build_params` live, but the
headers `clients.get_client` baked in at construction ignore it** — changing the
contact mid-process moves the params and not the headers.

**Editing `RETRACTION_UPDATE_TYPES` is a judgement about what leaves a paper
citable, not a vocabulary refresh.** An unknown type must not set the flag.

**Accepted limitation:** the search gate stamps its start *before* handing off to
the singles slot, so under mixed load two searches can land closer together than
the gap. Reserving the instant the way `Throttle.slot` does needs a second
`Throttle`, whose `pending` would then sum into this namespace's `in_flight` row.
