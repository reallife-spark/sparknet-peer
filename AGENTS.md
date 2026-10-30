---
title: sparknet peer-node — agent charter
updated: 2026-10-06
---

# sparknet peer-node — agent charter

The **peer role** of the Sparknet fabric: a working node that serves its own
knowledge vault and, while anchored to a Master, accepts Master-signed
decisions about what it may change.

This is a standalone knowledge vault. A session started inside `peer-node/`
resolves to *this* directory as its knowledge base, not to the parent.

## Role in one paragraph

A peer is autonomous for **reading, inference, and its own vault**. It is
subordinate for **authority**. It never holds the Master Ed25519 private key,
so it can verify an approval but never authorise one. With no anchor it
degrades to standalone: the vault still works, the guard refuses to bind and
refuses to apply. That asymmetry is deliberate — a node that has lost its
Master has lost its authority, and the correct response is to keep it useful
but inert.

The Master role lives in `master-node/`, and its signing side is **deployed
and proven live**: on 2026-10-07 a 2/2 pair co-formed a cluster trust root, the
signer ran as `sparknet-master-signer.service`, signed a live registry and
survived a real reboot. A node is Master because the members ran the ceremony
and a unanimous signed cluster trust root landed (2 of 2 in a pair, 3 of 3 in
the full cluster) — not because of anything on disk, which is why
`master-node/manifests/master-identity.json` says `role: "peer"` until the
ceremony does.

## Boundaries vs the Master

| Concern | Peer |
|---|---|
| Own vault, local inference | **authoritative** |
| Own protected files | verifies before applying |
| Signing new authorizations | **never** |
| Revoking any node | **never** |
| Own overlay address | `10.100.0.x/32`, one address |
| Network egress | restricted to the Master (`10.100.0.1`) only |
| Teardown | loses the link; cannot grant itself anything |

A peer holding the local root account can still rewrite its own machine. That
is not a property this design can prevent — it is why the useful protections
are the ones that do not depend on the peer's cooperation: `chattr +i` on
protected files, the host-firewall egress restriction (iptables on the deployed
fleet), and the requirement for a Master signature that simply does not exist
locally.

## The manual

[`USER_MANUAL.md`](USER_MANUAL.md) is the developer-facing manual for this
role: what a peer is, the seven services, the 24 scripts, the nine units, the
manifests, the vault, the gate, and how to make a change correctly. It is
published at the root of the `sparknet-peer` repository.

The **master manual** for the whole system lives in the `sparknet` repository
as `USER_MANUAL.md`; the peer's contract and the shared implementation it
obeys are named in prose there, because the published `sparknet-peer`
repository carries this role and not the contract.

## Layout (numbered PARA layout; legacy names are shims)

This subvault stores its vault content in numbered directories. Every legacy
category name (`concepts/`, `references/`, `_raw/`, …) is a **symlink** to its
numbered home, so the wiki skills' `VAULT_CATEGORIES` and every old instruction
that names a category directory keep working — both spellings are one file.

| Path | Holds |
|---|---|
| `00_System/` | `user.md`, `alpha-agent.md`, `schema.yaml`, `secrets-index.json`, `index.md`, `log.md` |
| `01_Inbox/` | unprocessed intake awaiting dispatch and signature |
| `02_Daily/` | journal entries and session memory (`YYYY-MM-DD.md`) |
| `03_Sources/` | external material and raw captures: docs, specs, transcripts, datasets |
| `04_Evergreen/` | stable evergreen material |
| `05_Knowledge/` | `concepts/`, `entities/`, `skills/`, `synthesis/` |
| `06_Projects/` | per-project knowledge |
| `07_Archives/` | superseded material and vault snapshots |

Shims (symlinks created by `../scripts/sparknet-env-init.sh`, never hand-made):

| Legacy name | Points at |
|---|---|
| `concepts/`, `entities/`, `skills/`, `synthesis/` | `05_Knowledge/<name>` |
| `journal/`, `sessions/` | `02_Daily/` |
| `references/`, `_raw/` | `03_Sources/` |
| `projects/` | `06_Projects/` |
| `_archives/` | `07_Archives/` |
| `index.md`, `log.md` | `00_System/<name>` |

`_staging/` is real, not a shim — it is the skill-internal staging area
(`WIKI_STAGED_WRITES`), not vault content. Write new content to the numbered
path; a shim exists so old links and old instructions still resolve.

Role-specific trees:

| Path | Holds |
|---|---|
| `manifests/` | this node's identity, critical paths, security policy, registry copy |
| `scripts/` | provisioning, rendering, install and containment scripts |
| `services/` | guard daemon, dispatch bridge |
| `systemd/` | unit files (no vault paths; they call generated shims) |
| `configs/` | network and WireGuard templates |
| `blockchain/` | hash-chained approval ledger |
| `agents/` | per-domain agent compose definitions |
| `deployment/` | netplan and shared Makefile fragments |
| `docs/` | peer-side operational documentation |
| `plans/` | peer-side planning notes |
| `tests/` | the peer suite |

## The contract this role obeys

`../contracts/` is the single source of truth for overlay addressing, the
capability vocabulary, the identity schema and the guard protocol. Read it
before changing anything that touches the Master↔peer conversation.

