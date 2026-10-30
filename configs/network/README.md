# Network configuration (current)

The deployed peer uses the dedicated `10.99.0.0/30` Ethernet underlay with
`wg1` endpoints pinned to it, plus `wlan0` as the LAN/uplink path. The peer's
overlay address is `10.100.0.2/32`; the Master anchor is `10.100.0.1`.

WLAN emergency routing is a **userspace NetworkManager/routing** concern on this
fleet, not nftables: `nft` is not installed, and the old
`configs/nftables/wlan-emergency.rules` template is legacy and never applied.

## Host firewall

The peer's host firewall is **iptables**, installed by:

- `scripts/sparknet-firewall.sh install|status|flush`
- `systemd/sparknet-firewall.service` (`PartOf=docker.service`, re-applies after
  a Docker restart)

It builds `SPARKNET-FW-IN` and `SPARKNET-FW-FWD` and protects the published
surfaces `8181`, `11434` and `18000`. Allowed sources are loopback, the LAN, the
`wg1` overlay, Docker bridges and link-local. It does not change the default
`INPUT` policy.

Do not describe this peer as using `nftables`, ConnectX-7/RoCEv2, DMA-BUF
zero-copy or MTU 9000; none of those are present on the deployed AGX pair.
