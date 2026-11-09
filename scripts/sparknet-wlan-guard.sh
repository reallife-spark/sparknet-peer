#!/usr/bin/env bash
# WLAN emergency routing guard.
#
# The guard is fail-closed when a WLAN interface exists: if nft is missing or
# the rules cannot be installed, the unit exits non-zero. A host without a WLAN
# interface has nothing to guard and exits 0. --dry-run never writes.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/sparknet-env.sh
source "$SCRIPT_DIR/sparknet-env.sh"

DRY_RUN=0
CHECK=0
MASTER_ANCHOR="${SPARKNET_MASTER_ANCHOR:-10.100.0.1}"
WLAN_IFACE="${SPARKNET_WLAN_IFACE:-wlan0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --check) CHECK=1; shift ;;
    --help|-h)
      echo "Usage: $0 [--dry-run] [--check]"
      echo "Restricts ${WLAN_IFACE} to the Master Anchor ${MASTER_ANCHOR}."
      echo "--check verifies the table exists; it does not apply and does not report a missing WLAN as success."
      exit 0
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { printf '[%s] [wlan-guard] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if ! ip link show "$WLAN_IFACE" >/dev/null 2>&1; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "INFO" "$WLAN_IFACE is not present; printing the policy for inspection"
  else
    log "INFO" "$WLAN_IFACE is not present; nothing to guard on this host"
    exit 0
  fi
fi

if [[ "$CHECK" -eq 1 ]]; then
  if nft list table inet sparknet_wlan >/dev/null 2>&1; then
    log "OK" "sparknet_wlan table is installed"
    exit 0
  fi
  log "ERROR" "sparknet_wlan table is not installed"
  exit 3
fi

RULES=$(cat <<RULES
table inet sparknet_wlan {
  chain output_wlan {
    type filter hook output priority 0; policy accept;
    oifname "$WLAN_IFACE" ip daddr != $MASTER_ANCHOR drop
  }
  chain input_wlan {
    type filter hook input priority 0; policy accept;
    iifname "$WLAN_IFACE" ip saddr != $MASTER_ANCHOR drop
  }
}
RULES
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "DRY-RUN" "would install:"
  printf '%s\n' "$RULES"
  exit 0
fi

if ! command -v nft >/dev/null 2>&1; then
  log "ERROR" "nft(8) is not installed; refusing to report the WLAN as guarded"
  exit 1
fi

nft delete table inet sparknet_wlan >/dev/null 2>&1 || true
printf '%s\n' "$RULES" | nft -f -
if ! nft list table inet sparknet_wlan >/dev/null 2>&1; then
  log "ERROR" "nft accepted the input but the table is not present"
  exit 1
fi
log "OK" "$WLAN_IFACE restricted to $MASTER_ANCHOR"
