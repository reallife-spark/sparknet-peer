#!/usr/bin/env bash
# Capture local user system identity and privilege context
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "Usage: $0"
  echo "Captures local user/system identity into"
  echo "  ${SPARKNET_VAULT_ROOT:-<vault root>}/manifests/user-env-manifest.json"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    *) echo "$0: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

source "$SCRIPT_DIR/sparknet-env.sh"

MANIFEST_OUT="${SPARKNET_VAULT_ROOT}/manifests/user-env-manifest.json"

mkdir -p "$(dirname "$MANIFEST_OUT")"

python3 -c "
import os, json, pwd, grp, platform, subprocess
from pathlib import Path

def get_user():
    try:
        uid = os.geteuid()
        return pwd.getpwuid(uid)
    except Exception:
        return None

pw = get_user()
uid = pw.pw_uid if pw else os.geteuid()
gid = pw.pw_gid if pw else os.getegid()
username = pw.pw_name if pw else os.environ.get('USER','unknown')
home = pw.pw_dir if pw else os.path.expanduser('~')

groups = []
try:
    for g in grp.getgrall():
        if username in g.gr_mem:
            groups.append(g.gr_name)
    if not any(gname for gname in groups):
        pass
except Exception:
    pass

manifest = {
  'timestamp': subprocess.check_output(['date','-u','+%Y-%m-%dT%H:%M:%SZ']).decode().strip(),
  'user': {
    'uid': uid,
    'euid': os.geteuid(),
    'gid': gid,
    'egid': os.getegid(),
    'username': username,
    'home': home,
    'shell': pw.pw_shell if pw else os.environ.get('SHELL',''),
    'groups': sorted(set(groups))
  },
  'environment': {
    'xdg_config_home': os.environ.get('XDG_CONFIG_HOME', ''),
    'xdg_data_home': os.environ.get('XDG_DATA_HOME', ''),
    'xdg_state_home': os.environ.get('XDG_STATE_HOME', ''),
    'path': os.environ.get('PATH','')
  },
  'system': {
    'hostname': platform.node(),
    'os': platform.system(),
    'platform': platform.platform(),
    'release': platform.release(),
    'machine': platform.machine()
  },
  'privilege_boundaries': {
    'is_root': os.geteuid() == 0,
    'note': 'All agentic operations default to user context; root-only actions require explicit gated approvals'
  }
}

Path('$MANIFEST_OUT').write_text(json.dumps(manifest, indent=2))
print('Wrote', '$MANIFEST_OUT')
"
echo 'ok'
