#!/usr/bin/env bash
# Run one peer event agent as a service. The role name is the first argument.
set -euo pipefail
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "usage: sparknet-agent-run.sh <engineering|cyber> [agent args]"
  exit 0
fi
ROLE="${1:?agent role required}"
shift
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/../.venv/bin/python" "$ROOT/agents/$ROLE/agent.py" --serve "$@"
