#!/usr/bin/env bash
# Bring up (or reload) a WireGuard overlay interface from an existing config.
#
# Why this exists rather than `wg-quick up`:
#
#   On a host without the wireguard kernel module — Jetson/L4T builds ship
#   `CONFIG_WIREGUARD=n` — `wg-quick up` cannot create the interface. Its
#   userspace fallback is not reliable either: it starts `wireguard-go`, does not
#   wait for the UAPI socket, and can hang in `wg setconf` before the interface
#   is configured. A reboot then leaves the node with no overlay.
#
# This helper does the sequence deterministically:
#
#   * kernel module present -> `ip link add ... type wireguard`, then `wg setconf`
#   * absent              -> `wireguard-go`, WAIT for /var/run/wireguard/<if>/sock,
#                            `wg setconf` (capped, its effect verified), then
#                            `ip addr add`, `ip link set mtu ... up`, and any
#                            requested routes
#
# `wg setconf` on the userspace implementation can fail to return *after* it has
# applied the configuration, so it is run under `timeout` and the result is
# confirmed with `wg show` rather than trusted from the exit status.
#
# Idempotent: an interface that already exists is reloaded in place — never torn
# down. Teardown is a Master decision or the root-gated containment switch's job,
# never a side effect of bringing the anchor up.
set -euo pipefail

log() { printf '[%s] [wg-bringup] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

usage() {
  cat <<'USAGE'
usage: sparknet-wg-bringup.sh --iface NAME (--conf FILE | --setconf FILE) [options]

Brings up a WireGuard interface, using the kernel module when it is present and
wireguard-go when it is not. Idempotent: re-running reloads the configuration.

  --iface   NAME   interface to bring up (e.g. wg1)                     [required]
  --conf    FILE   wg-quick-format config: supplies the overlay Address and MTU,
                   and — unless --setconf is given — the WireGuard config itself
                   (stripped of Address/MTU/DNS/Table, which wg setconf rejects)
  --setconf FILE   explicit pure config for `wg setconf` (e.g. a pre-rendered
                   <iface>.setconf); overrides stripping --conf
  --address CIDR   overlay address, overriding the one in --conf
  --mtu     N      MTU, overriding the one in --conf (default 1400)
  --route   CIDR   add/replace this route via the interface (repeatable)
  --help, -h       this text
USAGE
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

IFACE=""
CONF=""
SETCONF=""
ADDRESS=""
MTU=""
ROUTES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --iface)   IFACE="${2:-}"; shift 2 ;;
    --conf)    CONF="${2:-}"; shift 2 ;;
    --setconf) SETCONF="${2:-}"; shift 2 ;;
    --address) ADDRESS="${2:-}"; shift 2 ;;
    --mtu)     MTU="${2:-}"; shift 2 ;;
    --route)   ROUTES+=("${2:-}"); shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[[ -n "$IFACE" ]] || die "--iface is required (try --help)"
[[ -n "$CONF" || -n "$SETCONF" ]] || die "one of --conf or --setconf is required (try --help)"
[[ "$(id -u)" -eq 0 ]] || die "must run as root (euid 0); current euid=$(id -u)"
command -v ip >/dev/null 2>&1 || die "ip(8) not found"
command -v wg >/dev/null 2>&1 || die "wg(8) not found — install wireguard-tools"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# A wg-quick-format config carries keys wg setconf does not understand. Strip them
# the way `wg-quick strip` would, leaving only [Interface] PrivateKey/ListenPort
# and the [Peer] stanzas.
strip_conf() {
  grep -viE '^[[:space:]]*(Address|DNS|MTU|Table|PreUp|PostUp|PreDown|PostDown|SaveConfig|FwMark)[[:space:]]*=' "$1" \
    | grep -vE '^[[:space:]]*#' \
    | grep -vE '^[[:space:]]*$'
}

