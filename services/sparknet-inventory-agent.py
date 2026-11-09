#!/usr/bin/env python3
"""The peer inventory agent: users, models and usage into Qdrant, then the Master.

Open WebUI remains authoritative for accounts and chats. This agent projects a
small, stable inventory into the node's Qdrant ``sparknet-inventory`` collection
and pushes the same records to the Master's uplift intake on 8091. It never
stores prompt or response text in Qdrant and never edits Open WebUI.

The push is signed with the node identity key (shared/sparknet-http.py) and is
gated on the peer-held ``model-uplift`` capability by the Master's intake. A
failed push is spooled under ``$SPARKNET_STATE_DIR/uplift/outbox`` and retried;
a Master outage loses nothing.

Usage::

    sparknet-inventory-agent.py --once
    sparknet-inventory-agent.py --watch --interval 300
    sparknet-inventory-agent.py --once --no-push --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
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

DEFAULT_WEBUI = "http://127.0.0.1:8181"
DEFAULT_BRIDGE = "http://127.0.0.1:4096"
DEFAULT_OLLAMA = "http://127.0.0.1:11434"
DEFAULT_VLLM = "http://127.0.0.1:8000"
DEFAULT_UPLIFT = "http://10.100.0.1:8091"
INVENTORY_COLLECTION = "sparknet-inventory"
NAMESPACE = uuid.UUID("c3a7bb6e-4f6d-5d8e-9b8a-2b4f2f5b9c11")


class Refusal(RuntimeError):
    """A collection or push refused safely."""


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def state_dir() -> Path:
    env = os.environ.get("SPARKNET_STATE_DIR", "").strip()
    return Path(env) if env else ROLE_ROOT / ".state"


def node_id() -> str:
    configured = os.environ.get("SPARKNET_NODE_ID", "").strip()
    if configured:
        return configured
    identity_path = ROLE_ROOT / "manifests" / "node-identity.json"
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        identity = {}
    value = str(identity.get("node_id") or "").strip()
    if not value:
        raise SystemExit(f"sparknet-inventory: no node_id in SPARKNET_NODE_ID or {identity_path}")
    return value


def uplift_endpoint() -> str:
    return os.environ.get("SPARKNET_UPLIFT_ENDPOINT", "").strip() or DEFAULT_UPLIFT


def _load(name: str, path: Path) -> Any:
    if not path.is_file():
        return None
    return load_by_path(name, path)


def shared_dir() -> Path:
    env = os.environ.get("SPARKNET_SHARED_DIR", "").strip()
    if env:
        return Path(env)
    return FABRIC_ROOT / "shared"


def load_authz() -> Any:
    return _load("sparknet_inventory_authz", shared_dir() / "sparknet-authz.py")


def load_http() -> Any:
    return _load("sparknet_inventory_http", shared_dir() / "sparknet-http.py")


def point_id(kind: str, object_id: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{node_id()}:{kind}:{object_id}"))


def _http_json(url: str, token: str | None = None, timeout: float = 10.0) -> tuple[int, Any]:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def _webui_token(base: str) -> str | None:
    explicit = os.environ.get("SPARKNET_WEBUI_ADMIN_TOKEN", "").strip()
    if explicit:
        return explicit
    token_file = state_dir() / "webui-admin-token"
    try:
        value = token_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    email = os.environ.get("SPARKNET_WEBUI_ADMIN_EMAIL", "").strip()
    password = os.environ.get("SPARKNET_WEBUI_ADMIN_PASSWORD", "").strip()
    if not email or not password:
        return None
    body = json.dumps({"email": email, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/api/v1/auths/signin",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload.get("token") or "") or None
    except (OSError, ValueError):
        return None


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def normalize_user(row: dict[str, Any]) -> dict[str, Any]:
    user_id = str(row.get("id") or "")
    return {
        "schema": "sparknet-inventory/1",
        "node_id": node_id(),
        "kind": "user",
        "object_id": user_id,
        "user_id": user_id,
        "email": row.get("email"),
        "role": row.get("role"),
        "status": row.get("status") or ("active" if row.get("role") else None),
        "first_seen": row.get("created_at") or now_iso(),
        "last_seen": row.get("updated_at") or now_iso(),
        "source": "open-webui",
    }


def normalize_model(row: dict[str, Any], source: str) -> dict[str, Any]:
    model_id = str(row.get("id") or row.get("name") or row.get("model") or "")
    return {
        "schema": "sparknet-inventory/1",
        "node_id": node_id(),
        "kind": "model",
        "object_id": model_id,
        "model_id": model_id,
        "base_model_id": row.get("base_model_id") or row.get("base_model"),
        "family": row.get("family") or (model_id.split(":", 1)[0] if model_id else None),
        "status": "available",
        "first_seen": now_iso(),
        "last_seen": now_iso(),
        "source": source,
        "usage": {},
        "kb_ids": row.get("knowledge") if isinstance(row.get("knowledge"), list) else [],
    }


def session_usage(state: Path) -> Counter[tuple[str, str]]:
    """Count (user, model) turns from bridge sessions without reading content."""
    sessions = state / "sessions"
    counts: Counter[tuple[str, str]] = Counter()
    if not sessions.is_dir():
        return counts
    for path in sorted(sessions.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        model = str(row.get("model") or row.get("agent") or "unknown")
        grant = row.get("grant") if isinstance(row.get("grant"), dict) else {}
        user = str(grant.get("user_identity") or row.get("user_id") or "unknown")
        turns = row.get("turns")
        count = len(turns) if isinstance(turns, list) else 1
        counts[(user, model)] += count
    return counts


def collect(
    *,
    webui: str = DEFAULT_WEBUI,
    bridge: str = DEFAULT_BRIDGE,
    ollama: str = DEFAULT_OLLAMA,
    vllm: str = DEFAULT_VLLM,
    state: Path | None = None,
) -> list[dict[str, Any]]:
    state = state or state_dir()
    token = _webui_token(webui)
    records: list[dict[str, Any]] = []

    if token:
        status, payload = _http_json(f"{webui}/api/v1/users/", token)
        if status == 200:
            records.extend(normalize_user(row) for row in _rows(payload, "users"))
        status, payload = _http_json(f"{webui}/api/v1/models/all", token)
        if status == 200:
            records.extend(
                normalize_model(row, "open-webui") for row in _rows(payload, "models") or _rows(payload, "data")
            )
        status, payload = _http_json(f"{webui}/api/v1/knowledge/", token)
        if status == 200:
            for row in _rows(payload, "items") or _rows(payload, "knowledge"):
                kb_id = str(row.get("id") or "")
                if not kb_id:
                    continue
                records.append(
                    {
                        "schema": "sparknet-inventory/1",
                        "node_id": node_id(),
                        "kind": "knowledge_base",
                        "object_id": kb_id,
                        "kb_ids": [kb_id],
                        "status": "available",
                        "first_seen": row.get("created_at") or now_iso(),
                        "last_seen": row.get("updated_at") or now_iso(),
                        "source": "open-webui",
                    }
                )

    for endpoint, source in ((f"{ollama}/api/tags", "ollama"), (f"{vllm}/v1/models", "vllm")):
        status, payload = _http_json(endpoint)
        if status == 200:
            records.extend(normalize_model(row, source) for row in _rows(payload, "models") or _rows(payload, "data"))

    status, payload = _http_json(f"{bridge}/v1/models")
    if status == 200:
        records.extend(normalize_model(row, "bridge") for row in _rows(payload, "data") or _rows(payload, "models"))

    for (user, model), count in session_usage(state).items():
        records.append(
            {
                "schema": "sparknet-inventory/1",
                "node_id": node_id(),
                "kind": "usage",
                "object_id": f"{user}:{model}",
                "user_id": user,
                "model_id": model,
                "first_seen": now_iso(),
                "last_seen": now_iso(),
                "source": "bridge",
                "usage": {"requests": int(count), "prompt_tokens": 0, "completion_tokens": 0},
            }
        )

    # One point per object id, last writer wins; usage rows are deliberately
    # separate objects so a model point can never hide a user's usage.
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        deduped[(str(record.get("kind")), str(record.get("object_id")))] = record
    return [deduped[key] for key in sorted(deduped)]


def qdrant_upsert(records: list[dict[str, Any]]) -> int:
    try:
        qdrant = importlib.import_module("qdrant_client")
        models = importlib.import_module("qdrant_client.models")
        QdrantClient = qdrant.QdrantClient
    except ImportError as exc:
        raise Refusal("qdrant-client is not installed; run make vector-dev") from exc
    url = os.environ.get("SPARKNET_QDRANT_URL", "").strip() or "http://127.0.0.1:6333"
    key = os.environ.get("SPARKNET_QDRANT_API_KEY", "").strip() or None
    client = QdrantClient(url=url, api_key=key, timeout=30.0)
    try:
        if not client.collection_exists(INVENTORY_COLLECTION):
            client.create_collection(
                collection_name=INVENTORY_COLLECTION,
                vectors_config=models.VectorParams(size=1, distance=models.Distance.COSINE),
            )
        points = [
            models.PointStruct(id=point_id(str(row["kind"]), str(row["object_id"])), vector=[0.0], payload=row)
            for row in records
        ]
        if points:
            client.upsert(collection_name=INVENTORY_COLLECTION, points=points, wait=True)
    finally:
        client.close()
    return len(records)


def outbox_dir() -> Path:
    return state_dir() / "uplift" / "outbox"


def spool(records: list[dict[str, Any]]) -> Path:
    outbox_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    path = outbox_dir() / f"inventory-{int(time.time() * 1000)}-{os.getpid()}.json"
    payload = {"schema": "sparknet-uplift/1", "node_id": node_id(), "captured_at": now_iso(), "inventory": records}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return path


def push(records: list[dict[str, Any]], *, endpoint: str | None = None) -> tuple[bool, str]:
    endpoint = endpoint or uplift_endpoint()
    authz = load_authz()
    http = load_http()
    if authz is None or http is None:
        return False, "shared/authz or shared/http missing"
    key_path = next(
        (
            candidate
            for candidate in (
                state_dir() / "keys" / "node.ed25519",
                state_dir() / "keys" / "ed25519-node.key.b64",
                state_dir() / "keys" / "identity.ed25519",
            )
            if candidate.is_file()
        ),
        state_dir() / "keys" / "node.ed25519",
    )
    if not key_path.is_file():
        return False, f"node key missing: {key_path}"
    try:
        private_key = authz.load_private_key(key_path)
    except Exception as exc:
        return False, f"cannot load node key: {exc}"
    payload = {"schema": "sparknet-uplift/1", "node_id": node_id(), "captured_at": now_iso(), "inventory": records}
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    nonce = uuid.uuid4().hex
    signature = authz.sign_bytes(
        private_key, http.request_message("POST", "/api/v1/inventory", body, stamp, nonce=nonce, node_id=node_id())
    )
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/api/v1/inventory",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Sparknet-Node-Id": node_id(),
            "X-Sparknet-Timestamp": stamp,
            "X-Sparknet-Nonce": nonce,
            "X-Sparknet-Signature": signature,
            "User-Agent": "sparknet-inventory/1",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=20) as response:
            response.read()
            return 200 <= response.status < 300, f"pushed {len(records)} records"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"transport-error: {exc}"


def drain_outbox(endpoint: str | None = None) -> int:
    endpoint = endpoint or uplift_endpoint()
    pushed = 0
    if not outbox_dir().is_dir():
        return 0
    for path in sorted(outbox_dir().glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            records = payload.get("inventory") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                path.unlink(missing_ok=True)
                continue
            ok, reason = push(records, endpoint=endpoint)
            if ok:
                path.unlink(missing_ok=True)
                pushed += 1
            else:
                log(f"outbox retry failed: {reason}")
                break
        except (OSError, ValueError) as exc:
            log(f"outbox row unreadable: {exc}")
            path.unlink(missing_ok=True)
    return pushed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sparknet node inventory agent (Qdrant + Master uplift)")
    parser.add_argument("--once", action="store_true", help="collect and push once (default)")
    parser.add_argument("--watch", action="store_true", help="run until interrupted")
    parser.add_argument("--interval", type=int, default=300, help="seconds between passes in --watch")
    parser.add_argument("--no-push", action="store_true", help="write Qdrant only; do not dial the Master")
    parser.add_argument("--json", action="store_true", help="print the collected records")
    args = parser.parse_args(argv)

    def one_pass() -> int:
        records = collect()
        try:
            count = qdrant_upsert(records)
            log(f"inventory: upserted {count} records into {INVENTORY_COLLECTION}")
        except Refusal as exc:
            log(f"inventory: local upsert failed: {exc}")
            return 1
        if args.json:
            print(json.dumps(records, indent=2, sort_keys=True))
        if args.no_push:
            return 0
        ok, reason = push(records)
        if ok:
            log(f"inventory: {reason}")
            drain_outbox()
            return 0
        path = spool(records)
        log(f"inventory: push failed ({reason}); spooled {path.name}")
        return 0

    if args.watch:
        while True:
            one_pass()
            time.sleep(max(5, args.interval))
    return one_pass()


if __name__ == "__main__":
    sys.exit(main())
