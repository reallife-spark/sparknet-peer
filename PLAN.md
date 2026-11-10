---
title: sparknet peer-node Plan
updated: 2026-11-09
current: 19
---

# sparknet peer-node — Active Plan

> Single source of truth for multi-step work. The `current:` cursor names the
> step being worked. Write this file back **immediately** on every advance and
> append the transition line to `00_System/log.md`. Completed steps 1–10 are in
> `plans/archive/PEER_PLAN_FULL_2026-10-26.md`; do not copy their history back.

## Status Legend

- `[ ]` not started / in progress
- `[x]` complete — DoD verified
- `[~]` closed by scope/supersession — explicitly not DoD-verified as a new implementation
- **DoD** — the check that proves the step is done; `DoD: confirm: user` when a
  human must confirm
- `current: N` — step being worked; `current: 16` — terminal

## Closed steps

| Step | Scope | Evidence |
|---|---|---|
| 1–10 | subvault, container, grants, bridge, Open WebUI, conversation passing, MASTERSPIKE, sharing, AI stack, exposed/firewalled stack | archived plan and `log.md` |

## Step 11 — Per-user knowledge vaults (`peer-<user_id>`) and user KBs

- [x] **Step 11** — Every approved Open WebUI user gets a copy of the
  `peer-node` knowledge-vault slice at `/srv/sparknet/knowledge/peer-<user_id>/`,
  created on `pending` → `user`, connected to a KB they own and grow.
  - **DoD**: (1) the provisioner watches `GET /api/v1/users/` with the node admin
    token and records state under `/srv/sparknet/knowledge/.state/`; (2) the
    slice excludes `.state/`, `blockchain/`, `services/`, `scripts/`,
    `container/`, `configs/`, `tests/`, `manifests/`, `.git/` and the template
    `.env`; (3) the KB is created with a `read` grant to the user, embeds the
    vault markup and is the default model's `meta.knowledge`; (4) a digest pass
    re-embeds changed pages; (5) `master-<id>` stays inactive on a peer;
    (6) tests, `contract-test`, `lint` and `bundle-verify` are green.
  - **Status: complete 2026-10-31.** A disposable live account
    (`026a5abd-659f-44dc-acba-b9c6a409404f`) moved `pending` → `user` after
    Master approval; the provisioner created 26 files and KB
    `6c6198bf-2462-4c1c-964c-f2349fe1485f`; deletion removed the user, vault,
    KB/model and state record. Tests cover the slice, KB grant/model wiring and
    digest path; `make test-all` is green.

## Step 12 — Node admin agent: auth events and pending-signup supervision

- [x] **Step 12** — A local, stdlib-only agent turns the node's Open WebUI into
  an approval-mediated surface the Master can supervise.
  - **DoD**: signup enabled with `DEFAULT_USER_ROLE: "pending"`; the agent
    appends normalised auth events to a 0600 spool and registers its webhook;
    the node admin is a machine service account; `tests/test_admin_agent.py`
    proves the spool, allow-list, webhook body and pending gate; `manifests/
    ai-stack.json` records the posture; gates green.
  - **Status**: code complete 2026-10-12; live on the retired pair, not yet on
    the current pair.

## Step 13 — User AI context, knowledge loop and self-healing