# First value of `Key = value` in a wg-quick config (the Address line is unique).
conf_value() {
  grep -iE "^[[:space:]]*$2[[:space:]]*=" "$1" \
    | head -n1 \
    | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]+$//'
}

if [[ -n "$CONF" ]]; then
  [[ -f "$CONF" ]] || die "--conf not readable: $CONF"
  [[ -n "$ADDRESS" ]] || ADDRESS="$(conf_value "$CONF" Address)"
  [[ -n "$MTU" ]] || MTU="$(conf_value "$CONF" MTU)"
fi
[[ -n "$MTU" ]] || MTU=1400

if [[ -n "$SETCONF" ]]; then
  [[ -f "$SETCONF" ]] || die "--setconf not readable: $SETCONF"
  SETCONF_FILE="$SETCONF"
else
  SETCONF_FILE="$WORK/$IFACE.setconf"
  strip_conf "$CONF" > "$SETCONF_FILE"
  [[ -s "$SETCONF_FILE" ]] || die "stripping $CONF produced no configuration"
fi

[[ -n "$ADDRESS" ]] || die "no overlay address: pass --address or a --conf containing Address="

# `wg setconf` may apply and then hang on the userspace implementation. Cap it and
# confirm the effect with `wg show` (which does return) rather than its exit code.
apply_setconf() {
  local iface="$1" file="$2" rc=0
  timeout 10 wg setconf "$iface" "$file" || rc=$?
  if [[ "$rc" -eq 124 ]]; then
    log "wg setconf $iface did not return within 10s — confirming the result"
  elif [[ "$rc" -ne 0 ]]; then
    die "wg setconf $iface failed (rc=$rc)"
  fi
  wg show "$iface" >/dev/null 2>&1 || die "wg show $iface failed — interface is not configured"
}

ensure_address() {
  local iface="$1" addr="$2"
  if ! ip -brief addr show "$iface" 2>/dev/null | grep -qF "$addr"; then
    ip addr add "$addr" dev "$iface"
  fi
}

add_routes() {
  local iface="$1" route
  for route in "${ROUTES[@]:-}"; do
    [[ -n "$route" ]] || continue
    ip route replace "$route" dev "$iface"
  done
}

finish_up() {
  local iface="$1"
  ip link set dev "$iface" mtu "$MTU" up
  add_routes "$iface"
  log "$iface up: $ADDRESS mtu $MTU"
  ip -brief addr show "$iface" >&2 || true
  wg show "$iface" 2>/dev/null | sed 's/^/    /' >&2 || true
}

if ip link show "$IFACE" >/dev/null 2>&1; then
  log "$IFACE already exists — reloading configuration"
  apply_setconf "$IFACE" "$SETCONF_FILE"
  ensure_address "$IFACE" "$ADDRESS"
  finish_up "$IFACE"
  exit 0
fi

if ip link add dev "$IFACE" type wireguard 2>/dev/null; then
  log "kernel WireGuard module present — using it"
  apply_setconf "$IFACE" "$SETCONF_FILE"
  ensure_address "$IFACE" "$ADDRESS"
  finish_up "$IFACE"
  exit 0
fi

log "no kernel WireGuard module — using userspace wireguard-go"
command -v wireguard-go >/dev/null 2>&1 || die "wireguard-go not found and the kernel module is absent"

# Detach wireguard-go so it outlives this oneshot's exit. systemd keeps the
# unit's cgroup alive (RemainAfterExit=yes), so the tunnel stays up.
setsid wireguard-go "$IFACE" >/dev/null 2>&1 < /dev/null &

SOCK="/var/run/wireguard/$IFACE.sock"
for _ in $(seq 1 50); do
  if [[ -S "$SOCK" ]]; then
    break
  fi
  sleep 0.1
done
[[ -S "$SOCK" ]] || die "wireguard-go did not create the UAPI socket $SOCK"

apply_setconf "$IFACE" "$SETCONF_FILE"
ensure_address "$IFACE" "$ADDRESS"
finish_up "$IFACE"
exit 0
