# WireGuard Master Anchor

The Master Anchor is this node's hard dependency on the Master. When it is up,
the node has a route to the Master and is subject to Master-signed authorization.
When it is down, the node degrades to **standalone mode**: the vault still works,
but the guard refuses to bind and refuses to apply anything.

## Topology

| Role    | Overlay address | Notes                                     |
|---------|-----------------|-------------------------------------------|
| Master  | `10.100.0.1`    | the anchor; peers are registered with it   |
| Node    | `10.100.0.x/32` | one address per node, from the identity file |

Every node keeps exactly **one** peer: the Master. Peer-to-peer edges are
authorized *layers* on top (see [node-authorization.md](node-authorization.md)),
not extra WireGuard peers.

## Provisioning a node

```bash
# 1. Key material. Writes to $SPARKNET_STATE_DIR/keys at 0700; never committed.
make keys                       # or: scripts/sparknet-keygen.sh --role node

# 2. Put the node's WireGuard public key in manifests/node-identity.json and its
#    address in overlay_ipv4. The Master then registers the peer:
#      wg set wg0 peer <NODE_PUBLIC_KEY> allowed-ips 10.100.0.x/32 persistent-keepalive 25

# 3. Preview the rendered config. Dry-run by default; APPLY=1 writes at 0600.
make wg-render
make wg-render APPLY=1

# 4. Bring it up and pin it (root).
make anchor
```

`make anchor` is `scripts/sparknet-wireguard-anchor.sh`, which renders, runs
`wg-quick up`, and then applies `chattr +i`. The systemd unit
`sparknet-wireguard-anchor.service` calls the same script through a generated
shim, so the unit contains no vault path and stays relocation-safe.

## Why the config is immutable

`chattr +i` on `wg0.conf` means this node cannot remove its own Master link. That
is the "cannot be removed or blocked" requirement: an operator without root
cannot edit the file, and a compromised local process cannot either.

The Master retains control from the other side:

```bash
wg set wg0 peer <NODE_PUBLIC_KEY> remove      # revoke
```

Revocation takes effect immediately on the Master and, on the node, on the next
packet — the node's config is untouched, it simply stops reaching the Master.

## Teardown

There is deliberately **no** `ExecStop` and no `down` path in the anchor script.
Tearing down the anchor is either:

- the Master revoking the peer, or
- the root-gated KillSwitch (`systemd/sparknet-killswitch.sh`), which is
  documented `ROOT-GATED` and exists for emergency containment.

## Egress restriction

The WLAN guard pins WLAN egress to `10.100.0.1` only
(`scripts/sparknet-wlan-guard.sh`). This is intentional: a revoked node keeps its
local vault working but has no other network path.

## Secrets

- Private keys live only in `$SPARKNET_STATE_DIR/keys` (mode `0700`), or wherever
  `SPARKNET_WG_*` environment variables point. They are **never** committed.
- `configs/wireguard/wg0-node.conf.template` is tracked and contains only
  placeholders.
- The render script validates key shape (`wg genkey` base64, 32 bytes) and refuses
  an address that is not a `/32` in the overlay before writing anything.
- `make install-config` remains permanently write-free; it only shows the `/etc`
  diff.

## Not verified on the development host

The development host has no `wg`, no `wg-quick`, no `nft`, no WireGuard kernel
module and no `CAP_NET_ADMIN`. These require a privileged target node and are
**not** claimed as verified:

- [ ] live handshake and `PersistentKeepalive` behaviour over the real uplink
- [ ] `chattr +i` enforcement on the rendered config
- [ ] `nftables` egress rules taking effect
- [ ] KillSwitch teardown and recovery

Everything that *can* be checked without root is covered by
`tests/test_wireguard.py` (25 tests): template placeholders, render output,
key-shape validation, `/32` enforcement, dry-run purity, and that no private key
material appears in tracked files.