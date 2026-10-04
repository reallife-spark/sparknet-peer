#!/usr/bin/env python3
# OpenAI-compatible local proxy dispatch bridge with PPE workflow (minimal viable)
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATES = ["PLAN", "PERMISSION_PENDING", "PERMITTED", "EXECUTING", "COMPLETE", "DENIED", "ERROR"]


def log(level, msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [{level}] {msg}")


def compute_block_hash(prev_hash, index, timestamp, payload):
    m = hashlib.sha256()
    m.update((str(prev_hash) + str(index) + str(timestamp) + json.dumps(payload, sort_keys=True)).encode())
    return m.hexdigest()


def load_ledger(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log("ERROR", f"Failed to load ledger {path}: {e}")
        raise


def save_ledger(path, ledger):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(ledger, f, indent=2)
        return True
    except Exception as e:
        log("ERROR", f"Failed to save ledger {path}: {e}")
        return False


def main():
    p = argparse.ArgumentParser(description="Sparknet Dispatch Bridge with PPE (MVI)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--health", action="store_true", help="Health check")
    p.add_argument("--test-approval", action="store_true", help="Append test approval block")
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    vault_root = Path(os.environ.get("SPARKNET_VAULT_ROOT") or script_dir.parent)
    ledger_path = vault_root / "blockchain" / "ledger.json"

    log("INFO", "sparknet-dispatch-bridge starting")
    log("INFO", f"Ledger: {ledger_path}")

    if args.health:
        log("INFO", "Health check: OK")
        log("INFO", "PPE states: PLAN, PERMISSION_PENDING, PERMITTED, EXECUTING, COMPLETE, DENIED, ERROR")
        return

    try:
        ledger = load_ledger(ledger_path)
    except Exception:
        return

    if args.test_approval:
        blocks = ledger.get("blocks", [])
        idx = len(blocks)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        prev_hash = blocks[-1].get("block_hash") if blocks else ledger.get("genesis_hash", "")
        payload = {
            "type": "approval",
            "state": "PERMISSION_PENDING",
            "ppe_stage": "PERMISSION",
            "description": "Test approval block (dry-run/test)",
            "user": os.environ.get("USER", "unknown"),
        }
        bh = compute_block_hash(prev_hash, idx, ts, payload)
        block = {
            "index": idx,
            "timestamp": ts,
            "prev_hash": prev_hash,
            "block_hash": bh,
            "payload": payload,
            "signature": "ed25519:stub",  # template
        }
        if args.dry_run:
            log("INFO", f"DRY-RUN: Would append block {idx} with hash {bh[:16]}...")
            log("INFO", "sparknet-dispatch-bridge completed (DRY-RUN)")
            return
        blocks.append(block)
        ledger["blocks"] = blocks
        if save_ledger(ledger_path, ledger):
            log("INFO", f"Appended approval block {idx}")
        log("INFO", "sparknet-dispatch-bridge completed")
        return

    if args.dry_run:
        log("INFO", "DRY-RUN: PPE workflow ready (PLAN→PERMISSION→EXECUTE)")
        log("INFO", "sparknet-dispatch-bridge completed (DRY-RUN)")
        return

    log("INFO", "sparknet-dispatch-bridge completed (template mode)")
    return


if __name__ == "__main__":
    main()
