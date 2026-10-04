#!/usr/bin/env bash
# Provision sparknet node template (idempotent)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/sparknet-env.sh"

"$SCRIPT_DIR/sparknet-identity-capture.sh"
"$SCRIPT_DIR/sparknet-validate.sh"
echo "Provisioning complete (template mode)"
