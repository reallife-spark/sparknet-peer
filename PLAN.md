---
title: sparknet peer-node Plan
updated: 2026-10-05
current: 2
---

# sparknet peer-node — Active Plan

> Single source of truth for multi-step work. The `current:` cursor marks the
> step being worked. Write this file back **immediately** on every advance;
> append the matching transition line to `log.md`.

## Status Legend

- `[ ]` not started / in progress
- `[x]` complete — DoD verified
- **DoD** — the check that proves the step is done; `DoD: confirm: user` when a human must confirm
- `current: N` — step being worked now; `current: done` — terminal

## Step 1 — Verify the subvault after the split

- [x] **Step 1** — Prove `peer-node/` is independently runnable after being
      split out of the parent vault.
      - **DoD**: (1) `make validate` and `make test` pass from inside
        `peer-node/` with the CWD elsewhere; (2) `make test-all` at the parent
        is green; (3) `make contract-test` is green; (4) a relocated copy of the
        parent at a different path also passes; (5) `git status` shows only
        intended changes.
      - **Status**: complete, all five verified. `make -C peer-node validate` and
        `make -C peer-node test` green (185 passed, 1 skipped); `make test-all`
        green across contract, fabric, peer and master; a relocated copy of the
        parent passes the full fabric and peer suites from a different path.
      - **Note**: verifying DoD 4 surfaced a real defect rather than confirming
        the step. `.gitignore` ignored `_staging/` outright while the root
        charter documents it as a category and `tests/test_layout.py` requires a
        tracked `.gitkeep` — so a fresh clone had no `_staging/` at all and
        `test_vault_category_dir_survives_a_fresh_clone[_staging]` failed there.
        The local working tree hid it. Fixed by tracking the root directory
        through its `.gitkeep` while keeping staged pages ignored, and keeping
        the two role subvault queues ignored because neither role charter
        documents them.

## Step 2 — Multi-arch CPU container

- [ ] **Step 2** — The node has to run on a DGX Spark and on x86 GPU servers, and
      the container must not be able to drop its own tunnel.
      - **DoD**: (1) `docker buildx build` produces `linux/amd64` and
        `linux/arm64` from one Make target, with the per-architecture opencode
        binary and **no CUDA** in the peer image; (2) the tunnel lives in a
        long-lived anchor sidecar and the app joins it via
        `network_mode: container:`, so recreating the app leaves the tunnel
        intact — asserted, not assumed; (3) `.state` is a host bind mount, never
        a named volume, and a preflight proves `chattr +i` actually works on this
        filesystem before anything relies on it; (4) the image layout puts the
        node at `/srv/sparknet/peer-node` so the existing
        `SPARKNET_FABRIC_ROOT` default holds, and bakes in no vault path — the
        entrypoint generates it; (5) compose comes up and passes a healthcheck
        **with no GPU present**; (6) `make bundle` still verifies, with the
        container files included.
      - **Status**: pending.
      - **Note**: inference is a separate service — Ollama by default at
        `:11434/v1`, vLLM opt-in at `:8000/v1`. GPU sharing is configured inside
        the inference server, not by slicing containers, because Docker cannot
        partition VRAM.

## Step 3 — Signed session grants and two-tier trust

- [ ] **Step 3** — One signed grant replaces the conversation-hash heuristic and
      becomes the thing every vault write is authorised by.
      - **DoD**: (1) grant mint and verify reuse the guard's existing check
        order from `contracts/guard-protocol.md` plus the node identity key
        against the Master-signed registry — no second crypto implementation;
        (2) config under `/etc` stays Master-signed per file, and knowledge is
        grant-scoped with provenance frontmatter, a hash-chained ledger append
        and `_raw/` quarantine; (3) offline operator signing through
        `shared/sparknet-authz.py sign --master-key` works with no Master
        running; (4) every rejection branch is unit-tested — forged, expired,
        replayed, nonce-reused and out-of-scope all fail closed; (5) a
        subagent's write is authorised by the parent grant but attributed to the
        acting child session in the ledger, so audit is finer than authority.
      - **Status**: pending.

## Step 4 — Bridge: real server, persistent sessions, vault tools

- [ ] **Step 4** — `sparknet-dispatch-bridge.py` becomes a server, because today
      it is 110 lines with no bind code while `ports.json` calls it implemented.
      - **DoD**: (1) the bridge serves OpenAI-compatible HTTP on 4096 and its
        `/v1/models` lists the node's agents; (2) sessions are **persistent**,
        keyed by `grant_id` — the vendored MIT proxy's throwaway-session
        behaviour is replaced, and no conversation identity is derived from
        content hashing; (3) a session survives a bridge restart; (4) the
        `/vault/*` endpoints are grant-checked, canonicalise their paths so no
        request can escape the vault or a sub-vault mount, and append to the
        ledger on every mutation; (5) an OpenAPI spec describes the vault tools
        for Open WebUI to execute as HTTP tools — the proxy's client-executed
        MCP bridge is not used, so no Node runtime enters the image.
      - **Status**: pending.

