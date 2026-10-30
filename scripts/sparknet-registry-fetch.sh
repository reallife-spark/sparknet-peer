#!/usr/bin/env bash
# Install a Master-signed registry publication on this peer — the offline path.
#
# The live channel is a push: the Master posts a publication to the guard's
# `/api/v1/registry` and the guard installs it. This script exists for the two
# cases the live channel cannot cover: a node whose guard is not running yet,
# and an operator who wants to inspect a publication before it lands. It runs
# the same acceptance rule as the guard (shared/sparknet-registry-install.py),
# so "the peer accepted it" means the same thing on both paths.
#
# It verifies; it never authorises. Capabilities come from the entry the Master
# signed, not from this script.
#
# usage: sparknet-registry-fetch.sh --publication <publication.json> [--dry-run] [--json]
#        sparknet-registry-fetch.sh --status
set -euo pipefail

ROLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
. "$ROLE_ROOT/scripts/sparknet-env.sh" >/dev/null

usage() {
  cat <<'USAGE'
usage: sparknet-registry-fetch.sh --publication <publication.json> [--dry-run] [--json]
       sparknet-registry-fetch.sh --status

Installs a registry publication the Master signed, after verifying it against
the Master key pinned in manifests/keys/master-ed25519.pub.b64. Refuses a
publication whose sequence is not greater than the one already installed.

  --dry-run   verify and report, write nothing
  --status    print the installed sequence and this node's capabilities
USAGE
}

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --publication) ARGS+=(--publication "$2"); shift 2 ;;
    --dry-run|--json|--status) ARGS+=("$1"); shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

PY="${SPARKNET_PYTHON:-$SPARKNET_FABRIC_ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
[ -n "$PY" ] || { echo "no python interpreter found" >&2; exit 2; }

exec "$PY" "$ROLE_ROOT/services/sparknet-registry-client.py" "${ARGS[@]}"
