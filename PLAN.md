---
title: sparknet peer-node Plan
updated: 2026-10-12
current: 12
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

- [x] **Step 2** — The node has to run on a DGX Spark and on x86 GPU servers, and
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
      - **Status**: done (2026-10-10). (1) `make container-build` builds both
        images for `linux/arm64,linux/amd64`; the amd64 pair was cross-built on
        this arm64 host and the amd64 opencode binary ran under qemu
        (`opencode --version` → `1.18.35`). (2) `network_mode: service:anchor`
        (compose's namespace-sharing form of the same netns join) — asserted by
        `tests/test_node_container.py`. (3) `.state` is a host bind mount and
        `make container-preflight` refuses here as designed (uid 1000 cannot
        `chattr +i`); the deployed proof is a target-node act. (4) the bundle
        lands at `/srv/sparknet/{peer-node,contracts,shared,scripts,.opencode}`;
        the entrypoint regenerates `.env`, nothing path-baked. (5) the compose
        stack came up and `sparknet-node` reported **healthy** with no GPU; the
        anchor ran its honest inert mode (no rendered config). (6) `make
        bundle-verify ROLE=peer` green with `peer-node/container/` bundled; 19
        wiring tests pass (peer suite 236 passed, 1 skipped).
      - **DoD: confirm: user** for a **live tunnel**, `chattr +i` on a deployed
        node's filesystem, and the healthcheck on a target host — the peer
        charter forbids claiming those from this build host; only the inert-mode
        and no-GPU-health evidence above is real here.
      - **Note**: the anchor's `wireguard-go` is built from source off the
        official GitHub mirror pinned at tag `0.0.20230223` (commit `21636207`),
        because the vanity `golang.zx2c4.com` module domain returns 404 for
        `go get`; `container-load` builds the host arch with `--load` since the
        local store cannot load a manifest list. Inference is a separate
        service — Ollama by default at `:11434/v1`, vLLM opt-in at `:8000/v1`.
        GPU sharing is configured inside the inference server, not by slicing
        containers, because Docker cannot partition VRAM.

## Step 3 — Signed session grants and two-tier trust

- [x] **Step 3** — One signed grant replaces the conversation-hash heuristic and
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
      - **Status**: complete, all five DoD verified 2026-10-10.
        - Grants are verified by `shared/sparknet-authz.py
          verify_grant` in an order that restates `contracts/identity-schema.json
          grant.verification_order` = guard-protocol order unchanged, with no
          crypto in provenance (`test_no_second_crypto_implementation`);
          ordering is proven behaviorally (expiry precedes nonce precedes
          presenter).
        - The contract now carries the whole rule set: peer event vocabulary
          gained `grant_use`, `tracking-block` gained the optional `grant`
          field, and provenance-protocol.md gained a "Signed session grants"
          section. `make contract-test` green.
        - Knowledge writes go through `write_granted_note`: grant verified
          fail-closed first, then scope containment as prefix-of-path-parts
          (`grant_scope_allows`); in-scope lands at target with tracking.grant
          frontmatter + a signed note + a dual-signed hash-chained `grant_use`
          ledger block (`decision: granted`); out-of-scope is quarantined under
          `_raw/grant-quarantine/<grant_id>/` (`decision: quarantined`) — the
          vault keeps what it is given, authority is never widened. Nonces are
          consumed against the ledger, so a replay dies at the nonce check and
          two writes can never double-spend one grant.
        - Offline operator signing works with no Master running
          (`authz.py grant-mint/grant-verify --master-key`, key derived from the
          registry's `master_public_key_b64`); a delegated grant attributes the
          ledger block to the acting child session while authority traces to
          its `parent_grant_id`.
        - Unit-tested in `tests/test_grants.py` (29 tests): mint+verify happy
          path, mint refuses widen/unknown, forged/expired/not-yet-valid/
          nonce-reused/unknown-node/revoked/widen/malformed/node-mismatch all
          fail closed, scope containment (prefix-of-parts, empty scope allows
          nothing, `..` refused), in-scope write with full provenance, child
          session attribution, out-of-scope quarantine, forged-write refusal
          before touching the filesystem, replay consumption, overwrite
          refusal, chain integrity across uses, and the offline CLI.
        - Gates: peer suite 265 passed/1 skipped; fabric suite 687 passed/3
          skipped; `make contract-test` green; `make validate` green; ruff
          clean on the change set; `make bundle-verify ROLE=peer` green
          (standalone bundle runs grant-scoped writes from an extraction).
      - **Cursor**: `current: 4` (Step 4 — bridge server, persistent
        grant-keyed sessions, grant-checked `/vault/*` tools).

## Step 4 — Bridge: real server, persistent sessions, vault tools

- [x] **Step 4** — `sparknet-dispatch-bridge.py` becomes a server, because today
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
      - **Status**: done 2026-10-10. The bridge is now a real server on
        loopback 4096 with two personalities on one port: the **signed**
        envelope surface (`/v1/status` unchanged, fabric transport test still
        green — the plain surface never touches it) and a **plain** Open WebUI
        surface (`/v1/models` lists agents declared on disk —
        `.opencode/agents/*.md` ∪ `agents/*/` pods, so `chat`, `cyber`,
        `engineering`, `vault-curator` — never fabricated models that route to
        nothing). Every plain route is loopback-only and requires
        `Authorization: Bearer <base64url signed-grant>`; the grant is verified
        in guard-protocol order before anything is read or written, with one
        stable refusal vocabulary (`missing-grant` / `grant-refused` /
        `out-of-scope` / `bad-path`). Sessions are persistent and keyed by
        `grant_id` (`$SPARKNET_STATE_DIR/sessions/<grant_id>.json`, mode 0600,
        plain content turns — no content-hash identity), survive a restart
        (second server over the same state dir re-reads the conversation), and
        are readable only by the presenting grant. `/vault/*` tools — list,
        cat, capture, promote, patch — canonicalise every caller path (absolute
        and `..` refused) and stay inside the grant's `vault_scope` for reads
        *and* writes; mutations append a dual-signed hash-chained `grant_use`
        block through the one shared implementation (capture →
        `write_granted_note` with out-of-scope **quarantined** under
        `_raw/grant-quarantine/<grant_id>/`; promote/patch are new
        `promote_granted_note` / `patch_granted_note` on the same `_verified_grant`
        ladder — promote moves only pending raw/staged drafts, patch re-signs an
        exactly-once body edit so the note stays `valid`, neither relocates a
        stranger's signed note). The bridge names no ledger path at all — the
        anti-block-builder test's invariant is now "the bridge cannot write what
        it cannot name" (was "json.dump not in source"). `manifests/vault-openapi.json`
        describes the five vault tools + `/v1/models` for Open WebUI as HTTP
        tools (BearerAuth). DoD verified in `tests/test_bridge.py` (19 tests:
        models inventory, grant-required 401/unknown-bearer 403, persistent
        grant-keyed session + restart survival + cross-grant isolation, list/cat
        scope containment, capture→note+grant_use+chain+approval-blocks clean,
        quarantine, replay refusal, path-escape refusal, promote draft-in-scope
        + signed-note refusal, patch re-sign + exactly-one occurrence, OpenAPI
        operationIds). Gates green 2026-10-10: peer suite 284 passed/1 skipped,
        fabric 687 passed/3 skipped, contract-test, validate, ruff lint on the
        change set (my regions format-clean), `make bundle ROLE=peer` +
        bundle-verify rc=0, `make -C peer-node precommit` green after the daily
        journal was given its signature envelope. Honest boundary: the live Open
        WebUI handshake, `/v1/chat/completions` routing into a running agent,
        the permission card and `/session/:id` verbs are Steps 5–6; the bridge
        records and routes turns and never impersonates an assistant.
      - **Ledger authority unchanged**: the bridge serialises sessions, never a
        ledger block, and every mutation's block is built by
        shared/sparknet-provenance.py's own `build_grant_use_data`.

## Step 5 — Open WebUI surface

- [x] **Step 5** — The Open WebUI side of the node, configured as committed files
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
      - **Status**: done 2026-10-10. **DoD 1** — the declared surface lives in
        `manifests/agent-models.json` (schema `sparknet-agent-models/1`):
        `plan`/`build`/`vault`/`node-orchestrator`, each bound to the agent that
        owns it (`node-orchestrator` → `sparknet-node`). The bridge merges
        declared + discovered (`/v1/models`: declared roles first and
        `selectable`, on-disk pods after, unselectable) and `chat/completions`
        accepts any of them — the selectable model is never fabricated by the
        bridge and never a model that routes to nothing; the mounting of an
        agent paper to a running opencode process is Step 6's job. **DoD 2** —
        `POST /session/<grant>/permissions` raises a request
        (`perm-N`, status `pending`) and
        `POST /session/<grant>/permissions/<id>` resolves it `allow`/`deny`,
        grant-checked, session-scoped (403 `not-your-session`), persisted in the
        grant-keyed session file, and surfaced by `GET /session/<grant>` so a
        card can render and resolve it; refusals are stable (404 `no-session` /
        `unknown-permission`, 409 `already-resolved`, 400 `malformed`).
        **DoD 3** — unchanged from Step 4 (vault tools stay grant-checked;
        `test_bridge.py` still proves it); a per-session `POST
        /session/<id>/vault-access` toggle lets one grant work repeatedly in that
        session (mode `all`), or restores the one-shot default (`per-tool`),
        without changing scope, expiry, signature or the per-mutation audit.
        **DoD 4** — RAG is **enabled** on a **structure-only** corpus
        (`scripts/sparknet-rag-corpus.py`; `RAG_INGESTION_ENABLED: "true"`,
        `VECTOR_DB: chroma`, the node's own ChromaDB), never the personal pages;
        the surface env adds `MODEL_FILTER_ENABLED`, `MODEL_FILTER_LIST:
        plan,build,vault,node-orchestrator,masterspike`, `DEFAULT_MODELS: plan`. **DoD 5** — the whole surface is committed config: the model
        manifest, the ai-stack compose's bridge connection
        (`OPENAI_API_BASE_URLS: http://host.docker.internal:4096/v1` via
        `extra_hosts: host.docker.internal:host-gateway`) with a **deploy-time
        bearer** (`OPENAI_API_KEYS` expands `${SPARKNET_WEBUI_BRIDGE_GRANT:?...}`;
        no token committed, compose fails loudly while unset), and the
        `webui_surface` claims block in `manifests/ai-stack.json`. Verified in
        `tests/test_webui_surface.py` (11 tests: declared-first model list +
        agent binding, a declared role model in chat/completions, fail-closed
        without the manifest, the full permission lifecycle, deny path,
        refusals, cross-grant 403, **permission persistence across a bridge
        restart**, and the card path never touching the vault)
        + `tests/test_node_ai_stack.py` (surface env, deploy-time-key rule, the
        model manifest matching the model filter, compose parses with the grant
        var exported). Gates 2026-10-10: peer suite 298 passed/1 skipped,
        fabric 687 passed/3 skipped, contract-test, validate, ruff lint on the
        change set (bridge + new test file format-clean), `make -C peer-node
        precommit` green, `make bundle ROLE=peer` + bundle-verify rc=0. Honest
        boundary: the **live** Open WebUI handshake — a running webui container
        pulling the model surface, rendering a card and resolving it against the
        running bridge, including the bridge's loopback gate admitting the
        compose gateway — is `DoD: confirm: user`, exactly like the deployment
        it rides on; routing a conversation into a running agent is Step 6.

## Step 6 — Intra-node conversation passing

- [x] **Step 6** — Handing a conversation between the node's own agents.
      - **DoD**: (1) a conversation passes by keeping the session and changing
        agent — `POST /session/:id/message` accepts `agent`, so history survives
        the hand-off intact; (2) branching uses `POST /session/:id/fork` at a
        message and the original stays whole; (3) `GET /session/:id/children` and
        the share endpoints work; (4) the orchestrator's delegation and
        hand-off verbs are exercised against a real server, not asserted.
      - **Status**: done 2026-10-10. **DoD 1** — `POST /session/:id/message`
        takes `agent` + `role` + `content` (all required, else 400 malformed;
        unknown agent 400 unknown-model), appends the turn tagged with that
        agent to the same grant-owned session, and updates `session["agent"]` —
        the hand-off changes the routing agent, never the history; the session
        file survives on disk, so every earlier turn (and who handled it)
        stays. **DoD 2** — `POST /session/:id/fork` takes `at` (default: the
        last message; 400 malformed for out-of-range/non-int) and writes a
        child session `sessions/<id>-f<n>.json` carrying the shared prefix up to
        and including the fork message; the parent is only touched to grow its
        `children` list (verified: conversation untouched, pending permission
        cards do not branch). **DoD 3** — `GET /session/:id/children` lists the
        branch; the share verbs are `POST /session/:id/share` (kind must be
        `session-view` or `session-handoff` — `kb-*`/`full` are 400 because Step
        8 owns them), `GET /session/:id/shares` and
        `DELETE /session/:id/share/:share_id` (404 unknown-share,
        409 already-closed); a share starts `open`. **DoD 4** — the verbs were
        exercised against the **real** loopback bridge
        (`tests/test_conversation_passing.py`: a `ThreadingHTTPServer` on a
        pytest-owned port; `test_the_orchestrator_hand_off_runs_against_a_real_server`
        drives node-orchestrator → build → vault, then forks, lists children and
        shares — asserting the child file actually lands in the state dir), not
        asserted stubs. Ownership: the `/session/` gate was reworked from
        ``parts[0] == grant_id`` to *load the session, then compare
        `owner_grant_id`* — a fork's sibling becomes reachable by the same
        grant and by nobody else (404 no-session for a missing id, 403
        not-your-session for a foreign owner); the initial chat session is
        `session_id == owner_grant_id == grant_id`, so every pre-existing
        session test still passes unchanged. SESSION_SCHEMA bumped to
        `sparknet-session/2`; the plain surface grew `do_DELETE` (share
        close). Honest boundary: the verbs and their persistence are real, but
        *executing* a turn inside a live plan/build/vault opencode process
        (turning a routing decision into an agent's tool calls) remains a later
        step, as does the Step 8 sharing regime — this step records shares in
        `open` status; it never transfers them. Verified in
        `tests/test_conversation_passing.py` (13 tests) + a
        `conversation_passing` claims block in `manifests/ai-stack.json`.
        Gates 2026-10-10: peer suite 311 passed/1 skipped, fabric 687/3 skip,
        contract-test, validate, ruff lint on the change set
        (bridge format-clean), signed `/v1/status` transport test green,
        bundle-verify rc=0, `make -C peer-node precommit` green.
      - **Note**: this step uses only native opencode primitives. No custom
        passing mechanism is introduced, because none is needed — the earlier
        plan's conversation-hash identity is what Step 3 removes.

## Step 7 — `MASTERSPIKE`

- [x] **Step 7** — A model that carries the Master role and the llm-wiki format,
      without ever being able to sign anything.
      - **DoD**: (1) an Ollama `Modelfile` exists with the llm-wiki knowledge
        format inlined from `.opencode/skills/llm-wiki/SKILL.md` at
        `temperature 0.1`, plus `make model-build` / `make model-serve`; (2) a
        vLLM equivalent prompt artifact is generated **from the same source**, so
        switching inference backends cannot silently drop the format; (3)
        `make model-verify` fails if the two diverge or the source skill moves;
        (4) the model is selectable as an agent, and it holds no signing
        capability — authority is Ed25519 in the Master, never a prompt.
      - **Status**: complete 2026-10-10.
      - **DoD 1** — `scripts/sparknet-masterspike.py` generates
        `manifests/masterspike/Modelfile` (base `llama3.2`, `PARAMETER
        temperature 0.1`, `SYSTEM """` with the llm-wiki format body); Makefile
        gained `model-generate` / `model-build` (ollama create) / `model-serve`
        / `model-verify`.
      - **DoD 2** — `manifests/masterspike/vllm-system-prompt.md` embeds the
        byte-identical body, generated from the **one** source skill; both
        artifacts carry a `# source` / `# source-sha256` provenance header, so
        the exact revision that produced them is on record.
      - **DoD 3** — `make model-verify` regenerates both in memory and fails on
        any divergence, a removed artifact, a removed source, or a source that
        moved past generation (`tests/test_masterspike.py` exercises all five
        legs, the failure legs against `tmp_path` mirrors).
      - **DoD 4** — `masterspike` is selectable in `manifests/agent-models.json`
        (`agent: masterspike`, `capabilities: []`) and in the webui model filter;
        `manifests/masterspike.json` (schema `sparknet-masterspike/1`) declares
        `signing_boundary` — `capability: null`, `sign` explicitly not granted,
        structurally no key/signer-socket/state-dir. The fabric layout lint
        exempts the vLLM artifact as a verbatim copy of an excluded vendored
        source (`EXCLUDED_GENERATED` in `tests/test_layout.py`).
      - **Gates 2026-10-10**: peer suite 322 passed/1 skipped (incl.
        `test_masterspike.py`, 10 tests), fabric 687/3 skip, master parity,
        contract-test, validate, ruff lint clean on the change set, `make
        model-verify` ok, bundle-verify rc=0 (bundle ships the skill + artifacts
        and the suite passes there), `make -C peer-node precommit` green.
      - **Honest boundary**: this host has an `ollama` binary but no running
        daemon — the model is **declared, not built**. Pulling `llama3.2`,
        `ollama create sparknet-masterspike`, serving it, and selecting it in a
        live Open WebUI are target-node steps: `DoD: confirm: user`.

## Step 8 — Inter-node sharing and the governance regime

- [x] **Step 8** — Two users on two nodes share knowledge and sessions, under a
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
      - **Status**: complete 2026-10-10.
      - **DoD 1–4** — `services/sparknet-share-regime.py` implements the share
        conversation as a pure, in-process, message-dispatched state machine
        that reads the **machine contracts** (`capabilities.json` share_types +
        share_clocks, `overlay.json` governance_mode + data-plane stanza,
        `ports.json` share ports) and **refuses to run if they are missing**, so
        it cannot drift from prose. `offer()` opens a 1 h invite; `accept()`
        requires the receiver to name a type and enforces narrowing-only
        (`full.includes` is the only partial order); all five types come from the
        contract; a temporary share idle-closes while a permanent one survives
        idle (both end at the absolute cap); the four clocks default to
        1 h/20 m/2 h/10 h and are injected in seconds by the tests.
      - **DoD 5–6** — `mode_for()` recomputes governance from the registry node
        count on **every** request (never cached): `solo` for one node (drafts
        allowed, acceptance refused), `peer_pair` (symmetric, both endpoints
        `flagged`, **no** ratification clock — a Master arbitration is refused
        `no-arbiter`), `master_governed` (3+) where a Master `ratify` moves both
        endpoints to `authenticated` with the `flagged` retained for audit; a
        third node joining mid-share re-evaluates the live share rather than
        grandfathering it. `kb-share`/`full` reconciliation is additions and
        conflicts only — a divergent page is a review item, never silently
        overwritten — recorded in both directions.
      - **DoD 7–8** — `render_stanza()` emits a declarative, time-boxed
        data-plane stanza (by-kind port 4201/4202, mtu 1400, keepalive 25, config
        mode 0600, single/`/32` AllowedIPs, nftables fragment) and returns the
        authority-plane one-address invariant (`10.100.0.1/32`) unchanged; the
        host acts on returned actions (`stanza-up`, `teardown-stanza`,
        `isolate-data-plane`, `re-evaluate`). Non-ratification (2 h silence, or a
        late answer) is a refusal that severs the fabric data plane
        (`isolate-data-plane`) while the authority channel stays; every share,
        permanent included, ends at the 10 h absolute cap.
      - **Wiring** — the bridge's `/session/:id/share` verbs delegate to the
        engine (`SHARE_KINDS` is now the five contract types), so a created share
        on this solo node is an honest **invite** (a 1 h draft), not a false
        `open`; Step 6's `test_share_refuses_kinds_that_belong_to_step8` is
        replaced by `test_share_verbs_now_accept_all_five_step8_kinds`.
      - **Gates 2026-10-10**: peer suite 352 passed/1 skipped (incl.
        `test_share_regime.py`, 26 tests, in-process node pairs), fabric 687+
        passed/3 skip, master parity, contract-test, validate, ruff check clean
        on the change set, bundle-verify ROLE=peer rc=0 (bundle ships the engine
        + manifest, suite passes there), `make -C peer-node precommit` green.
      - **Status/manifest** — `manifests/share-regime.json` (schema
        `sparknet-share-regime/1`, status `wired, not deployed`) declares the
        types, clocks, actions and authority boundary; a test asserts the
        manifest and the engine agree.
      - **Honest boundary (DoD: confirm: user)** — this host has one peer, no
        Master and no second node, so the two-node and three-node behaviour is
        proven with **in-process node pairs** and nothing more. The WireGuard
        `mtu`/`keepalive` and stanza application, the live 4201/4202/4203
        listeners (all spec-only), the Master's ratification service, the review
        agents and the Master-hosted evidence repo are target-node legs —
        unbuilt, and never reported as if deployed.


## Step 9 — Node AI stack (cluster design item 3, slice A: own inference, committed)

- [x] **Step 9** — The node's own AI stack as committed config, so it survives a
      redeploy: its own Ollama and its own Open WebUI, bound to loopback, per
      `../CLUSTER_INFERENCE_DESIGN.md` adoption item 3.
      - **DoD**: (1) `configs/ai-stack/docker-compose.yml` declares exactly the
        two services — `ollama` (`:11434`, OpenAI-compatible at `/v1`) and
        `open-webui` (`:8080`, `OLLAMA_BASE_URL` = the compose service
        `http://ollama:11434`, `RAG_INGESTION_ENABLED=false`, `WEBUI_AUTH=true`,
        signup disabled) — both bound to `127.0.0.1` only, no GPU declared;
        (2) `manifests/ai-stack.json` declares the bound claims (loopback-only,
        own-server-only, no authority, no key/signer/state mount) and
        `tests/test_node_ai_stack.py` asserts them read off the files — not
        assumed; (3) `make ai-stack-config` runs `docker compose config
        --quiet` with no images pulled; (4) every port is loopback-only, no
        overlay or admin port is claimed, and no `sign` / `cluster-dispatch` /
        `cluster-serve` capability is reachable from the stack; (5) `make
        test-all`, contract-test, lint, validate and `make bundle-verify
        ROLE=peer` stay green with the stack bundled; (6) task ingress is
        deferred to design item 5 (cluster client + reducer) and recorded, not
        silently dropped.
      - **Status**: complete for the composed/declared slice; 11 new tests pass
        (peer suite 215 passed, 1 skipped); `docker compose config --quiet`
        green; bundle verified with `configs/ai-stack/` +
        `tests/test_node_ai_stack.py` inside. **The deployed healthcheck —
        pulling and running the images on a target node without a GPU — is not
        done and is not claimed.**
      - **DoD: confirm: user** for the running stack; the other five DoD items
        were auto-verified.
      - **Note**: `open-webui` on `:8080` loopback-only; 8080 is the Master's
        admin port on the Master, but here it is a node-local socket never
        exposed to the overlay. **Superseded by Step 10** (operator decision
        2026-10-10): the port, the Ollama wiring and the RAG posture were revised;
        the no-authority / no-key / sign-withheld boundaries Step 9 set are
        unchanged.

## Step 10 — Node AI stack revision (operator decision: exposed + firewalled, structure-only RAG, session toggle)

- [x] **Step 10** — Revise the node's committed AI stack for the operator's
      2026-10-10 decisions, without weakening the boundaries Step 9 set.
      - **DoD**: (1) `configs/ai-stack/docker-compose.yml` declares the single
        `open-webui` surface on `0.0.0.0:8181` (LAN + overlay, `PORT=8181`
        on the **host network** — the operator chose `network_mode: host` on
        2026-10-11 so the surface can reach the loopback-only dispatch bridge,
        which a bridge-network container cannot), standing on the node's infra
        Ollama (`http://127.0.0.1:11434`) and infra ChromaDB
        (`http://127.0.0.1:18000`), with a deploy-time grant
        (`${SPARKNET_WEBUI_BRIDGE_GRANT:?…}`) and the Step 5 model surface;
        (2) RAG is **enabled** but scoped to a **structure-only** corpus built
        by `scripts/sparknet-rag-corpus.py`, and `tests/test_rag_corpus.py`
        proves no excluded path (user.md, 02_Daily, .state, keys) can reach it;
        (3) the bridge gains `POST /session/<id>/vault-access` (`all`|`per-tool`)
        so one grant works repeatedly in a session, defaulting to one-shot, and
        `test_bridge.py` proves all four cases; (4) `manifests/ai-stack.json` +
        `manifests/vault-openapi.json` declare the revised wiring (published +
        firewalled, host-net bridge URL, the toggle path) and
        `tests/test_node_ai_stack.py` asserts them read off the files; (5) the
        `SPARKNET-FW` firewall (v4 `INPUT`+`DOCKER-USER`, v6 `INPUT`, systemd
        unit ordered after docker) ships as `scripts/sparknet-firewall.sh` +
        `systemd/sparknet-firewall.service`, `tests/test_firewall.py` holds the
        ports/rules in sync, and the exposure tradeoff is written in
        `CLUSTER_INFERENCE_DESIGN.md`.
      - **Status**: complete, deployed live on **both nodes 2026-10-11**.
        Committed config, manifests, bridge toggle, corpus builder + tests and
        the design-doc record were applied in the previous session; this session
        deployed them: (a) **Python 3.8 fix** — the bridge and shared runtime
        gained `from __future__ import annotations` + a `collections.abc`
        fallback for `Callable`, because the nodes run Ubuntu 20.04's Python
        3.8.10 and the modules used PEP 585/604 (bundle-only modules already
        had the future import; `sparknet-dispatch-bridge.py`,
        `sparknet-liveness.py`, `sparknet-election.py` did not);
        (b) **grants** — `webui-node131-20261011` and `webui-node133-20261011`
        minted on `.131` with the cluster2 operational key into the deployed
        registry, verified incl. a failing presenter check; bearer tokens stored
        0600 in each node's `$STATE/webui-bridge-grant.token`;
        (c) **Open WebUI** on both nodes (`0.0.0.0:8181`, host net) with the 9
        declared agent models reachable and 26 local Ollama models; first admin
        `admin@sparknet.local` created headlessly from deploy-time
        `SPARKNET_WEBUI_ADMIN_*` (passwords stored 0600 in
        `$STATE/webui-admin-password`); (d) **RAG** — the structure-only corpus
        (17 files) ingested into the `sparknet-structure` knowledge base and
        embedded into each node's ChromaDB on top of what the committed config
        declares; (e) **firewall** installed + enabled both nodes (loopback/LAN/
        overlay/docker/link-local allowed; global-v6 → 8181 dropped), proven to
        re-apply after a docker restart via `PartOf=docker.service`; (f)
        **MASTERSPIKE** built via `ollama create sparknet-masterspike` on both
        nodes (2.0 GB; answers in the llm-wiki voice). End-to-end verified: WebUI
        → Ollama chat, WebUI → bridge agent route (records sessions), overlay
        `.133→.131:8181` 200, LAN/SSH intact, firewall drop on public v6.
      - **DoD: confirm: user** for the parts a file cannot prove on a live node —
        that the surface answers on the nodes as described, and that the
        per-session vault-access toggle behaves in the real UI. Everything else
        was verified on the targets this session.

## Step 11 — Per-user knowledge vaults (`peer-<user_id>`) and user KBs

- [x] **Step 11** — Every approved Open WebUI user on a node gets a copy of the
      `peer-node` **knowledge-vault slice** at `/srv/sparknet/knowledge/peer-<user_id>/`,
      created when the node admin Accepts their signup (role `pending` → `user`),
      connected to that user's Open WebUI as a knowledge base they own and grow.
      Design: `../PER_USER_VAULTS_DESIGN.md` (fabric root).
      - **DoD**: (1) `services/sparknet-vault-provisioner.py` (systemd unit)
        watches `GET /api/v1/users/` with the node's admin token, triggers on a
        `pending`→`user` role change, and records provisioning state under
        `/srv/sparknet/knowledge/.state/`; (2) the materialiser copies only the
        knowledge-vault slice (numbered categories + shims + `AGENTS.md` +
        `docs/` + schema; **excluded**: `.state/`, `blockchain/`, `services/`,
        `scripts/`, `container/`, `configs/`, `tests/`, `manifests/`, `.git/`,
        template `.env`), generates a fresh `.env` with `VAULT_PATH` → the copy,
        and `make keys-check` stays clean after provisioning; (3) the KB wiring
        creates the user's KB with the admin token and grants **read** to that
        user (`access_grants` flat dicts, `principal_type: user`), embeds the
        vault markup into the KB, defaults the user's own chat model to it
        (`meta.knowledge`), and `tests/test_vault_provisioner.py` proves the
        grant shape and exclusion list on a fixture vault; (4) a re-indexer
        records a per-copy content digest and re-embeds changed pages; (5) the
        `master-<id>` copy rule is encoded (elected-Master + admin signed in)
        and is **inactive on `.131`** — users on the Master node and admins on
        peer nodes all get `peer-` copies; (6) tests green, `make contract-test`
        + `lint` green, bundle-verify rc=0.
      - **Status**: in progress. Design doc `PER_USER_VAULTS_DESIGN.md` written;
        Open WebUI 0.12.0 API surface confirmed on `.131` (KB create,
        `knowledge/<id>/file/add`, `access_grants` dict shape, admin-token
        grants). Deployment on a real signup+Accept cycle is
        `DoD: confirm: user`.

## Step 12 — Node admin agent: auth events + pending-signup supervision

- [ ] **Step 12** — Give each peer node a local, stdlib-only agent that turns
      the node's own Open WebUI into an approval-mediated surface the Master can
      supervise: signup is enabled with accounts landing `pending`, and the
      agent ingests the auth-event webhooks so signups and successful logins are
      recorded on the node.
      Design: `../ADMIN_FEDERATION_DESIGN.md` (fabric root, fabric Step 29).
      - **DoD**: (1) `configs/ai-stack/docker-compose.yml` enables signup with
        `DEFAULT_USER_ROLE: "pending"` and keeps no committed secret;
        (2) `services/sparknet-admin-agent.py` is stdlib-only, `--help` exits 0,
        and runs relocated; it serves a loopback listener that appends
        normalised `auth.*`/`user.role_updated` events to a 0600 JSONL spool
        under `$SPARKNET_STATE_DIR/admin-events/`, and can register the node's
        own Open WebUI event webhooks via `POST /api/events/webhooks`;
        (3) the node's WebUI admin is documented as a machine service account
        (password site-local, 0600), so no human logs in as admin on a peer;
        (4) `tests/test_admin_agent.py` proves the spool shape, the event
        allow-list, the webhook registration body, and the pending-only
        provisioning gate; (5) `manifests/ai-stack.json` records the new
        approval posture; (6) tests + `make contract-test` + `lint` green,
        bundle-verify rc=0.
      - **Status**: in progress, opened 2026-10-12. The signed cross-node route
        (`admin-supervise` + bridge `/admin/*`) is fabric Step 29 Phase 2, not
        this step; live deployment is `DoD: confirm: user`.

## Next

Steps 2–8 are the node's build-out: container, grants, bridge, Open WebUI,
conversation passing, `MASTERSPIKE`, then the sharing regime. Step 2 is placed
immediately after the fabric's contract steps so the node becomes deployable
before it becomes clever.

`current:` moves one step at a time, and only after a step's DoD is verified.

Master-side work is tracked in `../master-node/plans/MASTER_BUILD_PLAN.md` —
do not duplicate it here. Fabric-level contract and truthfulness work is tracked
in `../PLAN.md`.
