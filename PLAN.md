---
title: sparknet peer-node Plan
updated: 2026-11-10
current: done
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

- [x] **Step 19** — Add the peer half of the Step 91 integration. Opened ahead
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

  - **Status 2026-11-10 — COMPLETE, including live delivery.** The live peer
    at `agx32` signed `live-step91-issue` with its node key and delivered it to
    the Master at `10.100.0.1:8090`; the Master accepted it as
    `sparknet-node-0002`. The peer code is `a64e993`; the current peer bundle
    rebuilt and verified. No services outside the peer live leg are claimed.

## Step 20 — OpenShell execution sandbox

- [~] **Step 20 — CLOSED BY SCOPE (platform).** Replace the hand-rolled
  per-user Docker supervisor with OpenShell as the peer execution sandbox,
  running as the node owner's numeric UID/GID, with one vault mount, local
  approvals and Master-signed supervision. The v0.1.7 gateway was installed
  and authenticated on `agx32`, but the Docker driver cannot create a sandbox
  on the current Jetson pair (Docker 26.1.3 < 28.0; kernel 5.10.216-tegra has
  no Landlock ABI 3). The operator kept the existing Docker supervisor and
  removed the OpenShell install. The contract/profile code remains an
  uncommitted proposal for a supported host or a future VM-driver decision.
  - [x] **20A — Contract surface.** Add `sandbox-supervise` to
    `contracts/capabilities.json` and `shared/sparknet-authz.py`; add the
    `sandbox-request` envelope and its closed action set; add
    `contracts/sandbox-schema.json`; name `/sandbox/*` on bridge 4096; add the
    knowledge-flow row, the README index row and ADR 0012.
    **DoD**: `make contract-test`, `make render-check` and the envelope tests
    are green; the capability is inert until a signed registry grant exists.
    **Status 2026-11-10 — COMPLETE.** `sandbox-supervise` is in the shared
    vocabulary and both definitions; the `sandbox-request` envelope is pinned
    with a byte vector; `make contract-test`, `make render-check`,
    `tests/test_contracts.py` and `tests/test_envelopes.py` are green. No live
    grant is claimed.
  - [~] **20B — Gateway service.** Use the package-managed
    `openshell-gateway.service` (owner user service with linger, local mTLS,
    never an unauthenticated loopback gateway); add
    `peer-node/configs/openshell/gateway.toml` (Docker rootful,
    `allow_driver_config`, `enable_bind_mounts`, resource admission off), the
    env template and the operator README; pin OpenShell v0.1.7 by digest.
    **DoD**: config preflight and the live unit pass; the gateway binds loopback
    only and authenticates with the local mTLS bundle.
    **Status 2026-11-10 — validated, then removed by operator decision.** The
    v0.1.7 `.deb` was installed; `openshell-gateway.service` was active, mTLS
    authenticated, `openshell status` Connected and `config preflight` passed
    with `gateway.toml`. The custom sparknet unit was removed because the
    package owns the service. The package, config and state were removed when
    the platform blocker below closed the step.
  - [~] **20C — Identity and profile.** Add
    `peer-node/lib/sparknet_sandbox_profile.py` as the single owner-identity,
    policy and one-mount source; resolve the owner UID/GID in
    `sparknet-opencode-engine.py`, refuse 0, add a symlink-safe one-time re-own
    from `10001:10001`, and add `--sandbox-backend docker|openshell` with docker
    as the rollback default. 20D owns the runtime `WORKDIR` and the OpenShell
    create/transport path.
    **DoD**: tests prove uid/gid resolution, uid-0 refusal, the policy shape,
    the single mount, reserved-target refusal and migration idempotency; a file
    written in `/work` is owned `<uid>:<gid>` on the host
    (`DoD: confirm: user`).
    **Status 2026-11-10 — profile layer complete; not deployed.** The module
    and engine wiring are in place; the profile and service suites are green.
    Selecting `--sandbox-backend openshell` refuses with a named 20D message
    rather than starting a half-wired sandbox. The live file-ownership leg is
    blocked by the 20D platform finding.
  - [~] **20D — Model proxy and approval surface.** Replace the per-sandbox
    Unix socket with an authenticated host-side proxy reachable as
    `host.openshell.internal:<port>`; keep `proposal_approval_mode=manual` and
    `agent_policy_proposals_enabled=false`; replace the blanket reject in the
    engine with a pending proposal the user can approve or reject.
    **Blocker (2026-11-10):** the gateway was installed and authenticated on
    `agx32`, but the Docker driver cannot create a sandbox. The host runs
    Docker 26.1.3 (OpenShell requires 28.0+) and kernel 5.10.216-tegra with no
    Landlock LSM (OpenShell requires ABI 3, Linux 6.2+); `agx64` is identical.
    The Docker-driver plan is not deployable on this hardware. 20D was not
    implemented against an unsupported runtime; the VM driver (`/dev/kvm` is
    present) or a supported host is a new decision.
    **DoD**: the sandbox reaches only the model endpoint; other egress is
    denied; a proposal is never auto-approved; approve/reject round-trips.
  - [~] **20E — Signed supervision.** Add `/sandbox/*` to the dispatch bridge
    with the shared authz envelope, replay store and the `sandbox-supervise`
    grant; translate a verified request to a local OpenShell call without
    handing the Master the gateway credential.
    **DoD**: unsigned, stale, replayed and unknown-capability requests are
    refused before action; a signed request with no grant is inert; a granted
    stop/start works; no token appears in logs.
    **Status 2026-11-10 — not implemented; closed by platform scope.**
  - [~] **20F — Persistent services.** Add
    `peer-node/services/sparknet-sandbox-services.py` and its systemd unit,
    reading a 0600 manifest, creating service sandboxes with persistent mounts
    and never idle-pruning them.
    **DoD**: the reconciler is idempotent; a service sandbox survives a gateway
    restart and logout; session sandboxes still idle-stop.
    **Status 2026-11-10 — not implemented; closed by platform scope.**
  - [~] **20G — Gates and live leg.** Run the peer, contract, context, render,
    SBOM, bundle and CI gates; verify on `agx32` that the sandbox runs as the
    owner UID, writes owned files, enforces the read-only mount and egress
    policy, accepts signed supervision and rejects unsigned requests.
    **DoD**: named gates green; live legs `DoD: confirm: user`; rollback
    remains `--sandbox-backend docker` until the live leg is confirmed.
    **Status 2026-11-10 — not implemented; closed by platform scope.**
  - **DoD**: 20A–20G each have their named gate; a cross-role change commits the
    peer role first, then the superproject with the updated pin and contracts;
    `make contract-test` and `make ci` are green; no live claim comes from this
    host.
  - **Status 2026-11-10 — CLOSED BY SCOPE.** The v0.1.7 gateway was installed
    and authenticated on `agx32`; the Docker driver cannot create a sandbox on
    the current Jetson pair (Docker 26.1.3 < 28.0; kernel 5.10.216-tegra has no
    Landlock ABI 3). The operator kept the existing Docker supervisor and
    removed the OpenShell package/config/state. 20A remains complete; 20B/20C
    are retained as an uncommitted proposal; 20D–20G were not implemented.
    The uplift drain was paused and its timer disabled to stop the disk fill;
    unused images were pruned. The live DoD is not claimed.

