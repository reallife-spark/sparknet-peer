#!/usr/bin/env bash
# Render peer <iface>.conf from registry (peer side view). IFACE defaults to wg0
# and follows SPARKNET_WG_INTERFACE when the overlay interface is overridden.
set -euo pipefail

ROLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROLE_ROOT/scripts/sparknet-env.sh" >/dev/null

REG=""
OUT="/dev/stdout"
usage() {
  cat <<'USAGE'
usage: sparknet-wg-render-peer.sh --registry <registry.json> [--out <iface.conf>]
USAGE
}
while [ $# -gt 0 ]; do
  case "$1" in
    --registry) REG="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done
[ -n "$REG" ] || { echo "--registry required" >&2; exit 1; }
[ -f "$REG" ] || { echo "registry not found: $REG" >&2; exit 1; }

echo "sparknet-wg-render-peer: registry-derived renderer for $OUT is not implemented; use sparknet-wireguard-render.sh with the node's WG environment" >&2
exit 2
