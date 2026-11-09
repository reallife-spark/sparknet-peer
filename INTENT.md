---
title: sparknet peer-node — INTENT
role: peer
template: true
intent: design-contract
updated: 2026-10-31
---

# sparknet peer-node — INTENT

How a peer is meant to work and what must remain true on every deployment.
`PLAN.md` is the work order; `MISSION.md` is why the role exists; `AGENTS.md` is
the operating charter.

## Why a peer is the user's node

The fabric separates **usefulness** from **authority**. A peer is where a person
works: chat, agentic planning and building, their own vault, their own KB. It
holds an identity key and verifies what the Master signed, but never the
operational key. That asymmetry is deliberate: a node that can verify but not
sign is useful while it is inert. The Master supplies authority and scoped
fabric knowledge; it does not run the peer's model, choose the user's work or
edit the user's vault.

## Invariants

1. **Local-first usefulness.** Chat, planning, building, vault reads and
   self-healing work with no Master. An unreachable Master removes authority and
   external knowledge, not the node's usefulness.
2. **No Master private key.** The operational key never exists here.
   `make keys-check` and the peer validator enforce the absence.
3. **Verify, then apply.** A protected-file change applies only after a
   Master-signed authorization verifies under the current registry and root.
4. **Plan → Permission → Execute.** A mutable action is planned, explicitly
   permitted, then executed. Permission is not advisory.
5. **Knowledge and authority stay separate.** Knowledge enters only through a
   channel in `../contracts/knowledge-flow.md`: the local vault/KB, an accepted
   RFI answer, a reconciled share, or the build-time context pack. Registry
   entries, approvals, telemetry and admin responses are never knowledge.
6. **Scope narrows.** A KB, a session grant and an RFI answer carry allow-lists.
   A page outside it is out of bounds even when it is in the vault.
7. **Every AI instance loads the same context.** `CONTEXT.md` is generated from
   the identity pages and the knowledge-flow map; OpenCode, Open WebUI,
   per-user models and the vLLM/MASTERSPIKE precontext load it. It is not a
   second charter and is never hand-edited.
8. **Self-healing is conservative.** Safe, derived repairs may run
   automatically. User content, signatures, the ledger, protected files and the
   registry are reported, never forcibly repaired.
9. **A per-user vault is knowledge, not a node.** It has no key, no signer and
   no authority.
10. **One contract, one shared implementation.** `../contracts/` and
    `../shared/` are read, never copied.

## First deployment

1. `make env-init` writes `.env` and the numbered vault skeleton.
2. `make user-init` creates the user key and fills `00_System/user.md` once.
3. `make keys` generates WireGuard and Ed25519 material into the state dir.
4. `make validate` and `make wiki-preflight` prove the role and skill resolution;
   `make guard-status` shows the expected empty-registry, inert-guard state.

An empty registry on a fresh node is correct: nobody holds privileges until the
Master signs a grant.

## AI operating model

- Read `CONTEXT.md`, `AGENTS.md`, `MISSION.md`, `INTENT.md`, `MEMORY.md` and
  today's session file before acting; read `00_System/alpha-agent.md` for
  write, signing and secret discipline.
- Prefer the local vault and the user's KB. If fabric knowledge is missing, ask
  the Master through the RFI request path with an explicit scope.
- Vault writes are signed; never write an unsigned page into a signed category.
- Run `make vault-health` before a large vault operation and `make vault-heal`
  after one; never force a signature or rewrite history.
- Never claim live WireGuard, iptables, `chattr +i`, guard, KillSwitch or systemd.
- Route offensive-security work to `red`; this role is not an engagement surface.

## Blank-slate rule

A production peer starts with empty categories, its identity pages, the
governance set and its own role code. It does not inherit another node's
sessions, raw captures or live claims. Deployment state belongs in
`manifests/`, `MEMORY.md`, `log.md` and `02_Daily/`, not here.

## Change intent

New work gets a plan step, not a rewrite. Change `INTENT.md` only when the
design contract changes; a cross-role change is one commit. It is not a
runbook, deployment record or status page. If it contradicts `AGENTS.md` or
`../contracts/`, those win and this page is wrong.