- [x] **Step 13** — Make the peer a coherent user AI node on `agx64`:
  - [x] **13A — Context.** Generate `CONTEXT.md` from the identity pages and
    `../contracts/knowledge-flow.md`; load it in OpenCode, the Open WebUI
    structure KB, per-user KBs/models, the vLLM/MASTERSPIKE precontext and the
    admin terminal. `make context-check` is a gate.
  - [x] **13B — Chat/plan/build surface.** The local model is the per-user chat
    default; `plan` and `build` are selectable agent models; `MODEL_FILTER_LIST`
    names the same surface as `manifests/agent-models.json`.
  - [x] **13C — Knowledge request.** Add a peer RFI request client and a
    `vault.rfi` bridge tool. A request names an explicit scope; it never
    grants or widens anything.
  - [x] **13D — Knowledge intake.** A Master answer installs in `01_Inbox/`,
    records `inbox_acceptance`, is sanitized when untrusted, and is indexed
    into the user's KB only after acceptance.
  - [x] **13E — Self-healing.** `make vault-health` diagnoses; `make vault-heal`
    repairs only derived/safe state; `docs/self-healing.md` states what is
    report-only and what is gated; the timer is staged.
  - **DoD**: `make context-check`, `make vault-health`, the RFI loop test, the
    context-propagation test and the peer suite are green; `make bundle-verify
    ROLE=peer` passes; `agx64` live legs are `DoD: confirm: user`.
  - **Status: complete and live-verified on agx64 2026-10-31.** Context is in WebUI RAG, per-user KBs, vLLM precontext and OpenCode; `vault.rfi` completed a real request→answer→inbox round trip; telemetry/checkpoint/self-healing timers run; peer suite and bundle verify pass.

## Step 14 — Qdrant node surface, inventory and uplift

- [x] **Step 14** — Replace the node's ChromaDB with a loopback Qdrant server,
  project users/models/usage into `sparknet-inventory`, and push consented
  uplift data through the durable outbox.
  - [x] **14A — AI stack.** `docker-compose.qdrant.yml` declares a pinned,
    loopback-only Qdrant; Open WebUI uses `VECTOR_DB=qdrant`; the firewall and
    manifest no longer protect ChromaDB. Local tests pass.
  - [x] **14B — Inventory.** `services/sparknet-inventory-agent.py` normalises
    users/models/KBs/usage and never stores prompt text in Qdrant.
  - [x] **14C — Uplift.** `scripts/sparknet-uplift-push.py` signs inventory,
    trace, snapshot and KV pushes and spools failures.
  - [x] **14D — Live.** Migrated agx64 on 2026-11-01: Qdrant `v1.20.0` on
    loopback, Open WebUI 4 KBs/107 files reindexed, per-KB and per-file
    retrieval 200, inventory 41 records mirrored on the Master, four collection
    snapshots uploaded, Chroma stopped and its volume retained for 14 days.
  - **DoD**: peer suite green; live legs `DoD: confirm: user`.

## Step 15 — Peer typed memory adapters

- [x] **Step 15** — Serve the per-vault memory plane from the node:
  - [x] **15A — Service and gateway.** The Go `sparknet-memory` service on
    loopback `4110`, the Python gateway on `4093`, the setup-generated
    gateway/local/WebUI tokens, and `make memory-index` for vault pages.
  - [x] **15B — OpenCode.** `shared/sparknet-memory-mcp.py` is registered in
    `opencode.json`; it captures turns, injects scoped recall as data, and exposes
    search/remember/status/forget/project-delegate tools. The plugin route is
    deliberately absent because OpenCode 1.18.35 hangs on role-local plugins.
  - [x] **15C — Open WebUI.** `webui/functions/sparknet_memory_filter.py`
    injects and captures through the gateway; the filter is installed by the
    workspace helper.
  - [x] **15D — Console.** The read-only Memory page reads counts, retention and
    policy through `/api/memory`; it never reads a vault directly. Project
    lifecycle and bundle commands are operator CLIs, not console writes.
  - [x] **15E — Projects.** `make project-new|list|run|status|report` and the
    project agents cover `06_Projects`; a report is signed only after the
    operator files it.
  - [x] **15F — Bundles.** Export/validate/stage/import via
    `make memory-export|memory-bundle-validate|memory-bundle-stage|memory-import`.
  - **DoD**: `make memory-check`, `make test-role`, `make test-webui-console`,
    `make lint`, `make format-check`, `make context-check`,
    `make bundle-verify ROLE=peer`; live OpenCode/WebUI recall and capture are
    `DoD: confirm: user`.
  - **Status: complete 2026-11-04.** The memory sidecar is live on agx64 (`4110` store, `4093` gateway); the peer console `/api/memory` returns 200; OpenCode MCP `memory_remember`/`memory_search` round-trips; the Open WebUI filter is installed, active and global, and the WebUI token path stores and retrieves a user-vault record.