## Step 21 — OpenShell VM driver validation

- [~] **Step 21 — CLOSED BY SCOPE (peer hardware).** Prove the OpenShell
  microVM compute driver on a supported host before choosing it as the peer
  execution path. The peer Jetson pair is blocked at the Docker/kernel layer;
  the VM driver was validated where glibc, Docker and the kernel meet
  OpenShell's requirements. 21A–21C are complete on the dev host; 21D remains
  blocked on the peer.
  - [x] **21A — VM gateway.** Install OpenShell v0.1.7 with
    `compute_driver = "vm"`, package-managed mTLS, owner UID/GID 1000, a
    trusted bootstrap image and the VM driver binary.
    **DoD**: `config preflight`, `openshell status` and the driver process are
    green.
    **Status 2026-11-10 — COMPLETE on the dev host.** Debian forky, glibc
    2.42, Docker 28.5.2, kernel 7.0 with Landlock, `/dev/kvm`; preflight,
    mTLS authentication and the `openshell-driver-vm` process are green.
  - [x] **21B — VM sandbox.** Create a microVM with a bind-mounted host
    directory, the `sandbox` guest selector reconciled to the owner UID/GID,
    and an exposed guest service.
    **DoD**: the sandbox is Ready; the guest writes a host file owned
    `<uid>:<gid>`; an exposed port returns HTTP 200.
    **Status 2026-11-10 — COMPLETE on the dev host.** `ubuntu:24.04` reached
    Ready; the guest resolved `sandbox` to UID/GID 1000; the virtiofs rw mount
    produced `1000:1000` host ownership; `python:3.12-slim` on port 4200
    returned HTTP 200 through the service URL. The probe sandboxes were
    deleted; no peer claim is made.
  - [x] **21C — Engine integration.** Add the VM driver config JSON, the
    `sandbox` policy identity, the OpenCode service URL transport and the
    host-side model proxy to the engine, then run a real OpenCode turn in a
    microVM on the validated host.
    **DoD**: the engine creates a VM sandbox, reaches OpenCode, and completes a
    prompt; no Docker socket or Unix socket is used.
    **Status 2026-11-10 — COMPLETE on the dev host.** The engine created a
    microVM sandbox, opened an `openshell forward service` loopback port with
    Basic auth preserved, and completed a real OpenCode prompt through the
    host-loopback model proxy: assistant **VM-OK**. The slim VM image target
    `opencode-runtime-vm` was added. Peer deployment is still 21D.
  - [~] **21D — Peer deployment.** Run the VM driver on `agx32`.
    **Blocker (2026-11-10):** the embedded `libkrun.so` requires
    `GLIBC_2.33`, but Ubuntu 20.04 has `2.31`; the host also has Docker 26.1.3
    (< 28.0) and kernel 5.10.216-tegra without Landlock ABI 3. The VM path is
    proven on a supported host but is not deployable on the current Jetson
    pair without an OS/host change. Not implemented; a supported host or a
    host move is a new decision.
  - **Status 2026-11-10 — 21A–21C complete on the dev host; 21D blocked.**
    The VM driver and engine transport are viable on supported hardware. The
    peer `agx32` keeps the current Docker supervisor; its OpenShell package
    was removed. The dev-host gateway/config was retained as a validation bed.
    No peer live claim is made.

