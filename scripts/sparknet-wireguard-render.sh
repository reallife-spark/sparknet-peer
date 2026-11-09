#!/usr/bin/env bash
# Render the WireGuard Master Anchor config from the vault template.
#
# Security model:
#   * The vault tracks ONLY the template. Private keys never enter git.
#   * The rendered file lands in the state dir at mode 0600.
#   * Dry-run by default; --apply is required to write.
#   * After install the caller (sparknet-wireguard-anchor.service) makes the
#     file immutable, so the node cannot quietly drop its Master link.
#
# Placeholders come from the environment or a keyfile, never from the vault:
#   SPARKNET_WG_NODE_PRIVATE_KEY   (secret, base64)
#   SPARKNET_WG_MASTER_PUBLIC_KEY  (public)
#   SPARKNET_WG_NODE_ADDRESS       e.g. 10.100.0.2/32
#   SPARKNET_WG_MASTER_ENDPOINT    host:port
#   SPARKNET_WG_LISTEN_PORT        default 51820
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/sparknet-env.sh"

# The overlay interface name is node-local (the overlay address/MTU are pinned
# by contracts/overlay.json, not the iface name). Default wg0; a deployment that
# brings the overlay up out-of-band sets SPARKNET_WG_INTERFACE to match, e.g. wg1.
IFACE="${SPARKNET_WG_INTERFACE:-wg0}"
[[ "$IFACE" =~ ^[A-Za-z0-9_]{1,15}$ ]] || { echo "invalid SPARKNET_WG_INTERFACE: $IFACE" >&2; exit 1; }
TEMPLATE="${SPARKNET_WG_TEMPLATE:-$SPARKNET_VAULT_ROOT/configs/wireguard/wg0-node.conf.template}"
RENDER_DIR="${SPARKNET_WG_RENDER_DIR:-$SPARKNET_STATE_DIR/wireguard}"
[[ "$RENDER_DIR" = /* && "$RENDER_DIR" != *..* ]] || { echo "invalid SPARKNET_WG_RENDER_DIR: $RENDER_DIR" >&2; exit 1; }
RENDER_DIR="$(realpath -m "$RENDER_DIR")"
[[ "$RENDER_DIR" != "/" ]] || { echo "refusing a root render directory" >&2; exit 1; }
RENDERED="$RENDER_DIR/$IFACE.conf"
LISTEN_PORT="${SPARKNET_WG_LISTEN_PORT:-51820}"
NODE_ADDRESS="${SPARKNET_WG_NODE_ADDRESS:-}"
MASTER_ENDPOINT="${SPARKNET_WG_MASTER_ENDPOINT:-}"
MASTER_PUBLIC_KEY="${SPARKNET_WG_MASTER_PUBLIC_KEY:-}"

log() { printf '[%s] [wireguard-render] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --help|-h)
      echo "Usage: $0 [--apply]"
      echo "Renders $TEMPLATE -> $RENDERED (mode 0600). Dry-run without --apply."
      exit 0
      ;;
    *) die "Unknown argument: $arg" ;;
  esac
done

[[ -f "$TEMPLATE" ]] || die "template not found: $TEMPLATE"

# Private key: accept an env var or a keyfile, but never a tracked vault file.
if [[ -z "${SPARKNET_WG_NODE_PRIVATE_KEY:-}" && -n "${SPARKNET_WG_PRIVATE_KEY_FILE:-}" ]]; then
  [[ -f "$SPARKNET_WG_PRIVATE_KEY_FILE" ]] || die "private key file not readable: $SPARKNET_WG_PRIVATE_KEY_FILE"
  SPARKNET_WG_NODE_PRIVATE_KEY="$(tr -d '\n' < "$SPARKNET_WG_PRIVATE_KEY_FILE")"
fi
if [[ -z "${SPARKNET_WG_MASTER_PUBLIC_KEY:-}" && -f "$SPARKNET_VAULT_ROOT/manifests/keys/master-wireguard.pub.b64" ]]; then
  MASTER_PUBLIC_KEY="$(tr -d '\n' < "$SPARKNET_VAULT_ROOT/manifests/keys/master-wireguard.pub.b64")"
fi

MISSING=()
[[ -n "${SPARKNET_WG_NODE_PRIVATE_KEY:-}" ]] || MISSING+=(SPARKNET_WG_NODE_PRIVATE_KEY)
[[ -n "$MASTER_PUBLIC_KEY" ]] || MISSING+=(SPARKNET_WG_MASTER_PUBLIC_KEY)
[[ -n "$NODE_ADDRESS" ]] || MISSING+=(SPARKNET_WG_NODE_ADDRESS)
[[ -n "$MASTER_ENDPOINT" ]] || MISSING+=(SPARKNET_WG_MASTER_ENDPOINT)

if [[ ${#MISSING[@]} -gt 0 ]]; then
  log "Missing required input(s): ${MISSING[*]}"
  log "Generate key material with:  ./scripts/sparknet-keygen.sh"
  log "Key material belongs in \$SPARKNET_STATE_DIR (mode 0700), which is git-ignored."
  log "Generated files are never committed — see AGENTS.md: no secrets in git."
  exit 2
fi

# A WireGuard key is 32 bytes -> 44 base64 chars. Catch obvious copy/paste junk
# before it reaches wg-quick, which would fail opaquely.
if [[ ! "$SPARKNET_WG_NODE_PRIVATE_KEY" =~ ^[A-Za-z0-9+/]{43}=$ ]]; then
  die "node private key is not a 32-byte base64 WireGuard key"
fi
if [[ ! "$MASTER_PUBLIC_KEY" =~ ^[A-Za-z0-9+/]{43}=$ ]]; then
  die "master public key is not a 32-byte base64 WireGuard key"
fi
if [[ ! "$NODE_ADDRESS" =~ ^10\.100\.0\.[0-9]+/32$ ]]; then
  die "node address must be a /32 in 10.100.0.0/24 (got: $NODE_ADDRESS)"
fi
if [[ ! "$LISTEN_PORT" =~ ^[0-9]{1,5}$ ]] || (( 10#$LISTEN_PORT < 1 || 10#$LISTEN_PORT > 65535 )); then
  die "listen port must be 1-65535 (got: $LISTEN_PORT)"
fi
if [[ ! "$MASTER_ENDPOINT" =~ ^(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9._-]+):([0-9]{1,5})$ ]]; then
  die "master endpoint must be host:port (got: $MASTER_ENDPOINT)"
fi
if (( 10#${BASH_REMATCH[2]} < 1 || 10#${BASH_REMATCH[2]} > 65535 )); then
  die "master endpoint port must be 1-65535 (got: $MASTER_ENDPOINT)"
fi
if grep -Eq '^[[:space:]]*(PreUp|PostUp|PreDown|PostDown)[[:space:]]*=' "$TEMPLATE"; then
  die "WireGuard hook directives are not allowed in the vault template"
fi

log "template: $TEMPLATE"
log "target:   $RENDERED"
log "master:   $MASTER_ENDPOINT  node: $NODE_ADDRESS"

RENDERED_TEXT="$(sed \
  -e "s|\${NODE_PRIVATE_KEY}|$SPARKNET_WG_NODE_PRIVATE_KEY|g" \
  -e "s|\${NODE_ADDRESS}|$NODE_ADDRESS|g" \
  -e "s|\${NODE_LISTEN_PORT}|$LISTEN_PORT|g" \
  -e "s|\${MASTER_PUBLIC_KEY}|$MASTER_PUBLIC_KEY|g" \
  -e "s|\${MASTER_ENDPOINT}|$MASTER_ENDPOINT|g" \
  "$TEMPLATE")"

# Fail closed if any placeholder survived.
if grep -q '\${[A-Z_]*}' <<< "$RENDERED_TEXT"; then
  log "ERROR: unresolved placeholders remain:"
  grep -o '\${[A-Z_]*}' <<< "$RENDERED_TEXT" | sort -u | sed 's/^/    /'
  exit 1
fi

if [[ "$APPLY" -eq 0 ]]; then
  log "DRY-RUN — nothing written. Re-run with --apply."
  log "Rendered config (private key redacted):"
  sed -e "s|^PrivateKey = .*|PrivateKey = <redacted>|" <<< "$RENDERED_TEXT" | sed 's/^/    /'
  exit 0
fi

mkdir -p "$RENDER_DIR"
umask 077
tmp="$(mktemp "$RENDER_DIR/.$IFACE.conf.XXXXXX")"
printf '%s\n' "$RENDERED_TEXT" > "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$RENDERED"
log "wrote $RENDERED (mode 0600)"
log "Next: bring up the anchor with chattr +i, e.g."
log "  sudo chattr +i $RENDERED && sudo wg-quick up $RENDERED"
exit 0