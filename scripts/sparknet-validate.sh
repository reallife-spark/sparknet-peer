#!/usr/bin/env bash
# Validate the peer-node role environment.
#
# Beyond checking files exist, this asserts the *role* preconditions: a peer
# must not hold a Master signing key, and the contract it obeys must be
# reachable. Those are the two mistakes that would silently turn this node into
# something it must never be.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/sparknet-env.sh
source "$SCRIPT_DIR/sparknet-env.sh"

fail=0
ok()   { printf '  ✓ %s\n' "$*"; }
bad()  { printf '  ✗ %s\n' "$*"; fail=1; }

echo "=== sparknet peer-node validation ==="
echo "  role:            $SPARKNET_ROLE"
echo "  peer vault root: $SPARKNET_VAULT_ROOT"
echo "  fabric root:     $SPARKNET_FABRIC_ROOT"
echo

echo "--- required peer files ---"
for f in manifests/node-identity.json \
         manifests/node-authorizations.json \
         manifests/critical-manifest.json \
         manifests/security-policies.json \
         manifests/user-env-manifest.json \
         configs/wireguard/wg0-node.conf.template \
         services/sparknet-guard-d.py \
         scripts/sparknet-wireguard-render.sh; do
  [ -f "$SPARKNET_VAULT_ROOT/$f" ] && ok "$f" || bad "missing $f"
done
[ -x "$SCRIPT_DIR/sparknet-identity-capture.sh" ] && ok "identity capture executable" || bad "identity capture not executable"

echo
echo "--- shared contract reachable ---"
[ -d "$SPARKNET_CONTRACTS_DIR" ] && ok "contracts at $SPARKNET_CONTRACTS_DIR" \
  || bad "contracts not found at $SPARKNET_CONTRACTS_DIR (set SPARKNET_FABRIC_ROOT or SPARKNET_CONTRACTS_DIR)"
[ -f "$SPARKNET_SHARED_DIR/sparknet-authz.py" ] && ok "shared authz implementation" \
  || bad "shared authz missing at $SPARKNET_SHARED_DIR (set SPARKNET_SHARED_DIR)"

echo
echo "--- peer role must not hold signing authority ---"
# The single most important assertion in this file. A peer that has the Master
# private key can sign its own authorizations, which collapses the entire trust
# model: it could grant itself guard-apply and mutate any protected file.
if find "$SPARKNET_VAULT_ROOT" \
     -name 'ed25519-master*' -o -name 'master-ed25519.key*' \
     2>/dev/null | grep -q .; then
  bad "a Master private key appears to be present in this peer subvault"
else
  ok "no Master private key in the peer subvault"
fi

echo
echo "--- registry posture ---"
python3 - "$SPARKNET_VAULT_ROOT/manifests/node-authorizations.json" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"  ✗ registry unreadable: {exc}")
    raise SystemExit(1)
entries = data.get("authorizations", [])
if not entries:
    # Expected on a fresh deployment: fail-closed, nobody has privileges.
    print("  ✓ registry is empty — no node holds privileges (fail-closed default)")
else:
    print(f"  · registry holds {len(entries)} authorization(s)")
    for e in entries:
        ident = e.get("identity", {})
        print(f"      {ident.get('node_id', '?')}  caps={e.get('capabilities')}  revoked={e.get('revoked', False)}")
PY

echo
echo "--- this node's identity ---"
python3 - "$SPARKNET_VAULT_ROOT/manifests/node-identity.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"  node_id:  {d.get('node_id')}")
print(f"  role:     {d.get('role')}")
print(f"  overlay:  {d.get('overlay_ipv4')}")
print(f"  requests: {', '.join(d.get('requested_capabilities', []))}")
if d.get("role") != "peer":
    print("  ✗ node-identity.json role is not 'peer'")
    raise SystemExit(1)
PY

echo
if [ "$fail" -eq 0 ]; then
  echo "=== peer-node validation passed ==="
else
  echo "=== peer-node validation FAILED ===" >&2
  exit 1
fi