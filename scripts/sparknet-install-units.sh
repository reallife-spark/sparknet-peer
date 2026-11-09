#!/usr/bin/env bash
# Install sparknet systemd units and launcher shims.
#
# The tracked unit files in systemd/ contain no vault path: they call
# sparknet-* shims. This script generates those shims, which do contain the vault
# path but live outside the repository. That keeps the vault relocatable while
# the units stay location-independent.
#
# Two shim destinations, because system and user units resolve %h differently:
#   * system units run under the system manager, so %h is root's home. They get
#     shims in /usr/local/libexec/sparknet, referenced by absolute path.
#   * user units run under the user manager, where %h is the user's home. They
#     get shims in ~/.local/bin.
# Pointing a User=root system unit at %h/.local/bin would look for the shim in
# /root/.local/bin, which the invoking user's install never creates.
#
# Dry-run by default. Pass --apply to write anything.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/sparknet-env.sh"

BIN_DIR="${SPARKNET_BIN_DIR:-$HOME/.local/bin}"
SYSTEM_BIN_DIR="${SPARKNET_SYSTEM_BIN_DIR:-/usr/local/libexec/sparknet}"
# Python for the Python-backed shims. The services need >=3.10, which the host
# system python3 may not be, so a deployment points this at its fabric venv
# (e.g. SPARKNET_PYTHON=/home/raxxar/sparknet/.venv/bin/python).
SPARKNET_PYTHON="${SPARKNET_PYTHON:-python3}"
USER_UNIT_DIR="${USER_SYSTEMD_DIR:-$HOME/.config/systemd/user}"
SYSTEM_UNIT_DIR="${SYSTEM_UNIT_DIR:-/etc/systemd/system}"

# System units that run as root need their shim in the system location.
# Entries are bare names; the shim is always sparknet-<name>.
ROOT_UNITS=(wireguard-anchor guard dispatch-bridge wifi-guard firewall vault-provisioner admin-agent opencode-engine)

APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --help|-h)
      echo "Usage: $0 [--apply]"
      echo "Generates sparknet-* shims and installs systemd units."
      echo "  user shims:   $BIN_DIR"
      echo "  system shims: $SYSTEM_BIN_DIR"
      echo "Without --apply this only reports what it would do."
      exit 0
      ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$APPLY" -eq 0 ]]; then
  echo "DRY-RUN — nothing will be written. Re-run with --apply to install."
fi

emit() {
  if [[ "$APPLY" -eq 1 ]]; then
    printf '%s\n' "$1"
  else
    printf '  [would write] %s\n' "$1"
  fi
}

note() { printf '  %s\n' "$1"; }

# --- launcher shims ---------------------------------------------------------
# name -> command run inside the vault (resolved by sparknet-run)
shim_body() {
  case "$1" in
    guard)      printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_PYTHON" "$SPARKNET_VAULT_ROOT/services/sparknet-guard-d.py" ;;
    dispatch-bridge) printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_PYTHON" "$SPARKNET_VAULT_ROOT/services/sparknet-dispatch-bridge.py" ;;
    wifi-guard) printf 'exec %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_VAULT_ROOT/scripts/sparknet-wlan-guard.sh" ;;
    sync)       printf 'exec %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_VAULT_ROOT/scripts/sparknet-sync.sh" ;;
    wireguard-anchor) printf 'exec %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_VAULT_ROOT/scripts/sparknet-wireguard-anchor.sh" ;;
    firewall)   printf 'exec %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_VAULT_ROOT/scripts/sparknet-firewall.sh" ;;
    vault-provisioner) printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_PYTHON" "$SPARKNET_VAULT_ROOT/services/sparknet-vault-provisioner.py" ;;
    admin-agent) printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_PYTHON" "$SPARKNET_VAULT_ROOT/services/sparknet-admin-agent.py" ;;
    opencode-engine) printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_PYTHON" "$SPARKNET_VAULT_ROOT/services/sparknet-opencode-engine.py" ;;
    inventory) printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "${SPARKNET_VECTOR_PYTHON:-$SPARKNET_VAULT_ROOT/../.venv-vector/bin/python}" "$SPARKNET_VAULT_ROOT/services/sparknet-inventory-agent.py" ;;
    uplift-push) printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "${SPARKNET_VECTOR_PYTHON:-$SPARKNET_VAULT_ROOT/../.venv-vector/bin/python}" "$SPARKNET_VAULT_ROOT/../scripts/sparknet-uplift-push.py" ;;
    telemetry-push) printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_PYTHON" "$SPARKNET_VAULT_ROOT/services/sparknet-telemetry-push.py" ;;
    liveness-monitor) printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_PYTHON" "$SPARKNET_VAULT_ROOT/services/sparknet-liveness-monitor.py" ;;
    checkpoint-anchor) printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_PYTHON" "$SPARKNET_VAULT_ROOT/services/sparknet-checkpoint-anchor.py" ;;
    vault-health) printf 'exec %q %q %q %q "$@"' "$SPARKNET_PYTHON" "$SPARKNET_VAULT_ROOT/../scripts/sparknet-vault-health.py" --root "$SPARKNET_VAULT_ROOT" ;;
    exec-daemon) printf 'exec %q %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_PYTHON" "$SPARKNET_VAULT_ROOT/../scripts/sparknet-exec-daemon.py" ;;
    agent)      printf 'exec %q %q "$@"' "$SPARKNET_VAULT_ROOT/scripts/sparknet-run" "$SPARKNET_VAULT_ROOT/scripts/sparknet-agent-run.sh" ;;
    *) echo "unknown shim: $1" >&2; exit 1 ;;
  esac
}

