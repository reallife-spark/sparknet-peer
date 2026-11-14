#!/usr/bin/env bash
# Daily/on-request distillation on agx32.
#
# Collect consented teacher responses from the agx64 main/teacher, train the
# 4B student at 16k, export Q4_K_M, and push the signed artifact to the Master
# 8091 student-artifact intake. The 4B text engine is stopped while training.
set -euo pipefail
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  cat <<'HELP'
usage: sparknet-distill-daily.sh

Collects consented teacher traces from the agx64 main/teacher, trains the 4B
student at 16k, exports Q4_K_M and pushes the signed artifact to the Master's
8091 student-artifact intake. The 4B text engine is stopped only while
training and restarted on exit. Schedule it daily/on request with cron or the
`make distill-daily` target.
HELP
  exit 0
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE="${SPARKNET_DISTILL_STATE:-/mnt/ai/distill}"
UPLIFT="${SPARKNET_UPLIFT_STATE:-/mnt/ai/uplift}"
TEACHER_ENDPOINT="${SPARKNET_TEACHER_ENDPOINT:-http://10.100.0.1:8000/v1/chat/completions}"
TRAINER="${SPARKNET_DISTILL_TRAINER:-$ROOT/master-node/scripts/sparknet-distill-trainer.sh}"
CONFIG="$ROOT/peer-node/configs/distill.json"
PROMPTS="$ROOT/master-node/configs/distill-prompts.jsonl"
VLLM_WAS_RUNNING=0
mkdir -p "$STATE" "$UPLIFT/traces/agx32"

if docker ps --format '{{.Names}}' | grep -qx sparknet-vllm; then
  VLLM_WAS_RUNNING=1
  docker stop sparknet-vllm >/dev/null
fi
for model in qwen3-coder-30b-65k:latest qwen3-vl-4b-65k:latest; do
  curl -fsS -m 10 -H 'Content-Type: application/json'     -d "{\"model\":\"$model\",\"keep_alive\":0}"     http://127.0.0.1:11434/api/generate >/dev/null 2>&1 || true
done
trap 'if [ "$VLLM_WAS_RUNNING" = 1 ]; then docker start sparknet-vllm >/dev/null || true; fi' EXIT

python3 "$ROOT/scripts/sparknet-distill-collect.py" \
  --prompts "$PROMPTS" \
  --endpoint "$TEACHER_ENDPOINT" \
  --model qwen3-30b \
  --out "$UPLIFT/traces/agx32" \
  --max "${SPARKNET_DISTILL_MAX_PROMPTS:-12}"

SPARKNET_DISTILL_TRAINER="$TRAINER" \
SPARKNET_DISTILL_STATE="$STATE" \
python3 "$ROOT/master-node/services/sparknet-distill.py" run \
  --config "$CONFIG" \
  --state "$STATE" \
  --uplift "$UPLIFT" \
  --execute | tee "$STATE/last-run.json"

ARTIFACT="$(python3 - <<'PY'
import json, pathlib, sys
p = pathlib.Path("/mnt/ai/distill/last-run.json")
data = json.loads(p.read_text())
print(data.get("artifact") or "")
PY
)"
if [ -z "$ARTIFACT" ] || [ ! -s "$ARTIFACT" ]; then
  ARTIFACT="$(ls -t "$STATE"/runs/*/artifact 2>/dev/null | head -1 || true)"
fi
[ -n "$ARTIFACT" ] && [ -s "$ARTIFACT" ] || { echo "[distill-daily] no artifact" >&2; exit 1; }
RUN_ID="$(python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/mnt/ai/distill/last-run.json")
data = json.loads(p.read_text())
print(data.get("run_id") or "")
PY
)"
python3 "$ROOT/scripts/sparknet-uplift-push.py" student \
  --file "$ARTIFACT" \
  --model qwen3-4b-2507 \
  --base Qwen/Qwen3-4B-Instruct-2507 \
  --run "$RUN_ID"
echo "[distill-daily] uploaded $ARTIFACT"
