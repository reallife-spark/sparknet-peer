#!/usr/bin/env bash
# Daily/on-request distillation on agx32.
#
# Collect consented teacher responses from the agx64 main/teacher, build the
# dataset, and package a portable bundle for the approved external GPU trainer.
# The external host returns a GGUF; set SPARKNET_DISTILL_ARTIFACT to verify,
# sign and upload it through the Master's 8091 student-artifact intake.
set -euo pipefail
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  cat <<'HELP'
usage: sparknet-distill-daily.sh

Collects consented teacher traces from the agx64 main/teacher, builds the
dataset and packages /mnt/ai/distill/bundles/sparknet-distill-<id>-<utc>.tar.gz
for the external GPU trainer. The agx32 L4T r35 image cannot train Qwen3, so it
does not attempt a local training run.

To upload a returned artifact:
  SPARKNET_DISTILL_ARTIFACT=/path/student-q4_k_m.gguf \
  SPARKNET_DISTILL_EXPORT_JSON=/path/export.json \
  SPARKNET_DISTILL_RUN=<run-id> sparknet-distill-daily.sh

The external host never gets the peer key. Schedule this daily/on request with
cron or `make distill-daily`.
HELP
  exit 0
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STATE="${SPARKNET_DISTILL_STATE:-/mnt/ai/distill}"
UPLIFT="${SPARKNET_UPLIFT_STATE:-/mnt/ai/uplift}"
TEACHER_ENDPOINT="${SPARKNET_TEACHER_ENDPOINT:-http://10.100.0.1:8000/v1/chat/completions}"
CONFIG="$ROOT/peer-node/configs/distill.json"
PROMPTS="$ROOT/master-node/configs/distill-prompts.jsonl"
mkdir -p "$STATE" "$UPLIFT/traces/agx32"

if [ -n "${SPARKNET_DISTILL_ARTIFACT:-}" ]; then
  RUN="${SPARKNET_DISTILL_RUN:?set SPARKNET_DISTILL_RUN for a returned artifact}"
  args=(--artifact "$SPARKNET_DISTILL_ARTIFACT" --run "$RUN")
  if [ -n "${SPARKNET_DISTILL_EXPORT_JSON:-}" ]; then
    args+=(--export-json "$SPARKNET_DISTILL_EXPORT_JSON")
  fi
  python3 "$ROOT/scripts/sparknet-distill-import.py" "${args[@]}"
  exit 0
fi

python3 "$ROOT/scripts/sparknet-distill-collect.py" \
  --prompts "$PROMPTS" \
  --endpoint "$TEACHER_ENDPOINT" \
  --model qwen3-30b \
  --out "$UPLIFT/traces/agx32" \
  --max "${SPARKNET_DISTILL_MAX_PROMPTS:-12}"

python3 "$ROOT/master-node/services/sparknet-distill.py" dataset \
  --config "$CONFIG" \
  --state "$STATE" \
  --uplift "$UPLIFT" | tee "$STATE/last-dataset.json"

DATASET_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dataset_id"])' "$STATE/last-dataset.json")"
DATASET_DIR="$STATE/datasets/$DATASET_ID"
python3 "$ROOT/scripts/sparknet-distill-bundle.py" \
  --dataset "$DATASET_DIR" \
  --out-dir "$STATE/bundles" | tee "$STATE/last-bundle.json"

BUNDLE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["bundle"])' "$STATE/last-bundle.json")"
echo "[distill-daily] bundle: $BUNDLE"
echo "[distill-daily] copy it to the external GPU trainer; return the GGUF and use SPARKNET_DISTILL_ARTIFACT"
