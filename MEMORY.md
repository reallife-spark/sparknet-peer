# sparknet peer-node — Session Memory

> Standing state only. Read at session start; **replace, never append**.
> The plan and its cursor live in `PLAN.md` — this file mirrors it, it does not
> own it. Per-session detail belongs in `sessions/YYYY-MM-DD.md`.

Updated: 2026-10-10

## Standing State

- **Plan cursor**: `PLAN.md` `current: done` — the peer build-out is terminal.
  Steps 1–9 are complete (container, grants, bridge, Open WebUI surface,
  conversation passing, `MASTERSPIKE`, sharing regime, node AI stack); each
  carries a `DoD: confirm: user` leg for the live/target-node behaviour this
  host cannot exercise. New roadmap work gets a **new** step.
- **Vault**: this directory (`peer-node/`), categories: concepts, entities,
  skills, references, synthesis, journal.
- **Config**: `.env` (`VAULT_PATH`); staged writes: false.
- **Fabric root**: parent directory, holding `contracts/` and `shared/`.
  Override with `SPARKNET_FABRIC_ROOT` for a standalone deployment.
- **Blockers**: the Master role is specified but not built, so no peer can be
  granted `guard-apply` in practice. See `../master-node/plans/MASTER_BUILD_PLAN.md`.

## Key Standing Facts

- This role serves its own vault autonomously but never holds authority. It
  holds no Master signing key and cannot sign, revoke, or escalate.
- `../shared/sparknet-authz.py` is deliberately a single copy. Never duplicate
  it into this subvault; two copies can drift and stop validating each other.
- `scripts/sparknet-node-containment.sh` is THIS NODE's root-gated teardown.
  It is not the cluster KillSwitch, which is Master-side.
- No usable Master keypair exists. `manifests/keys/master-ed25519.pub.b64` is a
  bootstrap public key whose private half was discarded; the registry ships
  empty on purpose, so a fresh deployment is fail-closed.
- Live WireGuard, nftables, `chattr +i` and systemd behaviour are UNVERIFIED on
  the development host. Do not claim otherwise.