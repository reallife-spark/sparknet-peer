#!/usr/bin/env bash
# Generate Sparknet key material OUTSIDE the vault.
#
# Nothing produced here is ever written into the tracked tree (AGENTS.md: no
# secrets in git). Everything lands in $SPARKNET_STATE_DIR/keys at mode 0600.
#
# Produces:
#   wg-<role>.key              WireGuard Curve25519 private key (secret, base64)
#   wg-<role>.pub              WireGuard public key (share with the Master)
#   ed25519-<role>.key.b64     Ed25519 private seed (secret, base64)
#   ed25519-<role>.pub.b64     Ed25519 public key (share)
#
# The Master runs the same script with --role master to produce
# master-wireguard.key / master-ed25519.key.b64, which never leave the Master.
# Everything is base64 text, matching the vault's .b64 convention;
# sparknet-authz.py also accepts a raw 32-byte seed if you already have one.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/sparknet-env.sh"

KEY_DIR="${SPARKNET_KEY_DIR:-$SPARKNET_STATE_DIR/keys}"
ROLE="${SPARKNET_ROLE:-node}"
FORCE=0

log() { printf '[%s] [keygen] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --help|-h)
      echo "Usage: $0 [--role node|master] [--force]"
      echo "Writes key material to $KEY_DIR (0600). Never into the vault tree."
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done

case "$ROLE" in
  node|master) ;;
  *) die "--role must be 'node' or 'master' (got: $ROLE)" ;;
esac

# Refuse to clobber before checking tooling: rotating a key silently would
# orphan every peer config that references the old public key.
if [[ -f "$KEY_DIR/wg-$ROLE.key" && $FORCE -ne 1 ]]; then
  die "$KEY_DIR/wg-$ROLE.key already exists; refusing to overwrite (use --force to rotate, which invalidates every peer config)"
fi

command -v wg >/dev/null 2>&1 || die "wg(8) not installed — install wireguard-tools to generate WireGuard keys"

mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"
umask 077

WG_KEY="$(wg genkey)"
WG_PUB="$(printf '%s' "$WG_KEY" | wg pubkey)"

printf '%s\n' "$WG_KEY" > "$KEY_DIR/wg-$ROLE.key"
printf '%s\n' "$WG_PUB" > "$KEY_DIR/wg-$ROLE.pub"
chmod 600 "$KEY_DIR/wg-$ROLE.key"
chmod 644 "$KEY_DIR/wg-$ROLE.pub"

log "wrote $KEY_DIR/wg-$ROLE.key (0600, secret)"
log "wrote $KEY_DIR/wg-$ROLE.pub (public)"
log ""
log "WireGuard public key for $ROLE (give this to the Master, never the private key):"
log "  $(cat "$KEY_DIR/wg-$ROLE.pub")"

# Ed25519 signing key. Produced via python3/cryptography so the encoding is
# exactly what services/sparknet-authz.py `sign` consumes: base64 text, matching
# the vault's .b64 convention and avoiding raw binary on disk.
ED_KEY="$KEY_DIR/ed25519-$ROLE.key.b64"
ED_PUB="$KEY_DIR/ed25519-$ROLE.pub.b64"

if [[ -f "$ED_KEY" && $FORCE -ne 1 ]]; then
  log "skipped Ed25519 key: $ED_KEY already exists (use --force to rotate)"
elif ! python3 -c "import cryptography" >/dev/null 2>&1; then
  log "WARNING: cryptography is not importable, so no Ed25519 key was generated."
  log "         Install it (pip install -r requirements.txt) and re-run --force."
else
  umask 077
  python3 - "$ED_KEY" "$ED_PUB" <<'PY'
import base64
import os
import stat
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

key_path, pub_path = sys.argv[1], sys.argv[2]
key = Ed25519PrivateKey.generate()

raw_priv = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
raw_pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def write(path: str, data: bytes, mode: int) -> None:
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)  # umask would otherwise strip the intended mode


# Base64 text, not raw bytes: sparknet-authz.py `sign --master-key` reads this
# file, and load_private_key() also accepts a raw 32-byte seed if you have one.
write(key_path, base64.b64encode(raw_priv) + b"\n", 0o600)
write(pub_path, base64.b64encode(raw_pub) + b"\n", 0o644)
PY
  log "wrote $ED_KEY (0600, secret)"
  log "wrote $ED_PUB (public)"
fi

log ""
if [[ "$ROLE" == "master" ]]; then
  log "MASTER: keep $ED_KEY off every peer. Publish only $ED_PUB as"
  log "        manifests/keys/master-ed25519.pub.b64 (safe to track)."
else
  log "Store the Master public key at:"
  log "  manifests/keys/master-ed25519.pub.b64   (safe to track)"
  log "For a fresh node, send wg-$ROLE.pub to the Master and ask for a signed"
  log "entry in manifests/node-authorizations.json before the peer is added."
fi
exit 0