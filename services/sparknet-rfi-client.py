#!/usr/bin/env python3
"""Ask the Master for scoped knowledge (the peer half of the RFI loop).

The request is unsigned by design: the Master attributes it from the
WireGuard-authenticated overlay and refuses a body whose `peer_fingerprint`
disagrees (`contracts/rfi-schema.json`, `RFI_REQUEST_DESIGN.md` Option B). This
client adds nothing to that agreement; it builds the contract's request, posts
it to the Master's 8090 intake, and records the request-id locally so a repeat is
visible to an operator.

Usage::

    sparknet-rfi-client.py --question "..." --scope 05_Knowledge,docs
    sparknet-rfi-client.py --question "..." --scope contracts --dry-run
    sparknet-rfi-client.py --endpoint http://127.0.0.1:8090/api/v1/rfi-request ...
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parent
ROLE_ROOT = SERVICE_DIR.parent
REPO_ROOT = ROLE_ROOT.parent
AUTHZ = REPO_ROOT / "shared" / "sparknet-authz.py"
DEFAULT_REQUEST_DIR = "/mnt/log/sparknet-rfi-requests"
DEFAULT_PORT = 8090


def _load_authz() -> Any:
    if "sparknet_rfi_client_authz" in sys.modules:
        return sys.modules["sparknet_rfi_client_authz"]
    spec = importlib.util.spec_from_file_location("sparknet_rfi_client_authz", AUTHZ)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {AUTHZ}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def state_dir(vault_root: Path) -> Path:
    env = os.environ.get("SPARKNET_STATE_DIR", "").strip()
    return Path(env) if env else vault_root / ".state"


def load_identity(vault_root: Path) -> dict[str, Any]:
    path = vault_root / "manifests" / "node-identity.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return data


def master_endpoint(vault_root: Path) -> str:
    env = os.environ.get("SPARKNET_RFI_ENDPOINT", "").strip()
    if env:
        return env
    contracts = Path(os.environ.get("SPARKNET_CONTRACTS_DIR") or REPO_ROOT / "contracts")
    address = "10.100.0.1"
    try:
        overlay = json.loads((contracts / "overlay.json").read_text(encoding="utf-8"))
        address = str(overlay.get("master_anchor_ipv4") or address)
    except (OSError, ValueError):
        pass
    return f"http://{address}:{DEFAULT_PORT}/api/v1/rfi-request"


def build_request(
    vault_root: Path,
    *,
    question: str,
    scope: list[str],
    request_id: str | None = None,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    authz = _load_authz()
    identity = load_identity(vault_root)
    fingerprint = authz.fingerprint(identity)
    rid = request_id or f"RFI-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.urandom(3).hex()}"
    return authz.build_rfi_request(
        request_id=rid,
        peer_fingerprint=fingerprint,
        question=question,
        scope=scope,
        ttl_seconds=ttl_seconds,
    )


def send_request(request: dict[str, Any], endpoint: str, timeout: float = 10.0) -> tuple[int, dict[str, Any]]:
    if not endpoint.startswith(("http://", "https://")):
        raise RuntimeError("the endpoint must be an http(s) URL")
    body = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw) if raw else {}
        except ValueError:
            return exc.code, {"error": raw.strip() or exc.reason}
    except urllib.error.URLError as exc:
        raise RuntimeError(f"cannot reach the Master: {exc.reason}") from exc


def record_request(vault_root: Path, request: dict[str, Any], status: int, response: dict[str, Any]) -> Path:
    directory = state_dir(vault_root) / "rfi-requests"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{request['request_id']}.json"
    row = {
        "request": request,
        "status": status,
        "response": response,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    path.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def request_knowledge(
    vault_root: Path,
    *,
    question: str,
    scope: list[str],
    request_id: str | None = None,
    endpoint: str | None = None,
    ttl_seconds: int = 300,
    timeout: float = 10.0,
) -> dict[str, Any]:
    request = build_request(vault_root, question=question, scope=scope, request_id=request_id, ttl_seconds=ttl_seconds)
    status, response = send_request(request, endpoint or master_endpoint(vault_root), timeout=timeout)
    path = record_request(vault_root, request, status, response)
    return {"ok": 200 <= status < 300, "status": status, "request": request, "response": response, "record": str(path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ask the Master for scoped knowledge")
    parser.add_argument("--vault-root", type=Path, default=ROLE_ROOT)
    parser.add_argument("--question", required=True)
    parser.add_argument("--scope", default="", help="comma-separated vault categories the answer may draw on")
    parser.add_argument("--request-id")
    parser.add_argument("--endpoint")
    parser.add_argument("--ttl", type=int, default=300)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    scope = [part.strip() for part in args.scope.split(",") if part.strip()]
    if not scope:
        print("sparknet-rfi-client: --scope is required; ask only for categories you may read", file=sys.stderr)
        return 2
    root = args.vault_root.resolve()
    try:
        request = build_request(
            root, question=args.question, scope=scope, request_id=args.request_id, ttl_seconds=args.ttl
        )
        if args.dry_run:
            print(json.dumps(request, indent=2, sort_keys=True))
            return 0
        result = request_knowledge(
            root,
            question=args.question,
            scope=scope,
            request_id=args.request_id,
            endpoint=args.endpoint,
            ttl_seconds=args.ttl,
            timeout=args.timeout,
        )
    except RuntimeError as exc:
        print(f"sparknet-rfi-client: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"request {result['request']['request_id']}: HTTP {result['status']} {result['response']}")
        print(f"recorded: {result['record']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
