#!/usr/bin/env bash
# Bring up the Sparknet WireGuard Master Anchor and then pin it.
#
# Runs as root (needs CAP_NET_ADMIN for wg-quick). The systemd unit contains no
# vault path at all — it calls this via a generated shim, which is why the whole
# sequence lives here rather than being spread across ExecStart lines that would
# each need an absolute path.
#
# Steps:
#   1. render $SPARKNET_STATE_DIR/wireguard/wg0.conf at 0600
#   2. bring up the tunnel
#   3. chattr +i the config, so this node cannot drop its Master link
#
# Deliberately no teardown path. Stopping the anchor is the Master's decision
# (revocation) or the root-gated KillSwitch's; see docs/wireguard.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/sparknet-env.sh"

log() { printf '[%s] [wg-anchor] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# --help must work before the root gate, otherwise an operator cannot read the
# usage text on the machine they are about to grant root on.
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

[[ "$(id -u)" -eq 0 ]] || die "the anchor unit must run as root (euid 0); current euid=$(id -u)"

command -v wg-quick >/dev/null 2>&1 || die "wg-quick(8) not found — install wireguard-tools"
command -v chattr >/dev/null 2>&1 || die "chattr(1) not found (e2fsprogs) — cannot pin the anchor config"

CONF="${SPARKNET_WG_RENDER_DIR:-$SPARKNET_STATE_DIR/wireguard}/wg0.conf"

log "vault:  $SPARKNET_VAULT_ROOT"
log "config: $CONF"

# 1. Render. Secrets come from the environment file the unit loads, never the vault.
"$SCRIPT_DIR/sparknet-wireguard-render.sh" --apply

[[ -f "$CONF" ]] || die "render did not produce $CONF"

# 2. Bring it up. A second `up` on an already-up tunnel is not an error worth failing on.
if wg show wg0 >/dev/null 2>&1; then
  log "wg0 already up; reloading configuration"
  wg syncconf wg0 "$CONF" || wg-quick down wg0 2>/dev/null || true
  wg-quick up wg0
else
  log "bringing up wg0"
  wg-quick up "$CONF"
fi

log "anchor established:"
wg show wg0 | sed 's/^/    /' || log "    (wg show unavailable)"

# 3. Pin the config. Re-lock a config that was previously immutable before the
#    render step rewrote it — render.sh writes a fresh file, so this is normally
#    a no-op, but guard anyway so a failed render cannot leave it unlocked.
chattr +i "$CONF"
log "pinned $CONF with chattr +i"

log "anchor is up and immutable. The Master may still revoke this peer."
exit 0