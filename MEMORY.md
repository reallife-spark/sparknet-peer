# sparknet peer-node — Session Memory

> Standing state only. Read at session start; **replace, never append**.
> The plan and its cursor live in `PLAN.md` — this file mirrors it, it does not
> own it. Per-session detail belongs in `sessions/YYYY-MM-DD.md`.

Updated: 2026-10-11

## Standing State

- **Plan cursor**: `PLAN.md` `current: done` — all nine build steps plus Step 10
  are complete and **deployed live** on cluster2 (node131 = Master-signer@192.168.178.131,
  node133 = peer@192.168.178.133, each running Open WebUI on `0.0.0.0:8181`,
  the dispatch bridge on `127.0.0.1:4096`, Ollama, ChromaDB on 18000, the
  `SPARKNET-FW` host firewall, and `sparknet-masterspike`). The operator
  visually confirmed the live-UI legs 2026-10-11 (approve/deny card rendering,
  the per-session `vault-access` toggle); only a full node reboot is left
  unexercised (docker-restart survival is proven). Agent execution inside a
  running opencode process is still a later fabric step (the bridge
  records+routes by design).
- **Vault**: this directory (`peer-node/`), numbered PARA layout; categories:
  concepts, entities, skills, references, synthesis, journal (legacy names are
  symlinks).
- **Config**: `.env` (`VAULT_PATH`); staged writes: false.
- **Fabric root**: parent directory, holding `contracts/` and `shared/`.
  Override with `SPARKNET_FABRIC_ROOT` for a standalone deployment.
- **Blockers**: none currently active. Legacy facts updated 2026-10-11 — the
  Master role IS now built and its signer IS elected/proven live on node131
  (see `../master-node/plans/MASTER_BUILD_PLAN.md`); the old "Master unused /
  no usable keypair" facts below no longer hold and were removed.

## Key Standing Facts

- This role serves its own vault autonomously but never holds authority. It
  holds no Master signing key and cannot sign, revoke, or escalate.
- `../shared/sparknet-authz.py` is deliberately a single copy. Never duplicate
  it into this subvault; two copies can drift and stop validating each other.
- `scripts/sparknet-node-containment.sh` is THIS NODE's root-gated teardown.
  It is not the cluster KillSwitch, which is Master-side.
- The live nodes run **Python 3.8.10** (Ubuntu 20.04): bridge/shared runtime
  modules carry `from __future__ import annotations` + a `collections.abc`
  `Callable` fallback. Keep future PEP 585/604 usage out of anything deployed,
  or gate it the same way.
- Every AI-stack surface binds `0.0.0.0` **by operator decision** and is held
  off the public internet by `SPARKNET-FW` (custom `SPARKNET-FW-IN`/`SPARKNET-FW-FWD`
  chains, ports 8181/11434/18000, allowed loopback+LAN+overlay+docker+link-local).
  The published ports are reachable on LAN and the WireGuard overlay, dropped on
  public v6 (and v4 non-LAN). If asked "why 0.0.0.0", the answer is the firewall.
- Live WireGuard overlay (`10.100.0.0/24`), the firewall chains and systemd
  behaviour ARE now verified on the target nodes — unlike the development host,
  which still has no nftables/WireGuard and must not over-claim.
- Hopskotch: node131 mints cluster2 grants (e.g. `webui-node131-20261011`,
  `webui-node133-20261011`) with the cluster2 operational key
  (`$SPARKNET_STATE_DIR/keys/operational.ed25519`), bearer tokens 0600 at
  `$SPARKNET_STATE_DIR/webui-bridge-grant.token` on each node. Grant scope
  excludes `00_System` wholesale (user.md / secrets); structure RAG corpus
  similarly never includes `00_System`, `02_Daily` or `03_Sources` content.