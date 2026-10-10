#!/usr/bin/env bash
# sparknet peer-node container healthcheck.
#
# The node is healthy when its opencode binary answers with the pinned version.
# Nothing here depends on a GPU: a GPU only changes model throughput and is
# never required to run, so the presence or absence of one must not flip a
# node's health. Exit 0 = healthy, not 0 = the container is recreated.
set -euo pipefail

# The fabric contract: every shipped script answers --help without doing work.
if [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
sparknet peer-node container healthcheck

Usage: sparknet-healthcheck

Exit 0 when the pinned opencode answers; non-zero tells Docker to recreate the
container. Hardware acceleration is never required: it only changes throughput.
EOF
  exit 0
fi

want="${OPENCODE_VERSION:-}"
out="$(opencode --version 2>&1)"

case "$want:$out" in
  *"$want"*) exit 0 ;;
  *) echo "sparknet peer node: opencode answered '$out', wanted '$want'" >&2; exit 1 ;;
esac