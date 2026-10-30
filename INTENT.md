---
title: sparknet peer-node — INTENT
role: peer
template: true
intent: design-contract
updated: 2026-10-29
---

# sparknet peer-node — INTENT

This page states how a peer is meant to work and what must remain true on every
deployment. It is not a plan, a runbook or a status page. `PLAN.md` is the work
order; `MISSION.md` is why the role exists; `AGENTS.md` is the operating charter.

## Why a peer verifies instead of authorizes

A peer is autonomous for reading, inference and its own vault. It is subordinate
for authority. It holds its identity key and can verify a Master-signed
authorization, but it never holds the operational key and therefore cannot grant,
revoke or escalate.

That is deliberate. A node that can authorise itself has no authority model; it
only has a policy. A node that can verify but not sign stays useful while it is
inert.

## Invariants

1. **No Master private key.** The operational key never exists in this vault or
   on this node. `make keys-check` and the peer validator enforce the absence.
2. **Verify, then apply.** A protected-file change is applied only after a
   Master-signed authorization verifies under the current registry and trust
   root.
3. **Plan → Permission → Execute.** A mutable action is planned, explicitly
   permitted, then executed. The permission step is not advisory.
4. **With no anchor, the node is inert.** The vault, local inference and local
   agents keep working; the guard refuses to bind and refuses to apply. Losing
   the Master loses authority, not usefulness.
5. **Egress is restricted to the Master.** The peer reaches the overlay Master,
   not arbitrary peers or the WAN. The rule is part of the role, not an
   operator preference.
6. **Protected files are structurally protected.** `chattr +i`, the host
   firewall and the missing signature are the controls; a cooperative agent is
   not.
7. **Vault writes carry provenance.** Distilled notes are signed; approvals live
   in the hash-chained ledger; unsigned intake stays visibly pending.
8. **One contract, one shared implementation.** `../contracts/` and
   `../shared/` are read, never copied into this role.

## First deployment

A fresh peer is expected to run from its first boot:

1. `make env-init` writes the role `.env` and numbered vault skeleton.
2. `make user-init` creates the user key and fills `00_System/user.md` once.
3. `make keys` generates the node's WireGuard and Ed25519 material into the
   state directory, never into the vault.
4. `make validate` proves the required manifests, services, scripts and
   contracts are present.
5. `make wiki-preflight` proves the vendored wiki skills resolve this vault.
6. `make guard-status` reports the expected fail-closed state: an empty
   registry, no authorization to apply, and a guard that is inert until anchored.

An empty registry on a fresh node is correct. It means no node holds privileges
until the Master signs a grant.

## AI operating model

- Read `AGENTS.md`, `MISSION.md`, `INTENT.md`, `MEMORY.md` and today's session
  file before acting.
- Read `00_System/alpha-agent.md` for the write, signing and secret-handling
  discipline.
- Vault writes are signed; never write an unsigned page into a signed category.
- Never claim live WireGuard, iptables, `chattr +i`, guard, KillSwitch or systemd
  verification from a development host. Those are target-node checks.
- Route offensive-security work to the `red` role; this role is not an
  engagement surface.

## Blank-slate rule

A production peer starts with empty vault categories, its identity pages, the
governance set and its own role code. It does not inherit another node's sessions,
raw captures or live claims. Deployment-specific state belongs in `manifests/`,
`MEMORY.md`, `log.md` and `02_Daily/`, not in `MISSION.md` or `INTENT.md`.

## What this page is not

It is not a runbook, not a deployment record and not a status page. If a
statement here contradicts `AGENTS.md` or `../contracts/`, those win and this page
is wrong.
