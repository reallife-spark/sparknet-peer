"""In-process KV-cache dump hook for the AGX node's vLLM 0.2.7 runtime.

vLLM 0.2.7 predates the KVConnector API, so there is no supported export
endpoint. This hook is installed into the model-runner process by
``sparknet-vllm-kv-patch.sh`` and runs only when a host-side marker file is
present. It copies the KV blocks touched by the next execute_model call to CPU
and writes a safetensors artifact plus a manifest.

It is deliberately fail-open: a dump failure must never break serving.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import torch

try:
    from safetensors.torch import save_file
except Exception:  # pragma: no cover - image without safetensors
    save_file = None

MARKER = "capture.flag"


def _dump_dir() -> Path:
    return Path(os.environ.get("SPARKNET_KV_DUMP_DIR", "/srv/sparknet/peer-node/.state/kv-capture"))


def _block_ids(input_metadata, block_size: int) -> list[int]:
    ids: set[int] = set()
    if getattr(input_metadata, "block_tables", None) is not None:
        tables = input_metadata.block_tables.detach().cpu()
        lens = getattr(input_metadata, "context_lens", None)
        lens = lens.detach().cpu() if lens is not None else None
        for row_index, row in enumerate(tables):
            if lens is not None and row_index < len(lens):
                count = (int(lens[row_index]) + block_size - 1) // block_size
                values = row[:count]
            else:
                values = row[row > 0]
            ids.update(int(value) for value in values if int(value) >= 0)
    else:
        slots = getattr(input_metadata, "slot_mapping", None)
        if slots is not None:
            for value in slots.detach().cpu().flatten().tolist():
                if int(value) >= 0:
                    ids.add(int(value) // block_size)
    return sorted(ids)


def maybe_dump(kv_caches, input_metadata, model_config, parallel_config, block_size: int) -> None:
    directory = _dump_dir()
    marker = directory / MARKER
    if not marker.is_file():
        return
    try:
        marker.unlink()
    except OSError:
        return
    try:
        # Only a prefill call carries the request's own blocks. CUDA-graph
        # capture uses dummy inputs and would dump the whole graph table.
        if getattr(input_metadata, "use_cuda_graph", False) or not getattr(input_metadata, "is_prompt", False):
            return
        if save_file is None:
            raise RuntimeError("safetensors is not installed in the engine image")
        block_ids = _block_ids(input_metadata, block_size)
        if not block_ids:
            raise RuntimeError("the request touched no KV blocks")
        torch.cuda.synchronize()
        tensors = {}
        for layer_index, (key_cache, value_cache) in enumerate(kv_caches):
            index = torch.tensor(block_ids, dtype=torch.long, device=key_cache.device)
            tensors[f"layer_{layer_index}_key"] = key_cache.index_select(0, index).detach().cpu().contiguous()
            tensors[f"layer_{layer_index}_value"] = value_cache.index_select(0, index).detach().cpu().contiguous()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / "capture.safetensors"
        save_file(tensors, str(target))
        manifest = {
            "schema": "sparknet-kv-capture/1",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model_id": str(getattr(model_config, "model", "")),
            "architecture": str(getattr(getattr(model_config, "hf_config", None), "model_type", "")),
            "num_layers": int(model_config.get_num_layers(parallel_config)),
            "num_kv_heads": int(model_config.get_num_kv_heads(parallel_config)),
            "head_dim": int(model_config.get_head_size()),
            "dtype": str(getattr(model_config, "dtype", "")),
            "block_size": int(block_size),
            "block_ids": block_ids,
            "num_blocks": len(block_ids),
            "tensor_count": len(tensors),
            "file": target.name,
        }
        (directory / "capture.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:  # pragma: no cover - must never break serving
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            (directory / "capture.error").write_text(str(exc) + "\n", encoding="utf-8")
        except Exception:
            pass
