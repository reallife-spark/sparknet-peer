#!/usr/bin/env bash
# Show what installing the vault's generated config into /etc would do.
#
# This script is PERMANENTLY DRY-RUN. There is deliberately no --apply flag and
# no code path that writes: a malformed netplan apply on a remote node can drop
# your SSH session, so applying is a human decision made from the output below.
# tests/test_containment.py asserts that no write primitive exists here.
#
# The mapping is read from manifests/critical-manifest.json (source_of_truth),
# so this script cannot drift from the manifest.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/sparknet-env.sh"

MANIFEST="$SPARKNET_VAULT_ROOT/manifests/critical-manifest.json"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

if [[ ! -f "$MANIFEST" ]]; then
  log "ERROR: manifest not found: $MANIFEST"
  exit 1
fi

log "PERMANENT DRY-RUN — this script cannot write to /etc."
log "Manifest: $MANIFEST"
echo

# Emit "installed_path<TAB>vault_path" for entries this script would render.
plan="$(python3 - "$MANIFEST" <<'PY'
import json
import sys

with open(sys.argv[1]) as fh:
    manifest = json.load(fh)

sot = manifest.get("source_of_truth", {})
for entry in sot.get("generated", []):
    if entry.get("installer") == "scripts/sparknet-install-config.sh":
        print(f"{entry['installed_path']}\t{entry['vault_path']}")
PY
)"

if [[ -z "$plan" ]]; then
  log "No config entries are assigned to this installer."
  exit 0
fi

changed=0
while IFS=$'\t' read -r installed vault_rel; do
  [[ -n "$installed" && -n "$vault_rel" ]] || continue
  vault_path="$SPARKNET_VAULT_ROOT/$vault_rel"

  echo "── $installed"
  if [[ ! -f "$vault_path" ]]; then
    echo "   MISSING SOURCE: $vault_path"
    changed=$((changed + 1))
    echo
    continue
  fi

  echo "   source: $vault_rel"

  if [[ ! -e "$installed" ]]; then
    echo "   target does not exist — applying would create it with this content:"
    sed 's/^/     + /' "$vault_path"
    changed=$((changed + 1))
  else
    if diff -u --label "installed:$installed" --label "vault:$vault_rel" \
         "$installed" "$vault_path" > /dev/null 2>&1; then
      echo "   up to date — no change"
    else
      echo "   DRIFT — applying would produce this diff (installed -> vault):"
      diff -u --label "installed:$installed" --label "vault:$vault_rel" \
           "$installed" "$vault_path" 2>/dev/null | sed 's/^/     /' || true
      changed=$((changed + 1))
    fi
  fi
  echo
done <<< "$plan"

log "Files needing attention: $changed"
echo
echo "Applying is a deliberate, manual step. Review each diff above, then copy the"
echo "vault file to its installed path by hand. For netplan, apply the network"
echo "configuration only after you have read the diff."
echo
echo "Unmapped installed paths (no vault generator) are listed under"
echo "source_of_truth.unmapped in $MANIFEST."
