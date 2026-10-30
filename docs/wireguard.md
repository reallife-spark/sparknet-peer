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

`make anchor` is `scripts/sparknet-wireguard-anchor.sh`, which renders, brings
the interface up through `scripts/sparknet-wg-bringup.sh` (the kernel module when
present, `wireguard-go` when not — see below), and then applies `chattr +i`. The
systemd unit `sparknet-wireguard-anchor.service` calls the same script through a
generated shim, so the unit contains no vault path and stays relocation-safe.

## Hosts without the kernel module

The AGX hosts this overlay runs on ship `CONFIG_WIREGUARD=n`, so `wg-quick up`
cannot create the interface — and its userspace fallback is unreliable: it starts
`wireguard-go`, does not wait for the UAPI socket, and can hang in `wg setconf`
before the interface is configured, leaving the node with no overlay after a
reboot. `scripts/sparknet-wg-bringup.sh` does the sequence deterministically:
probe with `ip link add … type wireguard`; on failure start `wireguard-go`, wait
for `/var/run/wireguard/<iface>.sock`, then `wg setconf` (capped by `timeout`
because it can apply *and then* hang, with the result confirmed by `wg show`),
then `ip addr add` / `ip link set mtu … up` and any routes.

The unit reads its secrets from `EnvironmentFile=-/etc/sparknet/wireguard.env`
(`SPARKNET_WG_*`), so the anchor self-restores at boot with no operator action.

## Why the config is immutable

`chattr +i` on `wg1.conf` means this node cannot remove its own Master link. That
is the "cannot be removed or blocked" requirement: an operator without root
cannot edit the file, and a compromised local process cannot either.

**Both copies are pinned.** The anchor pins the authoritative state-dir config
(`$SPARKNET_STATE_DIR/wireguard/<iface>.conf`) *and* the conventional
`/etc/wireguard/<iface>.conf` copy — the path tooling and an operator expect, and
the path the guard's protected-path manifest names. Pinning only one would leave
the other a writable route to the live overlay. If any step between the unlock and
the final pin fails, the anchor's `EXIT` trap re-pins whatever exists, so a
partial run never ends with the config writable.

The anchor requires `SPARKNET_WG_INTERFACE` and refuses to guess it. A default
such as `:-wg0` would silently bring up a second, wrong interface when the env
file is missing; the deployment env file must name it (e.g. `wg1`).

### The Master pins too, and re-renders to change peers

The Master runs `sparknet-master-wireguard-anchor.sh`, which pins **its** overlay
config the same way — the Master is a peer that won an election, so it follows the
same anchor discipline. The consequence is that a peer stanza cannot be added or
removed by editing the pinned config in place: `wg set wg0 peer … remove` changes
the *running* interface immediately (and is unaffected by the pin), but making a
change persist across a reboot means re-rendering the config and re-running the
root-owned anchor, which unlocks, rewrites and re-pins both copies. That is the
intended workflow: an overlay-topology change is an operator action that goes
through the anchor, not a quiet in-place edit.

The Master retains control from the other side:

```bash
wg set wg0 peer <NODE_PUBLIC_KEY> remove      # revoke (immediate, survives the pin)
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

The WLAN guard is a **template only** (`scripts/sparknet-wlan-guard.sh`): it
emits a spec nftables fragment, does not apply it, and `nft` is absent on the
fleet. It is not the deployed egress control. The peer's live control is the
iptables `SPARKNET-FW-IN`/`SPARKNET-FW-FWD` firewall, which allows loopback,
LAN, the `wg1` overlay, Docker bridges and link-local for the published surfaces.
It does not pin all WLAN egress to `10.100.0.1`. A revoked node keeps its local
vault and LAN surface working but cannot receive a signed instruction over the
overlay, so it is inert for guarded actions rather than offline.

## WLAN uplink stability (AGX hosts)

The management uplink is `wlan0`. On the AGX hosts the Broadcom driver enables
802.11 power save by default, and a sleeping station intermittently misses
replies — observed live as `No route to host` / stale ARP against `.21` while the
node was in fact up on the cable and the overlay. The fix is applied on both
hosts and persists across reboots:

```bash
sudo iw dev wlan0 set power_save off          # immediate
sudo nmcli con mod <con> wifi.powersave 2      # persisted (2 = disable)
```

This is host-side OS tuning, not vault content, so it lives here as an operator
note rather than in `sparknet-node-setup.sh` (which stands up the node
container, not host networking).

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
- [ ] iptables `SPARKNET-FW-*` egress rules taking effect (the `wlan-guard` template's nft fragment is not applied)
- [ ] KillSwitch teardown and recovery

Everything that *can* be checked without root is covered by
`tests/test_wireguard.py`: template placeholders, render output, key-shape
validation, `/32` enforcement, dry-run purity, that no private key material
appears in tracked files, and that the anchor delegates to the userspace bring-up
helper rather than `wg-quick`.

The userspace bring-up, the `chattr +i` pin and the cold-boot recovery path were
verified **live** on the AGX pair (2026-10-19): with `wg1` deleted, restarting
`sparknet-wireguard-anchor.service` rebuilds the interface through `wireguard-go`
and the handshake returns within ~10 s; and a **real reboot of each host**
restored `wg1` unattended (peer `10.100.0.2/32`, master `10.100.0.1/24`), the
handshake returning without operator action. The development host still cannot
run any of it, so the checklist above stays a target-node checklist.

Both anchor units carry `Before=docker.service`, so the overlay address exists
before the Docker daemon starts any container that binds it. Without that
ordering the Master's admin surface (`10.100.0.1:8088`) failed at boot with
`bind: cannot assign requested address` (exit 128).