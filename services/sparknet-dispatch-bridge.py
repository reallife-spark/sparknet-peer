#!/usr/bin/env python3
# OpenAI-compatible local proxy dispatch bridge with PPE workflow (minimal viable)
import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

STATES = ["PLAN", "PERMISSION_PENDING", "PERMITTED", "EXECUTING", "COMPLETE", "DENIED", "ERROR"]

# Approvals are recorded through shared/sparknet-provenance.py, never here.
# This bridge used to carry its own `--test-approval` block builder with a
# different block shape (index/payload/block_hash, a stub signature), which is
# exactly how the two ledger shapes diverged in the first place. A second
# builder is the failure mode; the flag is gone rather than corrected.
APPROVALS_NOTE = (
    "approvals are recorded through shared/sparknet-provenance.py "
    "(make provenance-append), never through this bridge"
)


def log(level, msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [{level}] {msg}")


def main():
    p = argparse.ArgumentParser(description="Sparknet Dispatch Bridge with PPE (MVI)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--health", action="store_true", help="Health check")
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    vault_root = Path(os.environ.get("SPARKNET_VAULT_ROOT") or script_dir.parent)
    ledger_path = vault_root / "blockchain" / "ledger.json"

    log("INFO", "sparknet-dispatch-bridge starting")
    log("INFO", f"Ledger (append lives elsewhere): {ledger_path}")

    if args.health:
        log("INFO", "Health check: OK")
        log("INFO", f"PPE states: {', '.join(STATES)}")
        return

    if args.dry_run:
        log("INFO", "DRY-RUN: PPE workflow ready (PLAN→PERMISSION→EXECUTE)")
        log("INFO", "sparknet-dispatch-bridge completed (DRY-RUN)")
        return

    log("INFO", APPROVALS_NOTE)
    log("INFO", "sparknet-dispatch-bridge completed (template mode)")
    return


if __name__ == "__main__":
    main()