## Step 16 — OpenCode engine and Open WebUI v0.13.0

- [x] **Step 16** — Replace the bridge-as-model ack surface with the OpenCode
  engine and the workspace baseline.
  - [x] `services/sparknet-opencode-engine.py`, `configs/opencode-runtime/`,
    `configs/opencode-engine/`, `systemd/sparknet-opencode-engine@.service`;
    one sandbox per `peer-<user_id>`, no key/state/role-vault mount.
  - [x] `scripts/sparknet-webui-workspace.py apply`: settings, prompts, tools,
    functions, per-user model migration, structure-only KB, reindex.
  - [x] Live on agx64: Open WebUI `v0.13.0`, 16 prompts, 4 tools, 2 functions,
    three per-user models on `sparknet-chat`; chat returns `engine-ok`; build
    writes inside the sandbox only.
  - **DoD**: peer suite, contract-test, lint/format/shellcheck/render/sbom and
    `make ci CI_QUICK=1` green; browser click-through `DoD: confirm: user`.

## Step 17 — Peer role inversion: agx32 peer

- [x] **Step 17** — Move the peer role from `agx64` to `agx32` as part of the
  fabric role inversion.
  - [x] **17A — Freeze and preflight.** Park peer live legs; stop peer services;
    archive and verify `peer-*` vaults, provisioner `.state`, WebUI volume,
    Qdrant volume, memory state, OpenCode engine state and peer state dir;
    record users, KBs, chats, collection counts and service paths.
  - [x] **17B — Identity and overlay.** Set `peer-node/manifests/node-identity.json`
    to `sparknet-node-0002` at `10.100.0.2/32`; generate the new peer node
    identity key on `agx32`; keep the cluster key; render the peer WireGuard
    config and firewall egress restriction to `10.100.0.1`.
  - [x] **17C — Move data and services.** Restore the peer vaults, provisioner
    state, WebUI volume, Qdrant volume, memory and engine state on `agx32`;
    start guard, bridge, 4201/4202, telemetry/checkpoint/inventory/RFI, memory
    4110/4093, WebUI 8181, engine 4204, Qdrant, provisioner, admin agent and
    self-healing.
  - [x] **17D — Registry and model placement.** Install the higher-sequence
    registry signed by the new `agx64` Master; verify the peer entry; move the
    student runtime config/compose to `peer-node/`; update the peer WebUI model
    filter and `agent-models.json` to the student surface.
  - [x] **17E — Verify and clean up.** Prove user/KB/chat parity, memory and
    engine round trips, guard registry verification, model benchmark and all
    peer gates; remove the old peer services and old user-data copies from
    `agx64` only after `agx32` is verified.
  - **DoD**: peer WebUI 8181, guard 9090, bridge 4096, share 4201/4202,
    memory 4110/4093 and engine 4204 are healthy on `agx32`; user data and
    Qdrant counts match preflight; peer egress is restricted to `10.100.0.1`;
    peer suite, `make contract-test`, `make context-check`,
    `make bundle-verify ROLE=peer` and `make ci` pass; live legs are
    `DoD: confirm: user`.
  - **Status 2026-11-06:** peer live on `agx32`. Guard 9090, bridge 4096,
    share 4201/4202, memory 4110/4093, WebUI 8181, console 8099, engine 4204,
    Qdrant 6333. Registry sequence 9 installed; `authz-verify` allowed; RFI
    answer installed with `inbox_acceptance`; telemetry and 18-record
    inventory pushed; teacher/student benchmark 21.88/10.95 tok/s, 100%
    quality. Old `agx64` peer data/units removed. Step complete.

