---
title: sparknet peer-node — MISSION
role: peer
template: true
updated: 2026-10-29
---

# sparknet peer-node — MISSION

The **working node** role of the Sparknet fabric.

Read `INTENT.md` for the design contract behind this mission. `AGENTS.md` is the
operating charter and wins if the two ever disagree.

## Scope

- **In scope**: everything needed for a DGX Spark or Jetson AGX Xavier node to run its own local AI
  and knowledge vault, hold a Master-anchored WireGuard overlay, verify
  Master-signed authorization, and apply Master-approved changes to its own
  protected files under Plan → Permission → Execute.
- **Out of scope**: the Master role (see `../master-node/`) — signing
  authorizations, revoking peers, cluster-wide containment, the registry
  service, and the Master-side nocturnal batch-lease scheduler. Cross-role
  contracts live in `../contracts/`, not here.
- **Out of scope**: adjacent domains. A page serving another vault goes there.

## Success

This vault should answer, fluently and with citations:

1. How does a peer get from a bare clone to a verified, anchored node?
2. What exactly may this node do without asking anyone, and what requires a
   Master signature?
3. When the guard refuses, why is it refusing, and what would legitimately
   change that?
4. What is the peer unable to do, by design?
5. Which behaviours are proven by tests, and which need a privileged target
   node to verify?

## Success for the role itself

A peer is successful when it is **useful while inert**. It should keep serving
its vault, its agents and its inference with no anchor at all, while being
incapable of changing its own protected files. The moment it can act on its own
authority, the design has failed.

## Constraints

- Content language, citation rules and domain conventions are stated in
  `AGENTS.md` — that file wins over this page.
- No secrets in the vault. Key material belongs in `.state/` at mode 0700.
- Live network and kernel behaviour is not verifiable on the development host;
  claims about it must be marked unverified rather than asserted.