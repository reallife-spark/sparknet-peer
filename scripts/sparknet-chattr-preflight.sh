#!/usr/bin/env bash
# Prove `chattr +i` actually works on the filesystem that will hold .state
# BEFORE anything relies on it being immutable.
#
# The peer's protected files are made immutable with `chattr +i` on the NODE,
# not here. That defence is only worth something if the target filesystem
# honours the flag: on overlayfs, tmpfs and some network filesystems it is
# silently a no-op or an error. This preflight writes a probe file, makes it
# immutable, proves an append is refused, then removes the file. It exits 0
# only when immutability demonstrably holds; anything less fails loudly before
# the deploy path depends on it.
set -euo pipefail

usage() {
  cat <<'EOF'
sparknet-chattr-preflight.sh — prove chattr +i holds on the .state filesystem

Usage:
  sparknet-chattr-preflight.sh [DIR]

  DIR   the directory .state lives on (default: this vault's .state dir)

Exit status:
  0  the probe file was made immutable and an append really was refused
  1  anything else — immutability could not be proven, so do not rely on it
EOF
}

[ "${1:-}" = "--help" ] && { usage; exit 0; }

target="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.state}"
mkdir -p "$target"

probe="$target/.chattr-probe-$$"
rm -f "$probe"
: > "$probe"
trap 'rm -f "$probe" 2>/dev/null || true' EXIT

if ! command -v chattr >/dev/null 2>&1; then
  echo "sparknet-chattr-preflight: chattr not installed (e2fsprogs needed on the node)" >&2
  exit 1
fi

if ! chattr +i "$probe" 2>/dev/null; then
  echo "sparknet-chattr-preflight: chattr +i failed on $target — need root and a filesystem" >&2
  echo "  that honours the immutable flag (ext4/xfs); refusing to rely on immutability." >&2
  exit 1
fi

if echo cannot-append >> "$probe" 2>/dev/null; then
  echo "sparknet-chattr-preflight: chattr +i reported success but an append still landed —" >&2
  echo "  this filesystem does not honour immutability; refusing to rely on it." >&2
  chattr -i "$probe" 2>/dev/null || true
  exit 1
fi

chattr -i "$probe" 2>/dev/null || true
echo "sparknet-chattr-preflight: immutable flag proven on $target"
exit 0