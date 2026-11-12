#!/usr/bin/env python3
"""Push local skill-eval records to the Master as a signed audit report.

The records remain audit evidence: this service never grants authority, never
writes a vault page and never becomes an answer source. The Master verifies the
node signature and the peer-held `telemetry` capability, then stores the report
append-only for the read-only `/api/evals` view.
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
ROLE_ROOT = SERVICE_DIR.parent
DEFAULT_ENDPOINT = "http://10.100.0.1:8090"
EVAL_PATH = "/api/v1/evals"


def _report_module() -> Any:
    return load_shared("sparknet-eval-report.py", "sparknet_eval_report_push")


def _authz() -> Any:
    return load_shared("sparknet-authz.py", "sparknet_eval_push_authz")


def _http() -> Any:
    return load_shared("sparknet-http.py", "sparknet_eval_push_http")


def state_dir() -> Path:
    env = os.environ.get("SPARKNET_STATE_DIR", "").strip()
    return Path(env) if env else ROLE_ROOT / ".state"


def identity() -> dict[str, Any]:
    path = ROLE_ROOT / "manifests" / "node-identity.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"eval-push: cannot read identity {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"eval-push: identity {path} is not an object")
    return value


def node_id(ident: dict[str, Any]) -> str:
    configured = os.environ.get("SPARKNET_NODE_ID", "").strip()
    value = configured or ident.get("node_id")
    if not value:
        raise SystemExit("eval-push: no node_id in SPARKNET_NODE_ID or node-identity.json")
    return str(value)


def private_key(authz: Any) -> Any:
    candidates = (
        state_dir() / "keys" / "ed25519-node.key.b64",
        state_dir() / "keys" / "node.ed25519",
        state_dir() / "keys" / "identity.ed25519",
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    if not path.is_file():
        raise SystemExit(f"eval-push: node key missing: {path}")
    try:
        return authz.load_private_key(path)
    except Exception as exc:  # noqa: BLE001 - a bad key is a startup refusal
        raise SystemExit(f"eval-push: cannot load node key {path}: {exc}") from exc


def load_records(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"eval-push: cannot read records {path}: {exc}") from exc
    if isinstance(value, dict):
        value = value.get("records")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SystemExit("eval-push: records must be a JSON array of objects, or an object with a records array")
    return value


def push(records_path: Path, endpoint: str, *, dry_run: bool, as_json: bool) -> int:
    ident = identity()
    node = node_id(ident)
    authz = _authz()
    fingerprint = authz.fingerprint(ident)
    report = _report_module().build_report(
        node_id=node,
        peer_fingerprint=fingerprint,
        records=load_records(records_path),
    )
    if dry_run:
        summary = {"ok": True, "status": "dry-run", "node_id": node, "report": report}
        print(json.dumps(summary, indent=2, sort_keys=True) if as_json else f"eval-push: dry-run {report['report_id']}")
        return 0
    key = private_key(authz)
    status, response = _http().post_json(endpoint, "POST", EVAL_PATH, report, key, timeout=20)
    accepted = 200 <= status < 300
    summary = {"ok": accepted, "node_id": node, "report_id": report["report_id"], "http": status, "response": response}
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"eval-push: report={report['report_id']} http={status}")
    return 0 if accepted else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push skill-eval records to the Master as a signed audit report")
    parser.add_argument("--records", type=Path, required=True, help="JSON array of sparknet-skill-eval/1 records")
    parser.add_argument("--endpoint", default=os.environ.get("SPARKNET_EVAL_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        return push(args.records.resolve(), args.endpoint.rstrip("/"), dry_run=args.dry_run, as_json=args.json)
    except (OSError, SystemExit, ValueError) as exc:
        print(f"eval-push: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
