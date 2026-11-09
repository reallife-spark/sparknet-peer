#!/usr/bin/env bash
# sparknet anchor entrypoint.
#
# Two modes:
#  1. A wireguard config is mounted at $WG_CONF (/etc/wireguard/sparknet.conf
#     by default, set read-only in the compose file): bring up wgcore and run
#     wireguard-go in the foreground. The config is the host-rendered peer
#     stanza (make wg-render-peer); it is a network config, never key material.
#  2. No config is mounted: the anchor still runs and still OWNS the network
#     namespace the app joins (so the app's wiring is real), but it brings up no
#     tunnel. A real tunnel is a target-node, operator-verified act
#     (`DoD: confirm: user`) — it is never claimed from a build host.
set -euo pipefail

# The fabric contract: every shipped script answers --help without doing work.
if [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
sparknet anchor entrypoint

Usage: sparknet-anchor [-help]

Two modes:
  1. A non-empty wireguard config is mounted at $WG_CONF (default
     /etc/wireguard/sparknet.conf, read-only in compose): bring up wgcore,
     run wireguard-go in the foreground.
  2. No config: run inert, owning the namespace the app joins, with no tunnel.
     A real tunnel is a target-node operator act (DoD: confirm: user).
EOF
  exit 0
fi

conf="${WG_CONF:-/etc/wireguard/sparknet.conf}"

# `-s`: a missing config, a zero-length one, or a bind-mounted empty DIRECTORY
# (what docker creates when the source does not exist yet) all mean "no tunnel".
if [ ! -s "$conf" ]; then
  echo "sparknet anchor: no non-empty config at $conf — running inert (ns owner only)"
  exec sleep infinity
fi

dev=wgcore
# Prefer the kernel implementation. Only when the kernel has no WireGuard
# module do we let wireguard-go create the interface; creating it first and then
# handing it to wireguard-go collides with the existing link.
if ip link add "$dev" type wireguard 2>/dev/null; then
  wg setconf "$dev" "$conf"
  ip link set "$dev" up
  exec sleep infinity
fi
wireguard-go -f "$dev" &
go_pid=$!
for _ in $(seq 1 50); do
  ip link show "$dev" >/dev/null 2>&1 && break
  sleep 0.1
done
wg setconf "$dev" "$conf"
ip link set "$dev" up
wait "$go_pid"
