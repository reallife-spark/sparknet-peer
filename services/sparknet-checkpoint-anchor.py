#!/usr/bin/env python3
"""The peer's checkpoint anchor — a signed head statement, pushed as evidence.

``contracts/provenance-protocol.md`` defines the checkpoint: a ledger block whose
``data`` names the chain's ``size``, ``head_hash`` and the RFC 6962 ``root`` over
every block hash, signed by the node key like every other block. The builder and
the ledger writer already live in ``shared/sparknet-provenance.py``; this service
is the periodic caller the design called for, plus the telemetry push that lets
the Master hold a tamper-evident head for this peer.

One checkpoint per unchanged head
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``build_checkpoint_data`` snapshots the chain *before* the checkpoint is
appended, so after the first append the checkpoint is itself the newest block.
Re-running the builder naively would therefore see ``size + 1`` every time and
append a fresh checkpoint forever. The service instead recognises the common
case: when the newest block is a checkpoint whose ``data.size`` equals the
number of blocks before it and whose root re-computes over them, the head is
already anchored and no block is written. A chain that grew past that checkpoint
gets a new one. The re-push is deliberate — the same signed block is evidence the
Master may have missed — but the ledger does not change.

What is pushed, and what is not
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The payload is ``{"checkpoint": <the full signed block>}``, not just the three
numbers: the Master needs the header signature to run ``verify_checkpoint`` for
itself. The report envelope is unsigned and overlay-attributed exactly like 70A's
status and 70D's liveness (the WireGuard tunnel is the identity; nothing binds a
fingerprint to a node key, Step 68A). This service grants, revokes, escalates and
approves nothing: the only write is ``append_block``, the peer's own statement
about its own chain.

An empty ledger is silence, not an error
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A freshly installed peer may not have a block yet, and ``build_checkpoint_data``
refuses an empty chain by design. The service exits 0 with a reason so a timer
does not train an operator to ignore a red unit. An unreadable or tampered chain
is different — that is a real failure and exits non-zero.

Usage::

    sparknet-checkpoint-anchor.py              # append (if needed) and push
    sparknet-checkpoint-anchor.py --dry-run    # show the head, write nothing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
try:
    from sparknet_loader import load_shared  # noqa: E402 - shared/ is added by path above
except ImportError:  # a role copy without shared/ can still answer --help

    def load_shared(filename, modname, **_kwargs):
        raise FileNotFoundError(f"cannot load {filename}: shared/sparknet_loader.py is not present")


SERVICE_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT_ROOT = SERVICE_DIR.parent

KIND = "checkpoint_anchor"
DEFAULT_TIMEOUT = 10


def _shared_dirs() -> list[Path]:
    dirs = []
    env_shared = os.environ.get("SPARKNET_SHARED_DIR", "").strip()
    if env_shared:
        dirs.append(Path(env_shared))
    dirs.extend([DEFAULT_VAULT_ROOT.parent / "shared", SERVICE_DIR])
    return dirs


def _load_shared(filename: str, module_name: str) -> Any:
    return load_shared(filename, module_name, dirs=_shared_dirs(), missing_ok=True)


def _load_provenance() -> Any:
    return _load_shared("sparknet-provenance.py", "sparknet_checkpoint_provenance")


def _load_pusher() -> Any:
    return load_shared("sparknet-telemetry-push.py", "sparknet_checkpoint_pusher", dirs=[SERVICE_DIR], missing_ok=True)


def _ledger_path(vault_root: Path) -> Path:
    return vault_root / "blockchain" / "ledger.json"


def _checkpoint_snapshots_head(block: Any, prior: list[dict[str, Any]], provenance: Any) -> bool:
    """Is ``block`` a checkpoint whose data still describes the chain before it?

    This is the idempotence test, not a full checkpoint verifier: the block is
    the ledger's newest and ``verify_chain`` has already checked its header
    signature and hash. Here we only ask whether it snapshots exactly the blocks
    before it, so appending again would say nothing new.
    """
    if not isinstance(block, dict):
        return False
    data = block.get("data")
    if not isinstance(data, dict) or data.get("event") != "checkpoint":
        return False
    if not prior or data.get("size") != len(prior):
        return False
    hashes = [str(item.get("hash")) for item in prior]
    if data.get("head_hash") != hashes[-1]:
        return False
    try:
        root = provenance._load_merkle().merkle_root(hashes)
    except Exception:
        return False
    return data.get("root") == root


def anchor_once(
    vault_root: Path,
    provenance: Any,
    *,
    dry_run: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    """Return ``(action, checkpoint)`` for one run.

    Actions: ``empty`` (no ledger or no blocks), ``unchanged`` (the head is
    already checkpointed), ``would-append`` (dry-run only) and ``appended``.
    A broken chain raises ``provenance.ProvenanceError`` rather than appending
    to something that no longer verifies.
    """
    ledger_path = _ledger_path(vault_root)
    if not ledger_path.is_file():
        return "empty", None
    ledger = provenance.load_ledger(ledger_path)
    blocks = ledger.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return "empty", None
    ok, detail = provenance.verify_chain(ledger)
    if not ok:
        raise provenance.ProvenanceError(f"chain does not verify: {detail}")

    newest = blocks[-1]
    if _checkpoint_snapshots_head(newest, blocks[:-1], provenance):
        return "unchanged", newest

    data = provenance.build_checkpoint_data(vault=vault_root)
    if dry_run:
        return "would-append", {"data": data}
    block = provenance.append_block(vault_root, data)
    return "appended", block


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Anchor this peer's ledger head and push the signed checkpoint (no authority)"
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
        help="peer vault root (default: the directory above this service)",
    )
    parser.add_argument("--endpoint", help="override the telemetry POST URL")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="show what would be anchored; write and send nothing")
    args = parser.parse_args(argv)

    provenance = _load_provenance()
    if provenance is None:
        print("sparknet-checkpoint-anchor: shared/sparknet-provenance.py not found", file=sys.stderr)
        return 1

    try:
        action, block = anchor_once(args.vault_root, provenance, dry_run=args.dry_run)
    except provenance.ProvenanceError as exc:
        print(f"sparknet-checkpoint-anchor: {exc}", file=sys.stderr)
        return 1

    if block is None:
        print("sparknet-checkpoint-anchor: empty chain, nothing to checkpoint", file=sys.stderr)
        return 0

    data = block.get("data") if isinstance(block.get("data"), dict) else {}
    size = data.get("size")
    if action == "unchanged":
        print(
            f"sparknet-checkpoint-anchor: head already checkpointed at size {size}; re-pushing the same anchor",
            file=sys.stderr,
        )
    elif action == "appended":
        print(f"sparknet-checkpoint-anchor: appended checkpoint at size {size}", file=sys.stderr)

    if args.dry_run:
        print(json.dumps(block, indent=2, sort_keys=True))
        return 0

    pusher = _load_pusher()
    if pusher is None:
        print("sparknet-checkpoint-anchor: telemetry pusher not found", file=sys.stderr)
        return 1
    capabilities, identity, refusal = pusher.granted_capabilities(args.vault_root)
    if identity is None or refusal:
        print(f"sparknet-checkpoint-anchor: not pushing: {refusal}", file=sys.stderr)
        return 0
    if "telemetry" not in capabilities:
        print("sparknet-checkpoint-anchor: not pushing: telemetry capability not granted", file=sys.stderr)
        return 0

    authz = pusher._load_authz()
    if authz is None:
        print("sparknet-checkpoint-anchor: shared/sparknet-authz.py not found", file=sys.stderr)
        return 1

    report = authz.build_telemetry_report(
        kind=KIND,
        peer_fingerprint=authz.fingerprint(identity),
        payload={"checkpoint": block},
    )
    endpoint = pusher.resolve_endpoint(args.vault_root, args.endpoint)
    if not endpoint.startswith(("http://", "https://")):
        print(f"sparknet-checkpoint-anchor: refusing non-HTTP endpoint: {endpoint}", file=sys.stderr)
        return 1

    ok, detail = pusher.post_report(endpoint, report, args.timeout)
    if not ok:
        print(f"sparknet-checkpoint-anchor: checkpoint to {endpoint} failed: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
