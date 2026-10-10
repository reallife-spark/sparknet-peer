# The node's own AI stack, as committed config.

This directory carries the declarative slice of adoption item 3 in
`CLUSTER_INFERENCE_DESIGN.md`: a node's **own model server** and **own chat
surface**, configured here so the stack survives a redeploy.

## What is here

| File | Holds |
|---|---|
| `docker-compose.yml` | `ollama` (the node's own model server) + `open-webui` (the node's own chat surface, pointed at that Ollama). Both bind `127.0.0.1` only. |
| `../manifests/ai-stack.json` | the bound claims (loopback-only, own-server-only, no authority) that `../tests/test_node_ai_stack.py` asserts against the compose file. |
| `../tests/test_node_ai_stack.py` | wiring assertions: every port loopback-only, Open WebUI on its own Ollama, RAG off, no GPU declared, no key/signer/state-dir mount. |

## The rules this file exists to enforce

1. **Own inference is free; delegated inference is gated.** A node's own prompts
   need no capability and never leave these containers. Accepting a *signed
   subtask* is the task ingress + cluster client of adoption item 5 — it is not
   here, which is exactly why nothing in this file can yet be a vector for
   delegated inference.
2. **The vault is the only knowledge base.** `RAG_INGESTION_ENABLED=false`,
   the same rule as the Master admin surface.
3. **GPU is optional and declared at deploy, never in this file.** A node with a
   GPU adds a device reservation on the host; a node without one runs the same
   stack.

## Status

**Declared, not deployed.** Nothing here binds: no images are pulled on this
host. Running the stack on a target node — pull the images, bring the services
up, confirm both surfaces answer with no GPU present — is `DoD: confirm: user`.

Validate the compose without pulling anything:

```sh
make -C ../../.. ai-stack-config   # or, from the peer role root:
make ai-stack-config
```