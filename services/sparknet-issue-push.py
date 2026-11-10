#!/usr/bin/env python3
"""Push local issue-tracker files to the Master as signed work items.

The issue tracker is local markdown under ``.scratch/issues/``
(``contracts/issue-schema.json``). This service is the peer half of the signed
``POST /api/v1/issues`` route: it prepares, signs with the node key through
``shared/sparknet-http.py`` and sends. The Master verifies the signature, the
registered overlay source and the peer-held ``issue-report`` capability, then
records the item.

An issue is operational work. It is never installed into the vault, never served
as knowledge and never becomes authority.
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
    from sparknet_loader import load_by_path  # noqa: E402 - shared/ is added by path above
except ImportError:  # a role copy without shared/ can still answer --help

    def load_by_path(name, path, **_kwargs):
        raise FileNotFoundError(f"cannot load {path}: shared/sparknet_loader.py is not present")


SERVICE_DIR = Path(__file__).resolve().parent
ROLE_ROOT = SERVICE_DIR.parent
FABRIC_ROOT = ROLE_ROOT.parent
DEFAULT_ENDPOINT = "http://10.100.0.1:8090"
ISSUE_PATH = "/api/v1/issues"


def _load(name: str, path: Path) -> Any:
    if not path.is_file():
        return None
    return load_by_path(name, path)


def shared_dir() -> Path:
    env = os.environ.get("SPARKNET_SHARED_DIR", "").strip()
    return Path(env) if env else FABRIC_ROOT / "shared"


def load_authz() -> Any:
    return _load("sparknet_issue_authz", shared_dir() / "sparknet-authz.py")


def load_http() -> Any:
    return _load("sparknet_issue_http", shared_dir() / "sparknet-http.py")


def load_issue() -> Any:
    module = _load("sparknet_issue_push_module", shared_dir() / "sparknet-issue.py")
    if module is None:
        raise SystemExit("issue-push: shared/sparknet-issue.py is missing")
    return module


class Refusal(RuntimeError):
    """A local refusal; the issue is not sent."""


def _issue() -> Any:
    return load_issue()


# Compatibility names used by the peer tests and any older caller. The one
# implementation is in shared/sparknet_issue; these adapters do not restate it.
def split_issue(text: str) -> tuple[dict[str, Any], str]:
    return _issue().split_issue(text)


def redact(meta: dict[str, Any], body: str) -> None:
    return _issue().redact(meta, body)


def build_payload(
    meta: dict[str, Any],
    body: str,
    *,
    authz: Any,
    node: str,
    fingerprint: str,
) -> dict[str, Any]:
    return _issue().build_payload(meta, body, authz=authz, node=node, fingerprint=fingerprint)


def issue_files(root: Path) -> list[Path]:
    return _issue().issue_files(root)


try:
    # Keep the historical refusal name while the one exception lives in shared/.
    Refusal = _issue().IssueError
except SystemExit:
    pass


def state_dir() -> Path:
    env = os.environ.get("SPARKNET_STATE_DIR", "").strip()
    return Path(env) if env else ROLE_ROOT / ".state"


def node_id() -> str:
    configured = os.environ.get("SPARKNET_NODE_ID", "").strip()
    if configured:
        return configured
    path = ROLE_ROOT / "manifests" / "node-identity.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("node_id")
    except (OSError, json.JSONDecodeError):
        value = None
    if not value:
        raise SystemExit(f"issue-push: no node_id in SPARKNET_NODE_ID or {path}")
    return str(value)


def identity() -> dict[str, Any]:
    path = ROLE_ROOT / "manifests" / "node-identity.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"issue-push: cannot read identity {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"issue-push: identity {path} is not an object")
    return value


def private_key(authz: Any) -> Any:
    candidates = (
        state_dir() / "keys" / "ed25519-node.key.b64",
        state_dir() / "keys" / "node.ed25519",
        state_dir() / "keys" / "identity.ed25519",
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
    if not path.is_file():
        raise SystemExit(f"issue-push: node key missing: {path}")
    try:
        return authz.load_private_key(path)
    except Exception as exc:  # noqa: BLE001 - a bad key is a startup refusal
        raise SystemExit(f"issue-push: cannot load node key {path}: {exc}") from exc


def load_state(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def push(root: Path, endpoint: str, *, dry_run: bool, as_json: bool) -> int:
    authz = load_authz()
    http = load_http()
    if authz is None or http is None:
        raise SystemExit("issue-push: shared/authz or shared/http is missing")
    module = load_issue()
    node = node_id()
    fp = authz.fingerprint(identity())
    key = None if dry_run else private_key(authz)
    state_path = state_dir() / "issues" / "pushed.json"
    state = load_state(state_path)
    sent = skipped = refused = 0
    results: list[dict[str, Any]] = []
    for path in module.issue_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            payload = module.prepare_payload(
                path.read_text(encoding="utf-8", errors="replace"),
                path=path,
                node=node,
                fingerprint=fp,
                authz=authz,
            )
        except module.IssueError as exc:
            refused += 1
            results.append({"file": rel, "status": "refused", "reason": exc.reason})
            print(f"issue-push: refused {rel}: {exc}", file=sys.stderr)
            continue
        issue_id = str(payload["issue_id"])
        updated = str(payload["updated_at"])
        key_id = f"{node}:{issue_id}"
        if state.get(key_id) == updated:
            skipped += 1
            results.append({"file": rel, "issue_id": issue_id, "status": "unchanged"})
            continue
        if dry_run:
            sent += 1
            results.append({"file": rel, "issue_id": issue_id, "status": "dry-run", "payload": payload})
            continue
        status, response = http.post_json(endpoint, "POST", ISSUE_PATH, payload, key, timeout=20)
        if 200 <= status < 300:
            sent += 1
            state[key_id] = updated
            results.append({"file": rel, "issue_id": issue_id, "status": "accepted", "response": response})
        else:
            refused += 1
            results.append(
                {"file": rel, "issue_id": issue_id, "status": "refused", "http": status, "response": response}
            )
            print(f"issue-push: {rel}: HTTP {status}: {response}", file=sys.stderr)
    if not dry_run:
        save_state(state_path, state)
    summary = {
        "ok": refused == 0,
        "node_id": node,
        "sent": sent,
        "skipped": skipped,
        "refused": refused,
        "results": results,
    }
    if as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"issue-push: sent={sent} skipped={skipped} refused={refused}")
    return 0 if refused == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push local issues to the Master over the signed issue route")
    parser.add_argument("--root", type=Path, default=ROLE_ROOT)
    parser.add_argument("--endpoint", default=os.environ.get("SPARKNET_ISSUE_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--once", action="store_true", help="one pass (the default)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        return push(args.root.resolve(), args.endpoint.rstrip("/"), dry_run=args.dry_run, as_json=args.json)
    except (OSError, SystemExit) as exc:
        print(f"issue-push: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