`../shared/sparknet-authz.py` is the one implementation of the signed
authorization scheme. Do not copy it into this subvault, and do not reimplement
its canonical form — a second copy can drift and then silently stop validating
the Master's signatures.

## Memory Protocol

- `MEMORY.md` — **standing state only**, tiny: plan cursor, durable facts,
  blockers. Replace, never append indefinitely.
- `02_Daily/YYYY-MM-DD.md` — that session's full record (`sessions/` is a shim
  to it). One file per day; sessions spanning midnight use the start date.
- Read `AGENTS.md`, `MISSION.md` and `INTENT.md` at session start: the charter,
  the role's purpose and the design contract behind it. Then read `MEMORY.md`
  and today's session file; write detail only to today's session file. Never
  back-write history into `MEMORY.md`.

## Plan Protocol

`PLAN.md` is the single source of truth for multi-step work.

- Frontmatter `current:` marks the step being worked.
- Mark a step `[x]` **only** after its DoD is verified, then advance the cursor
  and write `PLAN.md` back **immediately**.
- Append one line per transition to `00_System/log.md` (append-only audit
  trail; `log.md` at the root is a shim to it).
- Terminal state is `current: done`. New roadmap work gets a **new** step; do
  not silently reuse a finished plan.
- A step whose DoD cannot be auto-verified is marked `DoD: confirm: user`.

## Canonical placement

- Distilled page → `05_Knowledge/<category>/`. Raw capture → `03_Sources/`
  (`_raw/` is a shim) until distilled. Inbox intake → `01_Inbox/`.
- Logs and telemetry → `/mnt/log/`, not the vault and not `~/`.
- Runtime caches → `$OPENCODE_DATA_DIR`; secrets → `.state/` (gitignored,
  mode 0700).
- One page per file; no duplicate copies. Prefer editing an existing page over
  adding a near-duplicate; cross-link instead of copying.

## Wiki skill suite

**Vendored in this repository** at `../.opencode/skills/` — there is no global
skill farm dependency, so a bare clone or an extracted node bundle can run the
whole suite. Do not reach for a copy under `/mnt/nvme` or `~/.config/opencode`.

Before using any skill, resolve this vault explicitly:

```bash
# From a role subvault. Walks up to the nearest copy — no .git required, because a
# standalone bundle ships without one.
for d in "$PWD" "$(dirname "$PWD")"; do
  [ -f "$d/scripts/sparknet-wiki-env.sh" ] && { source "$d/scripts/sparknet-wiki-env.sh"; break; }
done
```

That prints the role, the role root and the `VAULT_PATH` the skills will use.
**If it fails, stop** — run `../scripts/sparknet-env-init.sh .` once. Do not work
around it: a silent fallback resolves a *different* vault and every page lands in
the wrong place with no error.

Config resolves from `.env` in this directory (`VAULT_PATH`) — never hard-code a
path in a page. `make wiki-preflight` runs the same check.

Run `/wiki-status` first, then `/wiki-ingest`, `/wiki-query`, `/wiki-lint`,
`/wiki-capture`, `/wiki-synthesize`, `/wiki-digest`, `/wiki-rebuild`,
`/daily-update`.

### Role-local skill catalog

`peer-node/.opencode/skills/` is this role's addition to the base suite. It is
the expanded engineering set and is **sellable by construction**: MIT,
Apache-2.0, BSD, ISC, CC0/Unlicense, and the original dual MIT/Apache
`skill-template` only. The removed proprietary and unclear names are not
present; `kimi-*`, `docx`, `pdf`, `pptx`, `xlsx`, `powerpoint`,
`doc-coauthoring`, `verify`, the bundled `claude-api` and
`malware-analysis-static` are excluded.

The catalog is generated; do not hand-edit it. Counts, licenses, source paths
and hashes are in `.opencode/skills/.inventory.json`. Refresh with
`make skills-sync`; verify with `make skills-check`. The bundle check runs the
same manifest check on an extracted node.

## Templates

| Template | Purpose |
| --- | --- |
| `deployment/vault-template/` | Starter `00_System/` set for a fresh vault (`cp` + `make user-init`). |
| `manifests/node-template-manifest.json` | Identity manifest skeleton populated at provisioning. |
| `configs/wireguard/wg0-node.conf.template` | Peer WireGuard stanza (see `docs/wireguard.md`). |
| `agents/template.sparknet-agent@.service` | Per-agent systemd unit template for role agents. |

## Boundaries

- **Scope discipline.** This vault holds peer-role sparknet knowledge. Master
  role material goes in `../master-node/`; cross-role contracts in
  `../contracts/`.
- **No secrets.** Never commit tokens, keys or `.env` contents. Reference the
  secret *path* only. Key material lives in `.state/keys` at mode 0700.
- **Offensive security is out of scope** for a general/default agent session.
  Route offensive work to the `red` role and defensive work to `blue`.
- Cite sources. A distilled page without provenance is a guess — mark it as one.
- **Do not claim live verification** of WireGuard, iptables, `chattr +i`,
  KillSwitch or systemd behaviour. This host has none of them; those are
  privileged-target-node checks and are listed as unverified in
  `docs/wireguard.md`.