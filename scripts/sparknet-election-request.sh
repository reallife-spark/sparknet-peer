#!/usr/bin/env bash
# PEER-SIDE: run this node's part of the cluster election (the member ceremony).
#
# Files this node's bid (operational key included), votes for --candidate
# (default: this node — a peer requesting to become master votes for itself),
# waits for the three-signature cluster trust root, co-signs it, and installs
# the result on this node's identity.
#
# Nothing here grants anything on its own. The root forms only when all three
# members run their half — the other two run this same script with
#   CANDIDATE=<requester> make election-request
# on their own nodes. Install and refusal rules: shared/sparknet-election.py,
# normative in contracts/consensus-protocol.md.
set -euo pipefail

ROLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=./sparknet-env.sh
. "$ROLE_ROOT/scripts/sparknet-env.sh"

URL="${SPARKNET_ELECTION_URL:-http://127.0.0.1:4210}"
NODE_ID=""
CANDIDATE=""
IDENTITY="$ROLE_ROOT/manifests/node-identity.json"
KEY_DIR="${SPARKNET_KEY_DIR:-$SPARKNET_STATE_DIR/keys}"
WAIT="${SPARKNET_ELECTION_WAIT:-30}"

usage() {
  cat <<USAGE
usage: sparknet-election-request.sh [--url <url>] [--candidate <node-id>]
                                    [--node-id <node-id>] [--identity <file>]
                                    [--key-dir <dir>] [--wait-seconds <n>]

Runs the full member ceremony against a running election exchange:
bid -> vote -> wait for the root -> co-sign -> install on --identity.

Defaults:
  --url          \$SPARKNET_ELECTION_URL or http://127.0.0.1:4210
  --candidate    this node's id (self-request)
  --node-id      read from --identity
  --identity     $ROLE_ROOT/manifests/node-identity.json
  --key-dir      \$SPARKNET_KEY_DIR or \$SPARKNET_STATE_DIR/keys
  --wait-seconds \$SPARKNET_ELECTION_WAIT or 30
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --node-id) NODE_ID="$2"; shift 2 ;;
    --candidate) CANDIDATE="$2"; shift 2 ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    --key-dir) KEY_DIR="$2"; shift 2 ;;
    --wait-seconds) WAIT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

[ -f "$IDENTITY" ] || { echo "identity not found: $IDENTITY" >&2; exit 1; }

PY="${PY:-$SPARKNET_FABRIC_ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
ELECTION="$SPARKNET_SHARED_DIR/sparknet-election.py"
[ -f "$ELECTION" ] || { echo "election implementation not found: $ELECTION" >&2; exit 1; }

if [ -z "$NODE_ID" ]; then
  NODE_ID="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("node_id",""))' "$IDENTITY")"
fi
case "$NODE_ID" in
  ""|REPLACE_WITH_NODE_ID)
    echo "identity $IDENTITY has no real node_id yet (run: make user-init)" >&2
    exit 1
    ;;
esac
CANDIDATE="${CANDIDATE:-$NODE_ID}"

# The cluster key signs the bid and the vote; without it this node cannot be
# one of the three. Its public half must also be what membership lists — the
# exchange checks that, and refuses a bid signed by anything else.
if [ ! -f "$KEY_DIR/cluster.ed25519" ]; then
  echo "no cluster key at $KEY_DIR/cluster.ed25519 — create one first:" >&2
  echo "  $PY $ELECTION keygen --key-dir $KEY_DIR" >&2
  exit 1
fi

exec "$PY" "$ELECTION" --shared-dir "$SPARKNET_SHARED_DIR" member \
  --url "$URL" \
  --node-id "$NODE_ID" \
  --candidate "$CANDIDATE" \
  --identity "$IDENTITY" \
  --key-dir "$KEY_DIR" \
  --wait-seconds "$WAIT"
