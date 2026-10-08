#!/usr/bin/env python3
# OpenAI-compatible local proxy dispatch bridge with PPE workflow (minimal viable)
import argparse
import importlib.util
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATES = ["PLAN", "PERMISSION_PENDING", "PERMITTED", "EXECUTING", "COMPLETE", "DENIED", "ERROR"]
DEFAULT_PORT = 4096
DEFAULT_BIND = "127.0.0.1"

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


def _shared_dir() -> Path:
    """Resolve shared/ from the environment or from the repo layout.

    A standalone bundle ships shared/ next to the role; a full clone has it at
    the fabric root. Never import by name — there is exactly one copy, loaded by
    path for the same reason authz is (see shared/README.md).
    """
    env_shared = os.environ.get("SPARKNET_SHARED_DIR", "").strip()
    if env_shared:
        return Path(env_shared)
    here = Path(__file__).resolve().parent
    candidates = [here.parent / "shared", here.parent.parent / "shared"]
    for cand in candidates:
        if (cand / "sparknet-http.py").is_file():
            return cand
    raise FileNotFoundError(
        "cannot locate shared/sparknet-http.py; this bridge refuses to "
        "reimplement the signed transport"
    )


def _load_http() -> Any:
    """Load shared/sparknet-http.py by path, never by import."""
    shared = _shared_dir()
    if "sparknet_http" in sys.modules:
        return sys.modules["sparknet_http"]
    spec = importlib.util.spec_from_file_location("sparknet_http", shared / "sparknet-http.py")
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"no importable module at {shared / 'sparknet-http.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_keypair(vault_root: Path, authz: Any) -> Any:
    """The node's own signing key from .state/keys, proving it posseses its identity.

    The bridge is local-only and injects the user environment, so the client it
    trusts is the node itself. The key lives in .state/keys (mode 0600) and is
    never shipped; when the node has not run `make user-init`, there is nothing
    to bind with and serve refuses rather than serving unsigned.
    """
    keys = vault_root / ".state" / "keys"
    candidates = [
        keys / "ed25519-node.key.b64",
        keys / "user.ed25519",
    ]
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        return None
    return authz.load_private_key(path)


def _make_router(states: list[str]) -> Callable[[str, str, dict[str, Any], dict[str, Any]], tuple[int, dict[str, Any]]]:
    def router(method: str, path: str, body: dict[str, Any], identity: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if path == "/v1/status":
            return 200, {"ok": True, "states": states, "note": APPROVALS_NOTE}
        return 404, {"ok": False, "reason": "unknown-path", "path": path}
    return router


def serve(bind, port, vault_root, ready=None):
    """Serve the signed status endpoint on (bind, port)."""
    http = _load_http()
    authz = http._load_authz()
    private_key = _load_keypair(vault_root, authz)
    if private_key is None:
        log("ERROR", "no node key at .state/keys; run `make user-init` (serve refuses to bind unsigned)")
        return 1
    router = _make_router(STATES)
    server = http.serve(
        router,
        bind=bind,
        port=port,
        public_key=private_key.public_key(),
        ready=ready,
    )
    log("INFO", f"serving signed status on {bind}:{port} (PPP states: {', '.join(STATES)})")
    log("INFO", APPROVALS_NOTE)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main():
    p = argparse.ArgumentParser(description="Sparknet Dispatch Bridge with PPE (MVI)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--health", action="store_true", help="Health check")
    p.add_argument("--serve", action="store_true", help="Serve the signed status endpoint")
    p.add_argument("--bind", default=DEFAULT_BIND, help=f"bind address for --serve (default {DEFAULT_BIND})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port for --serve (default {DEFAULT_PORT})")
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

    if args.serve:
        return serve(args.bind, args.port, vault_root)

    if args.dry_run:
        log("INFO", "DRY-RUN: PPE workflow ready (PLAN→PERMISSION→EXECUTE)")
        log("INFO", "sparknet-dispatch-bridge completed (DRY-RUN)")
        return

    log("INFO", APPROVALS_NOTE)
    log("INFO", "sparknet-dispatch-bridge completed (template mode)")
    return


if __name__ == "__main__":
    main()