## Step 18 — Peer-side Gemini blueprint surfaces

- [~] **Step 18 — CLOSED BY STEP 89 SUPERSESSION (scope).** The peer half of
  the sovereign-cluster blueprint was delivered by Step 89C–H; this step is not
  a second implementation.
  - [x] **18A — Event bus and agents.** Step 89C: NATS/JetStream, the peer
    agent bus, signed task/result envelopes and queue/health surfaces.
  - [x] **18B — Collaboration.** Step 89G: cross-department rooms, personal
    agent participation, ABAC filtering and audit.
  - [x] **18C — Execution.** Step 89G: argv-only exec RPC/daemon, path roots,
    user OS cap, pairing and no `shell=True`.
  - [x] **18D — Self-improvement and packages.** Step 89H: worktree/Firecracker
    mutation, deterministic benchmarks, GitFlow promotion/rollback and the
    signed package CLI.
  - [~] **18E — Retrieval and model tap.** Graph, hybrid retrieval and the
    student model surface are live from Step 89D/F. The live vLLM KV connector
    remains deliberately refused by `scripts/sparknet-kv-export.py --from-vllm`;
    a simultaneous dual-response stream was not adopted. Those two optional
    extensions are retired from this step; a future need is new work.
  - **Status 2026-11-09 — closed by supersession.** Evidence: the Step 89
    channel matrix (`contracts/step89-matrix.json`), `tests/test_event*.py`,
    `test_agent_bus.py`, `test_orchestrator.py`, `test_collab.py`,
    `test_exec*.py`, `test_self_improve.py`, `test_package*.py`,
    `test_graph.py`, `test_hybrid.py`, `test_pipeline.py`, and the peer bridge
    Step 89 route tests. No new authority or knowledge channel was added.

## Step 19 — Understand-Anything + issue surfaces (peer half)

- [ ] **Step 19** — Add the peer half of the Step 91 integration. Opened ahead
  of the queued Step 18 by operator direction on 2026-11-09; Step 18 remains
  queued and is not cancelled.
  - [x] **19A — Skills.** Load the nine `/understand*` skills, the Matt Pocock
    engineering family and `setup-matt-pocock-skills` in the peer workspace.
  - [x] **19B — Issue tracker.** Run the setup with the local markdown tracker;
    write `docs/agents/issue-tracker.md`, `docs/agents/domain.md`,
    `docs/agents/triage-labels.md`, `GLOSSARY.md`, `docs/adr/` and
    `.scratch/issues/`.
  - [x] **19C — Pusher.** `services/sparknet-issue-push.py` reads local issues,
    uses the shared issue module for parse/validation/redaction, signs through
    `shared/sparknet-http.py` and posts to the Master `/api/v1/issues` under the
    `issue-report` capability. Fabric 93B moved the shared policy behind
    `shared/sparknet-issue.py`.
  - [x] **19D — Console.** The shared Understand and Issues tabs render for the
    peer role with the peer config.
  - **DoD**: `make -C peer-node test`, `make contract-test`, `make context-check`,
    `make role-skills-check` and `make bundle-verify ROLE=peer` are green; the
    local issue file schema and push payload round-trip in tests; live Master
    delivery is `DoD: confirm: user`.

  - **Status 2026-11-09 — peer half implemented.** The peer workspace loads
    the base skill tree and the peer catalog (125 role-local, 80 base), the
    local issue tracker and docs are in place, the signed pusher and its timer
    are installed by the unit installer, and `make -C peer-node test`,
    `make bundle-verify ROLE=peer` and `make ci` are green. Live Master delivery
    from the peer is `DoD: confirm: user`.

## Next

`current:` moves one step at a time, and only after the DoD is verified.
Master-side work stays in `../master-node/PLAN.md`; fabric work in `../PLAN.md`.
