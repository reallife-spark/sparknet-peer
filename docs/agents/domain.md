# Domain docs

The peer workspace uses the **single-context** layout.

## What to read

1. `AGENTS.md` — the peer charter; it wins on conflict.
2. `PLAN.md` — the active peer work order.
3. `GLOSSARY.md` — peer and vault vocabulary.
4. `docs/adr/` — accepted peer decisions.
5. `../contracts/` — the normative cross-role contracts.
6. `../contracts/knowledge-flow.md` — what may reach a user's AI and what may
   never do so.

## Rules

- A user's knowledge lives in the role vault or their `peer-<id>` vault/KB, not
  in `docs/` or an issue.
- Do not copy a contract into a peer document; link to it.
- Personal pages, keys, tokens and live host state never enter `docs/agents/`
  or `docs/adr/`.
- A plan step is the work order; an ADR is the durable decision. Do not use one
  as the other.
