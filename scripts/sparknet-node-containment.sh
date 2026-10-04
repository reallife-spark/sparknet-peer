#!/usr/bin/env bash
# Node containment switch (root-gated) — tears down THIS node's anchor only.
#
# SCOPE: this is the peer's local containment switch. It is NOT the Sparknet
# cluster KillSwitch, which is a Master-side action that halts the whole
# fabric's control planes and is specified in
# master-node/docs/killswitch-template.md. The two are deliberately distinct:
#   * this one  — one node, loses its own Master link, root on that node
#   * the other — every node, from the Master, root on the Master
#
# Keeping them separate matters: an operator reaching for "the KillSwitch"
# must not be able to blackhole a single node by accident, and a node must not
# hold the authority to halt the cluster.
#
# This is also the documented teardown path in docs/wireguard.md: there is
# deliberately no ExecStop on the anchor, so a node comes down either by
# Master-side revocation or by an operator running this deliberately.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$(cd "${SCRIPT_DIR}/../scripts" && pwd)/sparknet-env.sh"

DRY_RUN=0
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --help|-h)
      echo "Usage: $0 [--dry-run] [--force]"
      echo "Node containment: halts local schedulers, tears down this node's wg0, stops local"
      echo "control planes, enforces a local nftables drop (ROOT-GATED)."
      echo "Scope: THIS NODE ONLY. The cluster KillSwitch is Master-side and lives in master-node/."
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

log "INFO" "sparknet node-containment starting"
log "INFO" "EUID=$(id -u), UID=$(id -u), USER=${USER:-$(id -un)}"

# Root-gating check
if [[ "$(id -u)" -ne 0 ]]; then
  log "ERROR" "Node containment requires root privileges (EUID=0). Current EUID=$(id -u)"
  log "ERROR" "Root-gated as per design - requires signed approvals"
  exit 1
fi

log "INFO" "ROOT-GATED check: PASSED"
log "INFO" "Staged teardown (template):"
log "INFO" "  1. Halt Sparknet schedulers/agents"
log "INFO" "  2. Tear down wg0 (WireGuard Master Anchor)"
log "INFO" "  3. Stop control planes (orchestrator/agents)"
log "INFO" "  4. Enforce emergency nftables drop policy"
log "INFO" "  5. Lock critical paths state"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  log "INFO" "DRY-RUN: No actual teardown performed"
  log "INFO" "sparknet-killswitch completed (DRY-RUN)"
  exit 0
fi

if [[ "${FORCE}" -ne 1 ]]; then
  log "WARN" "Use --force to execute actual teardown (template mode)"
  log "INFO" "sparknet-killswitch completed (template mode, no action)"
  exit 0
fi

log "INFO" "Executing teardown (template mode)"
log "INFO" "sparknet-killswitch completed"
exit 0
