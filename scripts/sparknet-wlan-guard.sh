#!/usr/bin/env bash
# WLAN emergency routing guard (minimal viable implementation)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/sparknet-env.sh
source "$SCRIPT_DIR/sparknet-env.sh"

DRY_RUN=0
CHECK=0

MASTER_ANCHOR="10.100.0.1"
WLAN_IFACE="wlan0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --check)
      CHECK=1
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--dry-run] [--check]"
      echo "Enforces WLAN emergency routing policy: restrict wlan0 to Master Anchor only"
      exit 0
      ;;
    *)
      shift
      ;;
  esac
done

log() {
  local level="$1"
  shift
  printf "[%s] [%s] %s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$level" "$*"
}

log "INFO" "sparknet-wlan-guard starting"
log "INFO" "Policy: WLAN iface=${WLAN_IFACE} restricted to Master Anchor=${MASTER_ANCHOR}"
log "INFO" "Primary metric 100 (ethernet), WLAN metric 600 as per design"

# Check for wlan interface
if ip link show "${WLAN_IFACE}" >/dev/null 2>&1; then
  log "INFO" "WLAN interface ${WLAN_IFACE} exists"
else
  log "INFO" "WLAN interface ${WLAN_IFACE} not present (template mode)"
fi

# Generate nftables policy rules
NFT_RULES=$(cat <<'RULES'
# WLAN emergency routing policy (template)
# Restrict wlan0 to Master Anchor only
table inet sparknet_wlan {
  chain output_wlan {
    oifname "wlan0" ip daddr != 10.100.0.1 drop
    oifname "wlan0" ip6 daddr != <MASTER_IPV6> drop  # template
  }
  chain input_wlan {
    iifname "wlan0" ip saddr != 10.100.0.1 drop
  }
}
RULES
)

log "INFO" "Generated nftables rules:"
if [[ "${VERBOSE:-0}" -eq 1 ]] || [[ "${CHECK}" -eq 1 ]] || [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "${NFT_RULES}"
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log "INFO" "DRY-RUN: Would apply WLAN policy"
  log "INFO" "sparknet-wlan-guard completed (DRY-RUN)"
  exit 0
fi

log "INFO" "sparknet-wlan-guard completed (template mode)"
exit 0
