#!/usr/bin/env bash
# Provision sparknet node template (idempotent)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: $0"
  echo "Captures the node's identity manifest, then validates the peer layout."
  exit 0
fi

source "$SCRIPT_DIR/sparknet-env.sh"

"$SCRIPT_DIR/sparknet-identity-capture.sh"
"$SCRIPT_DIR/sparknet-validate.sh"
echo "Provisioning complete (template mode)"
