#!/usr/bin/env bash
# Install the KV-dump hook into the running vLLM 0.2.7 container and restart it.
#
# vLLM 0.2.7 has no KVConnector. The hook is a small, fail-open addition to
# ModelRunner.execute_model; it dumps only when a marker file exists. The
# container restart is required to load the patched module. The patch lives in
# the container layer, so recreating the container removes it; re-run this.
set -euo pipefail
if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
  cat <<'EOF'
usage: sparknet-vllm-kv-patch.sh [hook.py]

Installs the fail-open KV-dump hook into the running vLLM 0.2.7 container and
restarts it. The patch lives in the container layer and is removed by a
container recreation; re-run after a redeploy. Requires docker access.
EOF
  exit 0
fi
CONTAINER="${VLLM_CONTAINER:-sparknet-vllm}"
HOOK_SRC="${1:-$(cd "$(dirname "$0")" && pwd)/sparknet-vllm-kv-dump.py}"
[ -f "$HOOK_SRC" ] || { echo "hook not found: $HOOK_SRC" >&2; exit 2; }

docker cp "$HOOK_SRC" "$CONTAINER":/usr/local/lib/python3.8/dist-packages/vllm/worker/sparknet_kv_dump.py

docker exec -i "$CONTAINER" python3 - <<'PY'
from pathlib import Path
path = Path("/usr/local/lib/python3.8/dist-packages/vllm/worker/model_runner.py")
text = path.read_text()
if "sparknet_kv_dump" not in text:
    text = text.replace(
        "from vllm.utils import in_wsl\n",
        "from vllm.utils import in_wsl\nfrom vllm.worker import sparknet_kv_dump\n",
        1,
    )
    old = """        # Sample the next token.\n        output = self.model.sample(\n            hidden_states=hidden_states,\n            sampling_metadata=sampling_metadata,\n        )\n        return output\n"""
    new = """        # Sample the next token.\n        output = self.model.sample(\n            hidden_states=hidden_states,\n            sampling_metadata=sampling_metadata,\n        )\n        try:\n            sparknet_kv_dump.maybe_dump(\n                kv_caches, input_metadata, self.model_config,\n                self.parallel_config, self.block_size)\n        except Exception:\n            pass\n        return output\n"""
    if old not in text:
        raise SystemExit("model_runner.py shape changed; refusing to patch blindly")
    path.write_text(text.replace(old, new, 1))
    print("patched")
else:
    print("already patched")
PY

docker restart "$CONTAINER" >/dev/null
echo "waiting for vLLM health..."
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "vLLM healthy"
    exit 0
  fi
  sleep 5
done
echo "vLLM did not become healthy in time" >&2
exit 1
