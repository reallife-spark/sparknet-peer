---
title: Per-vault memory
updated: 2026-11-02
status: partially-built
---

> [!warning] **Built and locally tested; live node legs are pending operator confirmation.**
> The contracts, Go store, gateway, node wiring and adapters are implemented and
> covered by the local gates. A live node start, browser capture and a live Master
> read-through are `DoD: confirm: user`.

# Per-vault memory

The peer's memory plane is one Go service and one store per vault:

| Layer | What it is |
|---|---|
| `sparknet-memory` | Go service on loopback `4110`; stores episodic, semantic, procedural, short-term and shared records plus delegate runs |
| `sparknet-memory-gateway.py` | Python authority front door on loopback `4093`; verifies a signed session grant or Open WebUI session and narrows to one vault |
| OpenCode MCP | `sparknet-memory` MCP server in `opencode.json`; search, remember, status, forget and project-delegate tools |
| Open WebUI filter | injects scoped recall and captures turns through the same gateway |
| Console | `Memory` page, read-only counts, retention and policy |

## Vault identity

Memory is keyed by `vault_id`, derived from the node id and the canonical vault
path. The role vault, a user's `peer-<id>` vault and a project vault are separate
stores. Opening the same vault in OpenCode and Open WebUI returns the same
records; a query cannot cross a vault.

## Tokens

The setup script writes three `0600` tokens under `$SPARKNET_STATE_DIR`:

- `memory.gateway.token` — shared by the Go service and the gateway;
- `memory.local.token` — the console, CLI and OpenCode MCP adapter;
- `memory.webui.token` — the Open WebUI filter service account.

They are not fabric authority and never leave the node. The Go service trusts
only the gateway token.

## Controls

Capture, retention, suppression, deletion, export and sharing are owner
controls. The Master may read content only through the signed, audited
`memory-supervise` read-through; it keeps no mirror. The user's Memory page
records those reads.

## Index the vault

`make memory-index ROLE=peer` indexes `04_Evergreen/`, `05_Knowledge/` and
`06_Projects/` into semantic memory. It excludes `00_System`, daily notes,
sources, raw captures, state and inbox. With an embed endpoint and Qdrant
configured it writes `sparknet-memory`; otherwise retrieval stays lexical.

## Projects and reports

`make project-new PROJECT=alpha TITLE="Alpha"` creates a signed `PROJECT.md` and
`PLAN.md`. `make project-run` hands one bounded task to the delegate broker (set
`GOSPARK_MEMORY_OPENCODE_CMD` to the installed harness command, for example
`opencode run --agent project-orchestrator {task}`);
`make project-report RUN=<id>` files a signed report after the operator chooses
it. Delegates never write `REPORTS/`.

## Share

`make memory-export ROLE=peer OUT=bundle.json` writes a
`sparknet-memory-bundle/1`. A `session-handoff` or `kb-share` may carry it; it
stages under `01_Inbox/share-<id>/` and is imported only after acceptance. It
holds no short-term/cache, secrets or another user's records.

## Troubleshooting

- `make memory-check` runs the gateway, Go store and delegate tests.
- `curl 127.0.0.1:4110/healthz` checks the Go service.
- The gateway logs a refusal with a stable code; a missing token is a
  configuration fault, not an empty result.
- Live node starts and browser recall/capture are `DoD: confirm: user`.
