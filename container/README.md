# The node container — a deployable opencode node

This directory carries peer PLAN **Step 2**: the base container that turns a
node into something that can be deployed and recreated without dropping its own
tunnel. The AI stack is NOT here — `../configs/ai-stack` is the inference
layer. This is the node itself.

## What is here

| File | Holds |
|---|---|
| `Dockerfile` | the peer-node image: pinned opencode per `TARGETARCH`, the node's own bundle at `/srv/sparknet`, a venv, **no CUDA**. |
| `entrypoint.sh` | regenerates `.env` at boot (nothing path-baked), then execs. |
| `healthcheck.sh` | node is healthy when pinned opencode answers; GPU presence never flips health. |
| `anchor/Dockerfile` | the tunnel sidecar: `wireguard-go` **built from source** (official GitHub mirror pinned at tag `0.0.20230223`, commit `21636207` — the vanity `golang.zx2c4.com` module domain 404s), userspace (host kernels have `CONFIG_WIREGUARD=n`). |
| `anchor/entrypoint.sh` | brings up `wgcore` when a rendered config is mounted; runs inert (namespace owner only) otherwise. |
| `../configs/container/docker-compose.yml` | app joins the anchor's namespace; `.state` is a host bind mount; no-GPU healthcheck. |
| `../scripts/sparknet-chattr-preflight.sh` | proves `chattr +i` holds on the target filesystem **before** anything relies on it. |
| `../tests/test_node_container.py` | wiring assertions read off the files above. |

## The three load-bearing rules

1. **The tunnel outlives the app.** The anchor owns the network namespace; the
   app attaches with `network_mode: service: anchor`. Killing the anchor drops
   the overlay; recreating the app leaves it standing. Anchor interface is
   `wgcore`, never `wg0/wg1`, so a containerized node cannot collide with the
   host's own overlay interfaces.
2. **`.state` is a host bind mount, never a named volume.** Keys survive app
   recreation by construction and never leak into image layers.
3. **No GPU is required to run.** GPU changes throughput only; the committed
   images declare none and the healthcheck ignores it.

## Build and run

```sh
make container-build                 # make dist, stage the bundle, buildx both arches (registry-ready)
make container-load                  # build the HOST arch with --load, for local runs/tests
make container-config                # docker compose config --quiet
make container-preflight             # chattr +i proof on the .state filesystem (needs root)
make container-up                    # compose up (preflight first on a node)
make container-health                # per-service health
make container-down
```

`container-up` depends on the preflight passing, because the peer's immutable
protections are only real where the filesystem honours them. On this build host
the preflight will typically refuse (no root for `chattr`) — that is the check
working as designed, not a defect.

## Honest boundary

The committed images and wiring are real and built; a **tunnel to a real peer
node is not**. The anchor's inert mode is what runs anywhere without a rendered
config. Live WireGuard connectivity, `chattr +i` on the deployed node's
filesystem, and the compose healthcheck on a target node are
`DoD: confirm: user` — the peer charter forbids claiming them from this host,
and this README does not. `tests/test_node_container.py` proves the *declared*
wiring is coherent, exactly as `test_node_ai_stack.py` proves the AI stack's.