## Step 5 — Open WebUI surface

- [ ] **Step 5** — The Open WebUI side of the node, configured as committed files
      rather than UI clicks so it survives a redeploy.
      - **DoD**: (1) `plan`, `build`, `vault` and the node orchestrator are
        selectable as models, each routing to its agent; (2) a permission request
        surfaces as an Approve/Deny card and resolves through
        `POST /session/:id/permissions/:permissionID`; (3) the vault tools from
        Step 4 can list, read, capture, promote and patch, and every write is
        grant-checked; (4) Open WebUI's own RAG store is disabled — the vault is
        the only knowledge base; (5) all of it is expressed as versioned
        declarative config, because panel customisations come later and must not
        be trapped in a database.
      - **Status**: pending.

## Step 6 — Intra-node conversation passing

- [ ] **Step 6** — Handing a conversation between the node's own agents.
      - **DoD**: (1) a conversation passes by keeping the session and changing
        agent — `POST /session/:id/message` accepts `agent`, so history survives
        the hand-off intact; (2) branching uses `POST /session/:id/fork` at a
        message and the original stays whole; (3) `GET /session/:id/children` and
        the share endpoints work; (4) the orchestrator's delegation and
        hand-off verbs are exercised against a real server, not asserted.
      - **Status**: pending.
      - **Note**: this step uses only native opencode primitives. No custom
        passing mechanism is introduced, because none is needed — the earlier
        plan's conversation-hash identity is what Step 3 removes.

## Step 7 — `MASTERSPIKE`

- [ ] **Step 7** — A model that carries the Master role and the llm-wiki format,
      without ever being able to sign anything.
      - **DoD**: (1) an Ollama `Modelfile` exists with the llm-wiki knowledge
        format inlined from `.opencode/skills/llm-wiki/SKILL.md` at
        `temperature 0.1`, plus `make model-build` / `make model-serve`; (2) a
        vLLM equivalent prompt artifact is generated **from the same source**, so
        switching inference backends cannot silently drop the format; (3)
        `make model-verify` fails if the two diverge or the source skill moves;
        (4) the model is selectable as an agent, and it holds no signing
        capability — authority is Ed25519 in the Master, never a prompt.
      - **Status**: pending.

## Step 8 — Inter-node sharing and the governance regime

- [ ] **Step 8** — Two users on two nodes share knowledge and sessions, under a
      regime that defaults to quarantine.
      - **DoD**: (1) an invite is open for 1 h; acceptance requires the receiver
        to choose a share type, and may narrow what was offered; (2) share types
        are `kb-view`, `kb-share`, `session-view`, `session-handoff` and `full`;
        (3) temporary and permanent shares behave differently on idle, and a
        share closes after 20 min of inactivity; (4) all four clocks — 1 h
        accept, 20 min idle, 2 h ratification, 10 h end — are **clock-injected**,
        so their tests run in seconds rather than hours; (5) a two-node mesh runs
        `peer_pair`: symmetric, equal privileges, all shared information
        verified against the existing knowledge base and reviewed for additions
        and conflicts, reusing the wiki suite's contradiction detection; (6) a
        three-or-more mesh runs `master_governed`, downgrading both nodes to
        `unauthenticated` and `flagged`, and a third node joining mid-share
        re-evaluates the live share instead of grandfathering it; (7) transport
        is a time-boxed **data-plane** stanza plus an nftables exception, torn
        down on close, idle or expiry, leaving the authority plane's
        one-address `AllowedIPs` invariant intact; (8) non-ratification severs
        the fabric data plane while peer-to-peer continues, and the share ends
        at 10 h.
      - **Status**: pending.
      - **DoD: confirm: user** for (5)–(8). This host has one peer, no master and
        no second node, so cross-node behaviour is unit-testable with in-process
        node pairs and nothing more. The wiring is proven; the deployed behaviour
        is not, and must not be reported as if it were.
      - **Note**: review evidence lands in the Master-hosted git repository, and
        a ratified share is authorised with its flag **retained for audit**.
        Master's ratification service, review agents and evidence repo are
        unbuilt and tracked in `../master-node/plans/MASTER_BUILD_PLAN.md`.


## Next

Steps 2–8 are the node's build-out: container, grants, bridge, Open WebUI,
conversation passing, `MASTERSPIKE`, then the sharing regime. Step 2 is placed
immediately after the fabric's contract steps so the node becomes deployable
before it becomes clever.

`current:` moves one step at a time, and only after a step's DoD is verified.

Master-side work is tracked in `../master-node/plans/MASTER_BUILD_PLAN.md` —
do not duplicate it here. Fabric-level contract and truthfulness work is tracked
in `../PLAN.md`.
