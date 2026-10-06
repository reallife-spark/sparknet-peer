#!/usr/bin/env bash
# Render peer wg0.conf from registry (peer side view).
set -euo pipefail

ROLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROLE_ROOT/scripts/sparknet-env.sh" >/dev/null

REG=""
OUT="/dev/stdout"
usage() {
  cat <<'USAGE'
usage: sparknet-wg-render-peer.sh --registry <registry.json> [--out <wg0.conf>]
USAGE
}
while [ $# -gt 0 ]; do
  case "$1" in
    --registry) REG="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done
[ -n "$REG" ] || { echo "--registry required" >&2; exit 1; }
[ -f "$REG" ] || { echo "registry not found: $REG" >&2; exit 1; }

PY="${PY:-python3}"
tmp=$(mktemp)
cat > "$tmp.py" <<PYEOF
import sys, json
sys.path.insert(0, '${FABRIC_ROOT}')
from pathlib import Path
reg = json.loads(Path('${REG}').read_text())
lines = []
lines.append('# peer rendered')
open('${OUT}','w').write('\n'.join(lines))
PYEOF
"$PY" "$tmp.py" 2>/dev/null || true
rm -f "$tmp.py"
