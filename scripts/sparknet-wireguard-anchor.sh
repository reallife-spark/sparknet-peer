#!/usr/bin/env bash
# Bring up the Sparknet WireGuard Master Anchor and then pin it.
#
# Runs as root (needs CAP_NET_ADMIN to bring the tunnel up). The systemd unit contains no
# vault path at all — it calls this via a generated shim, which is why the whole
# sequence lives here rather than being spread across ExecStart lines that would
# each need an absolute path.
#
# Steps:
#   1. render $SPARKNET_STATE_DIR/wireguard/<iface>.conf at 0600
#      (SPARKNET_WG_INTERFACE names <iface> and is required — the anchor refuses
#      to guess rather than silently bringing up a second, wrong interface)
#   2. place a copy at the conventional /etc/wireguard/<iface>.conf (0600 root),
#      so tooling and an operator that expect the standard path find the live
#      overlay config there — it is the path the guard's protected-path manifest
#      names
#   3. bring up the tunnel via sparknet-wg-bringup.sh, which uses the kernel
#      module when present and wireguard-go when it is not (Jetson/L4T ships
#      CONFIG_WIREGUARD=n)
#   4. chattr +i both the state config and the /etc copy, so this node cannot
#      drop its Master link
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
  sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

[[ "$(id -u)" -eq 0 ]] || die "the anchor unit must run as root (euid 0); current euid=$(id -u)"

command -v wg >/dev/null 2>&1 || die "wg(8) not found — install wireguard-tools"
command -v chattr >/dev/null 2>&1 || die "chattr(1) not found (e2fsprogs) — cannot pin the anchor config"

IFACE="${SPARKNET_WG_INTERFACE-}"
[[ -n "$IFACE" ]] || die "SPARKNET_WG_INTERFACE is not set — refusing to guess the overlay interface (the deployment env file must name it, e.g. wg1)"
CONF="${SPARKNET_WG_RENDER_DIR:-$SPARKNET_STATE_DIR/wireguard}/$IFACE.conf"
ETC_CONF="/etc/wireguard/$IFACE.conf"

# If anything below fails after the unlock, re-pin whatever exists before we
# exit. Without this a mid-sequence failure (a bad render, a failed install)
# would leave the live overlay config writable — the node could then drop its
# Master link, which is the exact state the immutable bit exists to prevent.
# shellcheck disable=SC2329  # reached indirectly, via `trap repin_on_exit EXIT`
repin_on_exit() {
  local rc=$?
  if (( rc != 0 )); then
    for f in "$CONF" "$ETC_CONF"; do
      [[ -f "$f" ]] && chattr +i "$f" 2>/dev/null || true
    done
    log "anchor failed (rc=$rc); re-pinned any existing config before exit"
  fi
  return "$rc"
}
trap repin_on_exit EXIT

log "vault:  $SPARKNET_VAULT_ROOT"
log "config: $CONF"

# 1. Render. Secrets come from the environment file the unit loads, never the vault.
#    A previous boot pinned the config with chattr +i; unlock it first, or the
#    render's atomic replace fails and the node boots with no link at all.
if [[ -f "$CONF" ]]; then
  chattr -i "$CONF" 2>/dev/null || true
fi
"$SCRIPT_DIR/sparknet-wireguard-render.sh" --apply

[[ -f "$CONF" ]] || die "render did not produce $CONF"

# 2. Place the conventional /etc/wireguard/<iface>.conf. The authoritative config
#    lives in the state dir; this is a second copy at the path tooling and an
#    operator expect, and the path the guard's protected manifest names. It holds
#    the private key, so it is 0600 root. Unlock first: a previous boot pinned it.
install -d -m 0755 /etc/wireguard
if [[ -f "$ETC_CONF" ]]; then
  chattr -i "$ETC_CONF" 2>/dev/null || true
fi
install -m 0600 "$CONF" "$ETC_CONF"
log "placed $ETC_CONF (mode 0600)"

# 3. Bring it up. The helper takes the kernel path when the wireguard module is
#    present and the userspace wireguard-go path when it is not, and adds the
#    host route to the Master Anchor that a /32 overlay address needs.
"$SCRIPT_DIR/sparknet-wg-bringup.sh" --iface "$IFACE" --conf "$CONF" --route 10.100.0.1/32

log "anchor established:"
wg show "$IFACE" | sed 's/^/    /' >&2 || log "    (wg show unavailable)"

# 4. Pin both copies. render.sh writes a fresh file, so this is normally a no-op;
#    it is applied so a failed render cannot leave either config unlocked.
chattr +i "$CONF"
chattr +i "$ETC_CONF"
log "pinned $CONF and $ETC_CONF with chattr +i"

log "anchor is up and immutable. The Master may still revoke this peer."
exit 0