# The node's own AI stack, as committed config.

This directory carries the declarative slice of adoption item 3 in
`CLUSTER_INFERENCE_DESIGN.md`: a node's **own chat surface** standing on its own
infra **model server** and **vector store**, configured here so the stack
survives a redeploy.

## What is here

| File | Holds |
|---|---|
| `docker-compose.yml` | `open-webui` (the node's own chat surface) on the **host network** (`network_mode: host`), pointed at this node's infra Ollama (`http://127.0.0.1:11434`), its own Qdrant server (`http://127.0.0.1:6333`, loopback only) and the **OpenCode engine** at `http://127.0.0.1:4204/v1`. Answers on `0.0.0.0:8181` (`PORT`) behind the `SPARKNET-FW` host firewall. |
| `../manifests/ai-stack.json` | the bound claims (published-but-firewalled, own-server-only, structure-only RAG, no authority) that `../tests/test_node_ai_stack.py` asserts against the compose file. |
| `../tests/test_node_ai_stack.py` | wiring assertions: the one published port, Open WebUI on its own Ollama, structure-scoped Qdrant RAG, the Step 5 bridge surface, no GPU declared, no key/signer/state-dir mount. |

## The rules this file exists to enforce

1. **Own inference is free; delegated inference is gated.** A node's own prompts
   need no capability and never leave these containers. The OpenCode engine is
   the local execution path for those prompts; each user's sandbox mounts only
   that user's vault and has no key, ledger or role-state mount. Accepting a
   *signed subtask* remains the task ingress + cluster client of adoption item 5.
2. **Knowledge is structure-only.** RAG is **on**, but the corpus is the shape of
   the fabric — contracts, charters, taxonomy, design docs — never the vault's
   personal pages. `scripts/sparknet-rag-corpus.py` is the allow-list and a test
   proves no excluded path reaches it. (The Master admin surface, a different
   surface answering a different question, keeps `RAG_INGESTION_ENABLED=false`.)
3. **Exposure is a decision with a rule attached.** The chat surface answers on
   the node's LAN and overlay addresses; a `SPARKNET-FW` host firewall keeps it
   off the public internet. Binding the surface without that rule is the failure
   mode, so the two ship together.
4. **The bridge stays loopback-only.** Open WebUI shares the host network
   namespace so it can reach `127.0.0.1:4096`; a bridge-network container cannot
   reach a loopback-only host listener (`host-gateway` resolves to the docker0
   gateway, not `127.0.0.1`). Host networking is what keeps the agent surface off
   the wire.
5. **GPU is optional and declared at deploy, never in this file.** A node with a
   GPU adds a device reservation on the host; a node without one runs the same
   stack.

## Status

**Live on agx64 2026-11-05, OpenCode 2.2.5 rollout 2026-11-06.** Open WebUI is
`v0.13.0`; the live OpenCode sandbox is `2.2.5` and the engine connection is
live. The host firewall and browser interaction remain target-host checks; the
service and API path are dated live evidence.

Validate the compose without pulling anything:

```sh
make -C ../../.. ai-stack-config   # or, from the peer role root:
make ai-stack-config
```

Qdrant is deployed from `docker-compose.qdrant.yml`; generate `SPARKNET_QDRANT_API_KEY` under the node state dir at 0600 and export it for compose. The API key is never committed.