## Step 22 — Close the two-axis review

- [~] **Step 22 — CLOSED AFTER THE FIX PASS; signed goagx republish delegated.**
  Fix the Standards and Spec findings from the uncommitted OpenShell review:
  contract truth, driver-neutral identity, missing listeners, owner rendering,
  artifact pins, fail-closed migration, the VM-only profile, loader rule,
  WORKDIR, backend split, docs inventory and the test gaps.
  - [x] **22A — Contract truth and schema.** Remove the false `/sandbox/*`
    route-reachable claims; make `sandbox-schema.json` driver-neutral; add
    `uid_min`/`uid_max`; add `17670`, `11501` and `14201` to `ports.json`; bump
    the changed schema versions; render the manifest. **Status 2026-11-10 —
    COMPLETE.** Contract/render tests green.
  - [x] **22B — Owner, pins and rendering.** Add `manifests/openshell.json`
    with the package checksum and digest-pinned images; replace the committed
    home with `gateway-vm.toml.in`; add the owner renderer and `owner.json`.
    **Status 2026-11-10 — COMPLETE.** Render test proves owner 1500 and no
    hardcoded `/home/raxxar` in the template.
  - [x] **22C — Engine and profile correctness.** Source UID bounds from the
    contract, remove the unused Docker profile API, fail closed on chown,
    use the one loader, change `WORKDIR` to `/sandbox` and the Docker command
    to `cd /work`, enforce the owner via `owner.json` and `WorkloadIdentity`.
    **Status 2026-11-10 — COMPLETE.**
  - [x] **22D — Structure and docs.** Split the VM path into
    `lib/sparknet-openshell-backend.py`; move the image recipe to
    `scripts/sparknet-opencode-runtime.sh`; trim ADR 0012 to a decision record;
    add `lib/` to the role inventory. **Status 2026-11-10 — COMPLETE.**
  - [x] **22E — Tests.** Contract truth/driver/UID/ports assertions; VM policy
    and mount tests; migration failure; owner mismatch; fake CLI lifecycle;
    renderer test; Dockerfile/script asset test. **Status 2026-11-10 —
    COMPLETE.** Peer suite 646 passed/1 skipped.
  - [~] **22F — Gates, commit and republish.** Commit the peer role first, then
    the root with the updated pin and contracts; run the gates. The signed
    `goagx` republish is `DoD: confirm: user`: the operational key is on the
    Master, not this host.
  - **Status 2026-11-10 — code and gates complete.** `make test-all`, peer
    suite, contract/render/context/validate/lint/format/shellcheck, SBOM,
    bundle build and bundle verification are green. No peer live claim. The
    goagx republish remains with the Master key holder.

## Next

`current:` moves one step at a time, and only after the DoD is verified.
Master-side work stays in `../master-node/PLAN.md`; fabric work in `../PLAN.md`.
