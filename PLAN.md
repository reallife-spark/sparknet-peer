---
title: sparknet peer-node Plan
updated: 2026-10-04
current: 1
---

# sparknet peer-node — Active Plan

> Single source of truth for multi-step work. The `current:` cursor marks the
> step being worked. Write this file back **immediately** on every advance;
> append the matching transition line to `log.md`.

## Status Legend

- `[ ]` not started / in progress
- `[x]` complete — DoD verified
- **DoD** — the check that proves the step is done; `DoD: confirm: user` when a human must confirm
- `current: N` — step being worked now; `current: done` — terminal

## Step 1 — Verify the subvault after the split

- [ ] **Step 1** — Prove `peer-node/` is independently runnable after being
      split out of the parent vault.
      - **DoD**: (1) `make validate` and `make test` pass from inside
        `peer-node/` with the CWD elsewhere; (2) `make test-all` at the parent
        is green; (3) `make contract-test` is green; (4) a relocated copy of
        the parent at a different path also passes; (5) `git status` shows only
        intended changes.
      - **Status**: pending

## Next

Add roadmap steps here as work is identified. New roadmap work after
`current: done` becomes a new step, not a reuse of this plan.

Master-side work is tracked in `../master-node/plans/MASTER_BUILD_PLAN.md` —
do not duplicate it here.