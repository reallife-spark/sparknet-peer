#!/usr/bin/env bash
# sparknet peer-node container entrypoint.
#
# The one thing this must do before exec'ing the command is regenerate the
# vault's .env. Nothing about the vault path is baked into the image layers —
# the layout lives at /srv/sparknet, the .env that records it does not. This is
# also what keeps a role subvault honest: env-init fails visibly when it cannot
# reach the fabric root, instead of letting the node resolve a different vault.
set -euo pipefail

# The fabric contract: every shipped script answers --help without doing work.
if [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
sparknet peer-node container entrypoint

Usage: sparknet-entrypoint CMD [ARGS...]

Regenerates the vault's .env (nothing path-baked into the image), then execs
the command. With no NET_ADMIN context the node is a standalone vault only.
EOF
  exit 0
fi

# Point the node at its own container state. In the compose deployment this is a
# host bind mount (.state), so keys survive container recreation by construction
# — no named volume, which is precisely the guarantee being asserted.
export SPARKNET_STATE_DIR="${SPARKNET_STATE_DIR:-/srv/sparknet/peer-node/.state}"

if ! /srv/sparknet/scripts/sparknet-env-init.sh /srv/sparknet/peer-node; then
  echo "sparknet peer node: env-init failed — refusing to start without a .env" >&2
  exit 1
fi

source /srv/sparknet/peer-node/.env 2>/dev/null || true

exec "$@"