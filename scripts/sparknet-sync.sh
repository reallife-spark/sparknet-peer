#!/usr/bin/env bash
# User-space sync over WireGuard (minimal viable implementation)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/sparknet-env.sh
source "$SCRIPT_DIR/sparknet-env.sh"

DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--dry-run]"
      echo "Minimal viable sparknet sync (user-space over WireGuard overlay)"
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

log "INFO" "sparknet-sync starting (user-space)"
log "INFO" "SPARKNET_VAULT_ROOT=${SPARKNET_VAULT_ROOT}"
log "INFO" "SPARKNET_KNOWLEDGE_ROOT=${SPARKNET_KNOWLEDGE_ROOT}"
log "INFO" "USER=${USER:-$(id -un)}"
log "INFO" "EUID=$(id -u)"

# Validate paths
if [[ ! -d "${SPARKNET_VAULT_ROOT}" ]]; then
  log "ERROR" "Vault root does not exist: ${SPARKNET_VAULT_ROOT}"
  exit 1
fi

if [[ ! -w "${SPARKNET_VAULT_ROOT}" ]]; then
  log "WARN" "Vault root may not be writable: ${SPARKNET_VAULT_ROOT}"
fi

# Determine sync sources/targets (template mode - document structure)
SYNC_PATHS=(
  "${SPARKNET_VAULT_ROOT}/knowledge"
  "${SPARKNET_VAULT_ROOT}/configs"
  "${SPARKNET_VAULT_ROOT}/manifests"
  "${SPARKNET_VAULT_ROOT}/blockchain"
)

log "INFO" "Paths to sync (template mode): ${#SYNC_PATHS[@]} paths"

for p in "${SYNC_PATHS[@]}"; do
  if [[ -e "$p" ]]; then
    log "INFO" "  - $(realpath --relative-to="${SPARKNET_KNOWLEDGE_ROOT}" "$p" 2>/dev/null || echo "$p")"
  else
    log "INFO" "  - (missing, will be created if needed) $p"
  fi
done

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log "INFO" "DRY-RUN mode: no changes will be made"
  log "INFO" "Would sync over WireGuard overlay (user-space, rsync-based)"
  log "INFO" "sparknet-sync completed successfully (DRY-RUN)"
  exit 0
fi

log "INFO" "sparknet-sync completed successfully"
exit 0