write_shim() {
  local dir="$1" name="$2"
  local target="$dir/sparknet-$name"
  echo "  sparknet-$name"
  if [[ "$APPLY" -eq 1 ]]; then
    mkdir -p "$dir"
    {
      echo "#!/bin/sh"
      echo "# Generated by sparknet-install-units.sh — do not edit."
      echo "# Points at the vault at install time; re-run the installer if the vault moves."
      shim_body "$name"
    } > "$target"
    chmod +x "$target"
    echo "    wrote $target"
  else
    echo "    would write $target:"
    echo "      #!/bin/sh"
    echo "      $(shim_body "$name")"
  fi
}

echo "User launcher shims -> $BIN_DIR (for user units)"
for name in guard wifi-guard sync wireguard-anchor telemetry-push liveness-monitor checkpoint-anchor vault-health inventory uplift-push exec-daemon agent; do
  write_shim "$BIN_DIR" "$name"
done

echo
echo "System launcher shims -> $SYSTEM_BIN_DIR (for User=root system units)"
for name in "${ROOT_UNITS[@]}"; do
  if [[ ! -w "$SYSTEM_BIN_DIR" && ! -w "$(dirname "$SYSTEM_BIN_DIR")" ]]; then
    echo "  sparknet-$name"
    echo "    [needs root] re-run with sudo to write $SYSTEM_BIN_DIR/sparknet-$name"
    continue
  fi
  write_shim "$SYSTEM_BIN_DIR" "$name"
done

# --- units ------------------------------------------------------------------
# System units run as root (the guard is root-gated); the sync pair is a user unit.
SYSTEM_UNITS=(sparknet-wireguard-anchor.service sparknet-guard-daemon.service sparknet-dispatch-bridge.service sparknet-wifi-guard.service sparknet-vault-provisioner.service sparknet-admin-agent.service sparknet-firewall.service sparknet-opencode-engine@.service)
USER_UNITS=(sparknet-sync.service sparknet-sync.timer sparknet-telemetry-push.service sparknet-telemetry-push.timer sparknet-liveness-monitor.service sparknet-checkpoint-anchor.service sparknet-checkpoint-anchor.timer sparknet-vault-health.service sparknet-vault-health.timer sparknet-inventory.service sparknet-inventory.timer sparknet-uplift-sync.service sparknet-uplift-sync.timer sparknet-exec-daemon@.service sparknet-agent@.service)

echo
echo "System units -> $SYSTEM_UNIT_DIR (requires root)"
for unit in "${SYSTEM_UNITS[@]}"; do
  src="$SPARKNET_VAULT_ROOT/systemd/$unit"
  [[ -f "$src" ]] || { echo "  MISSING source: $src" >&2; exit 1; }
  if [[ "$APPLY" -eq 1 ]]; then
    if [[ ! -w "$SYSTEM_UNIT_DIR" ]]; then
      echo "  ERROR: need root to write $SYSTEM_UNIT_DIR — re-run with sudo." >&2
      exit 1
    fi
    cp "$src" "$SYSTEM_UNIT_DIR/$unit"
    echo "  installed $unit"
  else
    emit "$SYSTEM_UNIT_DIR/$unit"
  fi
done

echo
echo "User units -> $USER_UNIT_DIR"
for unit in "${USER_UNITS[@]}"; do
  src="$SPARKNET_VAULT_ROOT/systemd/$unit"
  [[ -f "$src" ]] || { echo "  MISSING source: $src" >&2; exit 1; }
  if [[ "$APPLY" -eq 1 ]]; then
    mkdir -p "$USER_UNIT_DIR"
    cp "$src" "$USER_UNIT_DIR/$unit"
    echo "  installed $unit"
  else
    emit "$USER_UNIT_DIR/$unit"
  fi
done

if [[ "$APPLY" -eq 1 ]]; then
  install -d -m 0755 -o root -g root /etc/sparknet
  if [[ -f "$SPARKNET_VAULT_ROOT/.env" ]]; then
    install -m 0600 -o root -g root "$SPARKNET_VAULT_ROOT/.env" /etc/sparknet/peer.env
    note "installed root-owned /etc/sparknet/peer.env"
  fi
  if [[ -f "$SPARKNET_VAULT_ROOT/.state/opencode-engine.env" ]]; then
    install -m 0600 -o root -g root "$SPARKNET_VAULT_ROOT/.state/opencode-engine.env" "/etc/sparknet/opencode-engine-${SPARKNET_ROLE:-peer}.env"
    note "installed root-owned OpenCode engine env"
  fi
fi

echo
if [[ "$APPLY" -eq 1 ]]; then
  note "Reloading systemd (skipped automatically if systemd is unavailable)."
  systemctl daemon-reload 2>/dev/null || note "  systemctl daemon-reload skipped"
  systemctl --user daemon-reload 2>/dev/null || note "  systemctl --user daemon-reload skipped"
  echo
  echo "Installed. Enable what you need, e.g.:"
  echo "  sudo systemctl enable --now sparknet-wireguard-anchor.service"
  echo "  sudo systemctl enable --now sparknet-guard-daemon.service"
  echo "  systemctl --user enable --now sparknet-sync.timer"
  echo "  systemctl --user enable --now sparknet-telemetry-push.timer"
  echo "  systemctl --user enable --now sparknet-liveness-monitor.service"
  echo "  systemctl --user enable --now sparknet-checkpoint-anchor.timer"
  echo "  systemctl --user enable --now sparknet-vault-health.timer"
else
  note "Nothing was written. Re-run with --apply to install."
fi
