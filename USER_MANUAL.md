---
title: sparknet-peer — user manual
updated: 2026-10-30
---

# sparknet-peer — user manual

> The manual for the **peer** role: a self-sufficient sparknet node that
> verifies the Master's decisions, serves its own knowledge vault and runs its
> own local inference — and can never authorise anything for itself.
>
> This repository is the `peer-node/` subtree of the fabric, published
> standalone. It deliberately does **not** contain `contracts/` or `shared/`;
> see [§2](#2-what-is-and-is-not-here).
>
> The **master manual** for the whole system lives in the `sparknet`
> repository as `USER_MANUAL.md`.

---

## Table of contents

1. [What a peer is](#1-what-a-peer-is)
2. [What is and is not here](#2-what-is-and-is-not-here)
3. [Orientation](#3-orientation)
4. [The services](#4-the-services)
5. [The scripts](#5-the-scripts)
6. [The units](#6-the-units)
7. [The manifests](#7-the-manifests)
8. [The vault](#8-the-vault)
9. [Build, run, test](#9-build-run-test)
10. [The gate](#10-the-gate)
11. [How to make a change correctly](#11-how-to-make-a-change-correctly)
12. [Configuration reference](#12-configuration-reference)
13. [Troubleshooting](#13-troubleshooting)
14. [Spec vs implemented](#14-spec-vs-implemented)
15. [Glossary](#15-glossary)

---

## 1. What a peer is

A peer is a sparknet node that:

- **serves its own knowledge vault** and its own local inference, and is
  useful with no Master at all;
- **verifies** Master-signed authorisations, and applies them to protected
  files — but only within a capability it holds;
- **never signs**. A peer holds no operational key, cannot sign a registry
  entry, cannot grant a capability, cannot revoke anything, and cannot escalate.

The asymmetry is the whole design. Authority in the fabric comes from **key
possession**, not from a capability grant: a peer that somehow obtained the
entire capability vocabulary still could not produce a signature, so it still
could not grant, revoke or escalate anything. Capabilities say *what* a node
may do; the key says *whether anyone believes it.

What a peer **can** do that a Master cannot do for it: run inference, hold and
serve its own knowledge, answer questions about its own vault, and keep
working when the fabric is partitioned.

> **Authority comes from key possession, not from a capability grant.**

**Who this manual is for:** a developer changing the peer role — the services,
the scripts, the units, the manifests or the vault layout — who needs to know
which contract the change must not violate and which gate will catch it.

---

## 2. What is and is not here

The peer role lives in two places, and the split matters:

- The **fabric** repository holds the contract (`contracts/`) and the one
  shared implementation (`shared/`). Both are loaded **by path** — the peer
  never copies them, because two copies of a signature-verifying service drift
  silently and then stop validating each other.
- This repository holds **only the peer role**: its services, scripts, units,
  manifests, tests and its own vault.

So a checkout of this repository alone **cannot verify a signature**. It
fails, loudly, rather than pretending to work. That is deliberate: it is the
reason `make bundle` exists, and the reason the node setup script stages a
bundle rather than pointing at a git clone.

| You need | You get it from |
|---|---|
| The capability vocabulary, overlay, ports, identity schema, protocols | the fabric's `contracts/` |
| The signed canonical form, election, consensus, provenance, proposal code | the fabric's `shared/` |
| The peer's own services, scripts, units, manifests, vault | **this repository** |
| A deployable node | a bundle, or the fabric's setup script |

**Cross-repository references in this manual are written as prose, not links.**
A relative link to `../contracts/capabilities.json` resolves in the fabric
checkout and is broken here, because this published repository does not contain
`contracts/`. Every link in this manual resolves inside this repository.

---

## 3. Orientation

Read [AGENTS.md](AGENTS.md) first: it is the role's charter and states the
layout, the memory and plan protocols, and the boundaries. `CONTEXT.md` is the
generated operating context; `contracts/knowledge-flow.md` is the channel map;
`docs/self-healing.md` is the safe-repair loop. This manual explains the code.

| Path | What it is |
|---|---|
| `services/` | The peer services, including the RFI request client. Each is a single-file, `#!/usr/bin/env python3` program with a `--help`. |
| `scripts/` | The 23 operational scripts: identity, keys, overlay, firewall, lockdown, sync, provisioning, the run helper. |
| `systemd/` | Twelve services and four timers. A unit contains **no vault path** — it calls a generated shim. |
| `manifests/` | The node's declared identity, capabilities, protected paths, policies and templates. |
| `configs/` | Templates: `wireguard/`, `network/`, `nftables/`, `container/`, `ai-stack/`. |
| `00_System/` … `07_Archives/` | The vault, in the numbered PARA layout. |
| `concepts/`, `entities/`, `skills/`, `synthesis/`, `projects/`, `journal/`, `references/` | Legacy spellings — **symlinks** into the numbered layout. |
| `tests/` | The peer's own pytest suite. |
| `Makefile` | The role's entire command surface. Every gate runs through it. |
| `pyproject.toml`, `requirements.txt` | The role's Python contract. |
| `blockchain/`, `security/`, `deployment/`, `docs/` | Role-local material: the ledger chain, policies, deployment notes, documentation. |
| `agents/` | The role's vendored agent definitions. |
| `skills/` | Legacy symlink into `05_Knowledge/skills/`. |

### 3.1 The numbered layout and the shims

The vault uses a numbered PARA layout, and **every legacy name stays a symlink
to its numbered home**:

| Legacy | Numbered home |
|---|---|
| `concepts/` | `05_Knowledge/concepts/` |
| `entities/` | `05_Knowledge/entities/` |
| `skills/` | `05_Knowledge/skills/` |
| `synthesis/` | `05_Knowledge/synthesis/` |
| `projects/` | `06_Projects/` |
| `journal/` | `02_Daily/` |
| `references/` | `03_Sources/` |
| `_raw/` | `01_Inbox/` |
| `_staging/` | `01_Inbox/` (staged writes) |
| `_archives/` | `07_Archives/` |
| `blockchain/` | `00_System/blockchain/` |
| `security/` | `00_System/security/` |

Both spellings therefore name **one file**. That is the point: a link written
before the reorganisation keeps resolving after it. `make shims-check` proves
every legacy name is still a symlink, and `tests/test_shims.py` enforces it.

---

## 4. The services

Eleven services. Each is a standalone program with a `--help`, and each is
wired into a unit.

### `sparknet-guard-d.py` — the guard daemon

The peer's enforcement point. It:

1. verifies Master-signed approvals against the pinned Master public key;
2. checks the guard's own preconditions — the `guard-apply` capability, an
   effective uid of 0, a protected target path, and `--apply-enabled` (off by
   default);
3. applies the change to a protected path: lift `chattr +i`, write, re-pin;
4. records every step in the hash-chained ledger.

The distinction that matters: **verification is always on; application is
off by default.** A peer that cannot apply is still useful — it verifies,
audits and refuses, which is the correct behaviour for a node that has lost
its Master's authority.

```sh
python3 services/sparknet-guard-d.py --help
make guard-status      # the manifest state and the authorization posture
make guard-contract    # prove this role matches the contract
```

### `sparknet-wireguard-anchor.sh` — the overlay anchor

Owns the `wg0` interface. It renders the peer configuration from the
template, brings the interface up, and pins it with `chattr +i` so nothing on
the host can quietly rewrite it. On hosts whose kernel has no WireGuard module
(`CONFIG_WIREGUARD=n` on the tegra machines) the bring-up goes through the
peer's `scripts/sparknet-wg-bringup.sh`, which uses a userspace `wireguard-go`
implementation and the same pinned configuration.

```sh
make anchor            # render + wg-quick up + pin (root)
make wg-render         # dry-run the rendering; APPLY=1 writes at 0600
```

### `sparknet-dispatch-bridge.py` — the dispatch bridge

Turns the cluster's job bus into this node's work. It accepts a signed
cluster task, splits it into signed subtasks, and fans them out to the node's
local engine. It is a **server**: it binds and serves, it does not poll.

### `sparknet-share-regime.py` — the sharing regime

Implements the peer↔peer sharing regime: offers, share types, the four clocks
(trust, freshness, quota, scope), trust levels, isolation between shares, and
the evidence trail. It is *clocked*: every decision is a function of the
current time against the share's declared windows, so a share expires on its
own.

### `sparknet-proposal-exchange.py` — the proposal exchange

Carries the cross-user proposal workflow over the share ports: a proposal
moves through draft → submitted → reviewed → decided, and the transitions
**fail closed** — an unverifiable chain is refused rather than repaired.

### `sparknet-registry-client.py` — the registry client

Installs a Master-signed registry **publication** after verifying it against the
key pinned in `manifests/keys/master-ed25519.pub.b64`, and reports the installed
sequence and this node's capabilities. It verifies; it never authorises.

The live channel is a push: the Master posts the publication to this node's
guard on 9090 and the guard installs it (see `docs/registry-protocol.md` in the
master role). The client is the **offline** path — a node whose guard is not
running, a recovery install, or an operator previewing what arrived:

```sh
make registry-fetch PUBLICATION=/path/to/current.json        # install
make registry-fetch PUBLICATION=/path/to/current.json DRY_RUN=1
make registry-status                                          # sequence + capabilities
```

The same acceptance rule runs on both paths
(`shared/sparknet-registry-install.py`): exact envelope, signature under the
pinned Master key, a registry the contract accepts, and a sequence that is not
lower than the installed one. A replayed *older* publication is refused as
`stale_publication` — that is what stops a revocation from being undone. Re-
installing the exact publication already installed (equal sequence and digest)
answers `REGISTRY_ALREADY_INSTALLED` and writes nothing, so the Master's timed
re-delivery is a no-op rather than an error.

### `sparknet-rfi-client.py` — the knowledge request client

Asks the Master for missing fabric knowledge with an explicit scope. It builds
the contract's overlay-attributed request, posts it to the Master's `8090`
intake, and records the request-id under `.state/rfi-requests/` at 0600. The
bridge exposes it as `POST /vault/rfi` so a session can ask without leaving the
grant path. It never reads the local vault and never grants anything. An empty
answer is a legitimate answer; the peer keeps serving its own knowledge.

### `sparknet-vault-provisioner.py` — the vault provisioner

Materialises the knowledge-vault slice this node is entitled to and wires the
knowledge base around it. It runs after the guard has established which paths
are protected.

### `sparknet-telemetry-push.py` — the telemetry pusher

Pushes the peer's status and metrics to the Master's 8090 peer intake as
**evidence, never authority**. It is deliberately **unsigned**: attribution is
the WireGuard-authenticated overlay, and the Master maps the source address to
a fingerprint through the signed registry. A peer without the `telemetry`
capability is silent — the Master refuses the report before reading the body.

```sh
python3 services/sparknet-telemetry-push.py --kind status --dry-run
python3 services/sparknet-telemetry-push.py --kind metrics
```

### `sparknet-liveness-monitor.py` — the liveness probe

The peer's presence evidence, on `4211`. It probes and claims; it never
authorises anything. A **dedicated probe port** is the point: the monitor must
never make the serving path do probe work, so a hung engine cannot make the
node look dead. Like telemetry, there is no signature — the Master derives the
same fingerprint from the overlay source, and the contract is
`health-protocol.md`.

### `sparknet-checkpoint-anchor.py` — the checkpoint anchor

Anchors the peer's **ledger head** and pushes the signed checkpoint as
evidence. It builds one checkpoint per unchanged head — naively rebuilding on
every tick would see `size + 1` each time and produce an endless chain that
says nothing — and the payload is the full signed block, not just its digest.
The Master may have missed a checkpoint; the ledger does not change because of
one.

### `sparknet-admin-agent.py` — the node admin agent

The peer's side of the admin surface: auth-event ingest, webhook
registration, and pending supervision. It is **pending** by design — the
supervision it waits for is granted by the Master through the
`admin-supervise` capability, one of the three sensitive capabilities in the
vocabulary.

---

## 5. The scripts

23 scripts, all `bash` with `set -euo pipefail`, all answering `--help` **and
changing nothing when they do** (enforced by `tests/test_shell.py`).

| Script | Purpose |
|---|---|
| `sparknet-env.sh` | The role's environment contract: every path resolves from the script's own location, so the role is relocatable. |
| `sparknet-run` | The run helper: a single entry point for the role's scripts and services. |
| `sparknet-keygen.sh` | Generate the WireGuard and Ed25519 keypairs into the state dir. Secrets never leave it. |
| `sparknet-identity-capture.sh` | Capture this node's identity into the manifests. |
| `sparknet-wg-render-peer.sh`, `sparknet-wireguard-render.sh` | Render a WireGuard configuration from the template. Dry-run by default; `APPLY=1` writes at `0600`. |
| `sparknet-wireguard-anchor.sh` | The anchor: render, bring up, pin. |
| `sparknet-wg-bringup.sh` | Userspace `wireguard-go` bring-up for hosts without the kernel module. |
| `sparknet-registry-fetch.sh` | Install a Master-signed registry publication (offline path); `--status` reports what is installed. |
| `sparknet-registry-verify-peer.sh` | Verify the registry against the pinned Master key. |
| `sparknet-chattr-preflight.sh` | Prove `chattr +i` holds on the state filesystem. |
| `sparknet-firewall.sh` | The peer's egress policy: the peer may reach **only** the Master. |
| `sparknet-wlan-guard.sh` | The wireless guard. |
| `sparknet-lockdown.sh` | Host lockdown. |
| `sparknet-node-containment.sh` | **This node's** root-gated teardown. Not the cluster KillSwitch — that is Master-side. |
| `sparknet-provision.sh` | The provisioning pass. |
| `sparknet-sync.sh` | Vault sync over the overlay. |
| `sparknet-install-config.sh` | Print the `/etc` diff. Never writes, by design. |
| `sparknet-install-units.sh` | Generate the shims and install the units. Dry-run report unless `APPLY=1`. |
| `sparknet-validate.sh` | Validate the role's manifests and wiring. |
| `sparknet-election-request.sh` | This node's part of a master election. |
| `sparknet-masterspike.py` | The MASTERSPIKE artefact generator: the `llm-wiki` format as an inference artefact. |
| `sparknet-webui-workspace.py` | Open WebUI workspace wiring. |

`make -C peer-node precommit` runs the keys check, a secret scan and a strict
provenance verification, so the common failure modes are caught before a
commit rather than by a reviewer.

---

## 6. The units

Twelve services and four timers in `systemd/`:

| Unit | Purpose |
|---|---|
| `sparknet-guard-daemon.service` | The guard. |
| `sparknet-wireguard-anchor.service` | The overlay anchor. |
| `sparknet-dispatch-bridge.service` | The dispatch bridge. |
| `sparknet-vault-provisioner.service` | The vault provisioner. |
| `sparknet-admin-agent.service` | The admin agent. |
| `sparknet-liveness-monitor.service` | The liveness probe on `4211`. |
| `sparknet-telemetry-push.service` + `.timer` | Telemetry evidence, on a timer. |
| `sparknet-checkpoint-anchor.service` + `.timer` | The ledger-head checkpoint, on a timer. |
| `sparknet-sync.service` + `.timer` | Vault sync, on a timer. |
| `sparknet-firewall.service` | The egress policy. |
| `sparknet-wifi-guard.service` | The wireless guard. |

Two rules the units obey:

- **A unit contains no vault path.** It calls a generated shim, so the vault
  can move without editing units.
- **Nothing inert.** A unit that no target enables is a file that looks like
  functionality and is not; `make install-units` reports it, and the role's
  suite fails on it.

`make install-units` is a **dry-run report** by default. Applying a malformed
systemd unit on a remote node can drop your access, so that stays a human
decision: pass `APPLY=1`.

---

## 7. The manifests

`manifests/` is what the peer *declares*. Templates only — never live key
material, and never a secret.

| File | Declares |
|---|---|
| `node-identity.json` | This node's identity and fingerprint. |
| `node-authorizations.json` | The registry: the Master-signed entries and their capabilities. The shipped file is **empty on purpose**. |
| `node-template-manifest.json` | The templates the node ships. |
| `critical-manifest.json` | The protected paths, with their provenance. |
| `security-policies.json` | The peer's security policies. |
| `user-env-manifest.json` | The environment contract: the key set every `.env` must expose. |
| `share-regime.json` | The sharing regime's configuration. |
| `ai-stack.json` | The node's declared AI stack. |
| `agent-models.json` | The declared models. |
| `masterspike.json`, `masterspike/` | The MASTERSPIKE artefacts and their manifest. |
| `vault-openapi.json` | The vault's API surface. |
| `keys/` | Key *templates*. Generated keys go to the state dir instead. |

Two declarations are checked rather than trusted:

- `test_node_container.py` — the node container is coherent **as declared**;
  `test_node_ai_stack.py` — the AI stack likewise.
- `test_manifests.py` — the critical-manifest provenance map: every protected
  path has a recorded provenance.

---

## 8. The vault

The peer is a **knowledge vault** in its own right, with its own `.env`, its
own category skeleton and its own `AGENTS.md`.

| Path | Holds |
|---|---|
| `00_System/` | `user.md`, `alpha-agent.md`, `schema.yaml`, `secrets-index.json`, `index.md`, `log.md` |
| `01_Inbox/` | unprocessed intake awaiting dispatch and signature |
| `02_Daily/` | journal entries and session memory (`YYYY-MM-DD.md`) |
| `03_Sources/` | external material and raw captures |
| `04_Evergreen/` | stable evergreen material |
| `05_Knowledge/` | `concepts/`, `entities/`, `skills/`, `synthesis/` |
| `06_Projects/` | per-project knowledge |
| `07_Archives/` | superseded material and vault snapshots |

**The skills resolve the vault from the nearest `.env`.** That is what keeps a
role subvault honest: a session started here resolves to *this* vault, not to
the parent. Run `make wiki-preflight` before using a skill; if it fails, run
`make env-init` once. Never work around the failure — a silent fallback
resolves a *different* vault, and every page lands there with no error at all.

### 8.1 Signing the vault

The peer's vault is signed. `make user-init` generates the key into the state
directory and fills `00_System/user.md` — **once**, because the key never
leaves the node.

```sh
make user-init             # generate the user key + fill 00_System/user.md
make ingest                # land STDIN (or SRC=) as an unsigned note in 01_Inbox/
make provenance-append QUESTION=... DECISION=... AGENT=... MODEL=...
make provenance-verify     # verify every note, the ledger and the approvals
STRICT=1 make provenance-verify
```

`make precommit` runs the keys check, the secret scan and
`provenance-verify --strict` — so a commit cannot land with an unsigned note
or a broken chain.

The provenance chain is **dual-signed** for an approval: the note's own
signature plus the approving authority's. A note without a valid chain is
evidence that something was written, not that it was approved.

---

## 9. Build, run, test

### 9.1 Setup

```sh
make devtools      # optional .venv: cryptography, pytest, ruff
make env-init      # write this vault's .env and the category skeleton (idempotent)
make keys          # generate the WireGuard and Ed25519 keypairs into the state dir
make user-init     # generate the user key and fill 00_System/user.md
make provision     # identity capture, then validate
```

Exactly one third-party package is needed at runtime: `cryptography>=42`,
because Ed25519 is not in the standard library. `pytest` and `ruff` are
optional.

### 9.2 The suite

```sh
make test          # this role's pytest suite
make validate      # the env contract + manifest checks
make lint          # ruff over the role
```

The suite is behaviour-first. Its files:

| Test | Covers |
|---|---|
| `test_authz.py` | The authorization layer: SHA-256 identity fingerprints verified against Ed25519. |
| `test_guard.py` | The guard daemon: verification, refusal, and arming. |
| `test_grants.py` | Signed session grants: mint, verify, scope, and grant-scoped vault writes. |
| `test_wireguard.py` | The WireGuard Master Anchor: template, rendering, key hygiene. |
| `test_bridge.py` | The dispatch bridge becomes a server. |
| `test_share_regime.py` | The clocked, contract-driven sharing regime. |
| `test_proposal_exchange.py` | The proposal exchange over the share ports. |
| `test_vault_provisioner.py` | The vault provisioner: knowledge-vault slice materialisation and KB wiring. |
| `test_vault_curator.py` | The vault curator: the deterministic CLI and the acceptance gate for untrusted ingress. |
| `test_reflex_surface.py` | The peer's reflex surface: a sensor, never authority. |
| `test_admin_agent.py` | The node admin agent: auth-event ingest, webhook registration, pending supervision. |
| `test_admin_supervise.py` | The signed admin surface. |
| `test_containment.py` | Relocation and write-safety regressions. |
| `test_conversation_passing.py` | Intra-node conversation passing, against a real server. |
| `test_firewall.py` | The host firewall is the rule the published-port decision needs. |
| `test_masterspike.py` | The `llm-wiki` format as an inference artefact. |
| `test_webui_surface.py`, `test_webui_workspace.py` | The Open WebUI surface and workspace wiring. |
| `test_env.py` | The env contract: paths resolve from the script's own location. |
| `test_scripts.py` | The viability checks, converted from manual marks into assertions. |
| `test_services.py` | Service-level behaviour and the zero-dependency guarantee. |
| `test_node_container.py`, `test_node_ai_stack.py` | The node container and AI stack, coherent as declared. |
| `test_shims.py` | Every legacy name is a symlink to its numbered home. |

The **zero-dependency guarantee** is worth stating: a peer's services run
without the fabric's `shared/` being importable in the ordinary sense —
the shared modules are loaded by path, and the services must not reach for
anything else.

### 9.3 The container

```sh
make container-config      # validate the compose parses; no images pulled
make container-preflight   # prove chattr +i holds on the .state filesystem
make container-build       # buildx, both architectures
make container-load        # build the host architecture with --load
make container-up          # preflight, then bring the node up
make container-health      # per-service health
make container-down
```

`container-preflight` exists because the guard's write-safety depends on
`chattr +i` actually holding. Proving that *before* anything relies on it is
cheaper than discovering it during a guarded write.

---

## 10. The gate

```sh
make keys-check      # no key material tracked; state-dir keys are 0600
make shims-check     # every legacy name is still a symlink
make provenance-verify
make precommit       # keys-check + secret scan + provenance-verify --strict
```

The role's own gate is the last three. The **fabric-level** gate is
authoritative for a merge into the fabric, and it runs this role's suite as
part of it. When you work against the published `sparknet-peer` repository,
run the same three targets there — the subtree is identical, so the gate
behaves the same.

The conventions the gate does not check for you:

- **One page per file**, in its category directory; provenance to
  `03_Sources/`. Never scatter copies.
- **A script that mutates state fails closed** and says what it refused.
- **No secret in any tracked file** — reference the secret *path* only. A
  leaked key is rotated, not rewritten silently.
- **A claim must be verifiable.** Say "implemented and unit-tested", never
  "verified in production" unless it was.
- **Conventional commits**; one logical change per commit.
- **`/etc` configuration is derived from the vault**, never the reverse. The
  install script prints the diff and cannot write it.

---

## 11. How to make a change correctly

### 11.1 Adding a service

1. Write it under `services/` as a single-file program with a `--help` that
   changes nothing.
2. Give it a unit under `systemd/` — no vault path in the unit; call a
   generated shim.
3. Enable the unit from the role's Makefile, or exclude it explicitly with a
   reason. A unit nobody enables is a file that looks like functionality and
   is not.
4. Extend the role's test suite. The peer suite is behaviour-first: assert the
   behaviour, not the file's existence.
5. If the change touches a capability, a port, the overlay or the identity
   schema, it is a **contract** change: land it in the fabric's `contracts/`
   and both roles **in the same commit**, then run the fabric's contract test.

### 11.2 Adding a script

- `bash`, `set -euo pipefail`, bash 3.2 compatible (the peer targets macOS
  and old images too).
- Quote every expansion. The shellcheck gate is strict about it.
- `--help` must work, print usage, and mutate nothing.
- Paths resolve from the script's own location — never from `$PWD`.

### 11.3 Adding a protected path

1. Add it to `manifests/critical-manifest.json` **with its provenance**.
2. Make sure the guard's preconditions still hold: `guard-apply` capability,
   effective uid 0, and `--apply-enabled`.
3. Extend `test_manifests.py` if the path needs a specific assertion.

### 11.4 Changing the vault layout

Never move a category without leaving a symlink. The rule is: **the legacy
spelling must keep resolving**. Add the new directory, move the content, and
replace the old path with a symlink — in that order, so there is never a
moment when the name resolves to nothing.

```sh
make shims-check   # proves every legacy name is still a symlink
```

### 11.5 Writing documentation

- One page per file. Frontmatter `title` + `updated`; provenance pages add
  `source`/`license`/`type`.
- Cite sources. A distilled page without provenance is a guess — mark it as
  one.
- Use `[[wikilink]]` inside the vault; use relative markdown links to files
  that exist in this repository.

---

## 12. Configuration reference

| Thing | Where | Notes |
|---|---|---|
| Vault env | `.env`, from `.env.example` | The template exposes a fixed key set; the suite asserts it. |
| Node identity | `manifests/node-identity.json` | A template. |
| Registry | `manifests/node-authorizations.json` | Entries signed per entry; the document itself is unsigned. Shipped **empty on purpose**. |
| Protected paths | `manifests/critical-manifest.json` | With provenance. |
| Security policies | `manifests/security-policies.json` | |
| Share regime | `manifests/share-regime.json` | |
| WireGuard templates | `configs/wireguard/` | Rendered at `0600`; `APPLY=1` to write, dry-run by default. |
| Host firewall | `scripts/sparknet-firewall.sh`, `systemd/sparknet-firewall.service` | iptables chains `SPARKNET-FW-IN`/`SPARKNET-FW-FWD`; protects 8181/11434/18000. `configs/nftables/` is a legacy template, not installed. |
| State dir | `.state/` | `0700`; the keys inside are `0600`. Gitignored. |
| Node compose env | `.state/spark.env` (or `spark-agx.env`) | Written by the setup script; gitignored. |

**Secrets** live under `.state/` and are referenced by path only. The
`secrets-index.json` in `00_System/` records *which* secret is stored where —
never the value.

**`/etc` configuration is derived from the vault, never the other way round.**
`make install-config` prints the diff and cannot write it: applying a malformed
netplan on a remote node can drop your SSH session, so that stays a human
decision.

---

## 13. Troubleshooting

| Symptom | Likely cause | Where to look |
|---|---|---|
| The guard verifies but refuses to apply | The peer lacks `guard-apply`, or `--apply-enabled` is off (off by default), or the target path is not protected. | `make guard-status`; the contract's guard protocol. |
| The guard will not bind at all | No overlay, or an empty registry. The shipped registry is empty on purpose — the guard refuses to bind without authority. | `make guard-status`; `make registry-fetch`. |
| `chattr +i` does not hold | The filesystem does not support the immutable bit. | `make container-preflight`. |
| The overlay does not come up after a reboot | The host kernel has no WireGuard module. | `scripts/sparknet-wg-bringup.sh` (userspace `wireguard-go`); the anchor unit. |
| `make shims-check` fails | A legacy category name is a real directory, not a symlink. | `make env-init` recreates the shims. |
| `make wiki-preflight` fails | The nearest `.env` is missing, so the skills would resolve a *different* vault. | `make env-init`. Never work around it. |
| `make keys-check` fails | Tracked key material, or a state-dir key with the wrong mode. | Rotate anything that was committed. |
| `make provenance-verify` fails | A note is unsigned, or the ledger chain is broken, or an approval lacks its dual signature. | The ledger under `00_System/blockchain/`; re-approve rather than re-sign. |
| A service exits immediately | It is missing the state dir, or a path does not resolve from the script's own location. | `make validate`; the unit's journal. |
| `make test` fails on a shell test | A script stopped answering `--help`, or started mutating state when it does. | `tests/test_shell.py`. |
| The container will not start | The image was built for the wrong platform, or the compose env file is the other platform's. | `make container-config`; the env file in `.state/`. |
| The firewall blocks the cluster | The peer's rule permits **only** the Master. A peer must not reach a sibling directly. | `scripts/sparknet-firewall.sh`; the peer's egress policy. |

---

## 14. Spec vs implemented

Broadly, in this role:

- **Implemented and exercised** — the guard, the overlay anchor (kernel or
  userspace `wireguard-go`), the dispatch bridge, the share regime, the
  proposal exchange, the registry client and the shared publication install
  rule, the vault provisioner, the vault curator, the admin agent's
  pending-supervision path, the liveness probe, telemetry push, the checkpoint
  anchor, the firewall and wireless guards, the containment script, the
  MASTERSPIKE generator, the vault provisioning and signing flow, the Open
  WebUI wiring, the reflex surface, the shim discipline, the container
  surface.
- **Contracted and scaffolding** — parts of the admin surface; the
  agent-domain ports `4097`, `4098` and `4100` are provisioned but nothing
  binds them; the nocturnal scheduler and share ratification are Master-side,
  and the peer's participation in them is only specified.
- **Deliberately parked** — the destructive legs of the cluster's own
  runbook (rollback; replace-Master re-formation) are documented and
  deliberately left unexecuted. The reflex **enforce** stage is deferred;
  shadow and assist are what run.

The honest test: **if you cannot point at a command that exercises it, it is
specified.** Do not describe a specified-only surface as working. The peer's
ports are in `contracts/ports.json`; the role has no signing path at all.

---

## 15. Glossary

| Term | Meaning |
|---|---|
| **Peer** | A node that verifies the Master's decisions and never signs. |
| **Guard** | The daemon that verifies Master-signed approvals and applies them to protected paths. |
| **Anchor** | The unit that owns the `wg0` interface and pins its configuration. |
| **Overlay** | The WireGuard network the roles talk over. |
| **Registry** | The Master-signed statement of which node may hold which capability. |
| **Capability** | One entry of the closed vocabulary. |
| **Protected path** | A path the guard may write to, and only under the right conditions. |
| **Dual-signed approval** | An approval carrying both the note's signature and the approver's. |
| **Ledger** | The hash-chained record of approvals and decisions. |
| **Provenance chain** | The per-note signature and its verification path. |
| **Shim** | A symlink from a legacy name to its numbered home. |
| **Precontext** | Vault content baked into a template and prepended to a chat request. |
| **MASTERSPIKE** | The `llm-wiki` format expressed as an inference artefact. |
| **Spec-only** | Designed and contracted, no code, by deliberate role boundary. |
