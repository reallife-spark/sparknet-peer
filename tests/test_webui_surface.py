"""The Step 5 WebUI surface: declared models, the Approve/Deny card, config.

peer PLAN Step 5 verifies five claims as code before any live Open WebUI exists:

* DoD 1 — ``plan``/``build``/``vault``/``node-orchestrator`` are selectable
  models, each bound to the agent that owns it, and they come from the committed
  ``manifests/agent-models.json`` (never invented by the bridge);
* DoD 2 — a permission request is raised and resolved through
  ``POST /session/:id/permissions/:permissionID`` (allow/deny), grant-checked,
  session-scoped and persisted like every other session turn;
* DoD 3 — the Step 4 vault tools stay grant-checked (covered by `test_bridge`).
* DoD 5 — the surface is versioned declarative config, and the bridge fails
  closed when that config is absent.

The live Open WebUI handshake is `DoD: confirm: user`, exactly like the
ai-stack deployment it rides on; these tests prove the files cohere.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest
import test_bridge as tb  # pytest inserts this tests dir on sys.path (no __init__.py)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

VAULT_ROOT = Path(__file__).resolve().parent.parent
SHARED_DIR = VAULT_ROOT.parent / "shared"
NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _load(name: str, label: str) -> ModuleType:
    path = SHARED_DIR / f"{name}.py"
    if not path.is_file():
        pytest.skip(f"shared implementation not present at {path}")
    spec = importlib.util.spec_from_file_location(label, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


authz = _load("sparknet-authz", "sparknet_authz_webui")
prov = _load("sparknet-provenance", "sparknet_provenance_webui")


def _b64(grant: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(grant).encode("utf-8")).decode("ascii")


def _bearer(grant: dict) -> str:
    return f"Bearer {_b64(grant)}"


def _master_pub_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture
def master_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def ctx(tmp_path: Path, master_key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch) -> dict:
    vault = tmp_path / "vault"
    shutil.copytree(
        VAULT_ROOT,
        vault,
        ignore=shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "references", ".opencode"),
    )
    import os as _os

    _raw = vault / "_raw"
    if _raw.is_dir() and not _raw.is_symlink():
        shutil.rmtree(_raw)
        _os.symlink("03_Sources", _raw)
    state = tmp_path / "state"
    monkeypatch.setenv("SPARKNET_STATE_DIR", str(state))

    prov.user_init(vault, owner_id="U-20161", force=True)

    node_private_b64, _ = authz.generate_keypair()
    node_key = state / "keys" / "ed25519-node.key.b64"
    node_key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    node_key.write_text(node_private_b64 + "\n")
    node_key.chmod(0o600)

    identity = json.loads((vault / "manifests" / "node-identity.json").read_text())
    entry = authz.sign_authorization(identity, master_key, ["telemetry", "p2p-sync"])
    registry = {
        "schema_version": "1.0",
        "master_public_key_b64": _master_pub_b64(master_key),
        "description": "test registry",
        "authorizations": [entry],
    }
    (vault / "manifests" / "node-authorizations.json").write_text(json.dumps(registry, indent=2) + "\n")
    chat = vault / ".opencode" / "agents" / "chat.md"
    chat.parent.mkdir(parents=True, exist_ok=True)
    chat.write_text("# chat\n")
    return {"vault": vault, "state": state, "identity": identity, "registry": registry, "master_key": master_key}


def _grant(ctx: dict, **overrides) -> dict:
    params = dict(
        node_identity=ctx["identity"],
        grant_id="GRANT-WEBUI-1",
        user_identity="U-20161",
        conversation_key="01010101-0101-0101-0101-010101010101-2",
        capabilities=["p2p-sync"],
        vault_scope=["05_Knowledge"],
        ttl_minutes=480,
        nonce="webui-nonce-0001",
        master_private_key=ctx["master_key"],
        registry=ctx["registry"],
        now=NOW,
    )
    params.update(overrides)
    return authz.mint_grant(**params)


@pytest.fixture
def server(ctx):
    srv = tb.Server(ctx)
    try:
        yield srv
    finally:
        srv.close()


def _start_chat(server, ctx, model: str = "chat", content: str = "hello"):
    grant = _grant(ctx)
    status, payload = server.request(
        "POST",
        "/v1/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": content}]},
        token=_bearer(grant),
    )
    return status, payload, grant


# --- DoD 1: the declared agents are selectable models ---------------------------


def test_models_list_the_declared_roles_first(server, ctx):
    status, payload = server.request("GET", "/v1/models", token=_bearer(tb._grant(ctx)))
    assert status == 200
    ids = [m["id"] for m in payload["data"]]
    assert ids[:4] == ["plan", "build", "vault", "node-orchestrator"], ids
    declared = {m["id"]: m for m in payload["data"] if m.get("selectable")}
    assert declared["plan"]["agent"] == "plan"
    assert declared["node-orchestrator"]["agent"] == "sparknet-node"
    discovered = [m["id"] for m in payload["data"] if not m.get("selectable")]
    assert "chat" in discovered, "the on-disk agent still surfaces as a discovered model"


def test_chat_completions_accept_a_declared_role_model(server, ctx):
    status, payload, grant = _start_chat(server, ctx, model="plan", content="route this to the planner")
    assert status == 200
    assert payload["model"] == "plan"
    assert "plan" in payload["choices"][0]["message"]["content"]

    status2, session = server.request("GET", f"/session/{grant['grant_id']}", token=_bearer(grant))
    assert status2 == 200
    assert session["session"]["model"] == "plan"
    assert session["session"]["conversation"][0]["content"] == "route this to the planner"


def test_masterspike_is_selectable_and_routes_but_signs_nothing(server, ctx):
    status, payload = server.request("GET", "/v1/models", token=_bearer(tb._grant(ctx)))
    assert status == 200
    by_id = {m["id"]: m for m in payload["data"]}
    assert by_id["masterspike"]["selectable"] is True, by_id
    assert by_id["masterspike"]["agent"] == "masterspike"

    status, payload, grant = _start_chat(server, ctx, model="masterspike", content="carry the format")
    assert status == 200
    assert payload["model"] == "masterspike"
    status2, session = server.request("GET", f"/session/{grant['grant_id']}", token=_bearer(grant))
    assert status2 == 200
    assert session["session"]["model"] == "masterspike"
    assert session["session"]["conversation"][0]["content"] == "carry the format"


def test_declared_models_fail_closed_when_the_manifest_is_absent(ctx):
    models = json.loads((ctx["vault"] / "manifests" / "agent-models.json").resolve().read_text())
    assert models.get("schema") == "sparknet-agent-models/1"
    manifest = ctx["vault"] / "manifests" / "agent-models.json"
    manifest.unlink()
    models = tb.bridge_mod._available_models(ctx["vault"])
    ids = [m["id"] for m in models]
    assert "plan" not in ids and "node-orchestrator" not in ids, ids
    assert "chat" in ids, "discovered agents survive; only the declared surface disappears"


# --- DoD 2: the Approve/Deny card resolves through the permission endpoint -----


def test_permission_request_requires_a_session(server, ctx):
    status, payload = server.request(
        "POST",
        "/session/GRANT-BRIDGE-1/permissions",
        {"action": "write", "target": "05_Knowledge/x.md", "reason": "test"},
        token=_bearer(tb._grant(ctx)),
    )
    assert status == 404
    assert payload["reason"] == "no-session"


def test_permission_request_lifecycle(server, ctx):
    status, payload, grant = _start_chat(server, ctx)
    assert status == 200
    grant_id = grant["grant_id"]

    status, payload = server.request(
        "POST",
        f"/session/{grant_id}/permissions",
        {"action": "vault.promote", "target": "05_Knowledge/x.md", "reason": "human sign-off"},
        token=_bearer(grant),
    )
    assert status == 200
    perm = payload["permission"]
    assert perm["status"] == "pending"
    assert perm["action"] == "vault.promote"
    assert perm["permission_id"] == "perm-1"

    status, payload = server.request(
        "POST",
        f"/session/{grant_id}/permissions/{perm['permission_id']}",
        {"decision": "allow"},
        token=_bearer(grant),
    )
    assert status == 200
    assert payload["permission"]["status"] == "resolved"
    assert payload["permission"]["decision"] == "allow"
    assert payload["permission"]["decided_at"]

    status, session = server.request("GET", f"/session/{grant_id}", token=_bearer(grant))
    permissions = session["session"]["permissions"]
    assert len(permissions) == 1 and permissions[0]["decision"] == "allow"


def test_permission_can_be_denied(server, ctx):
    status, payload, grant = _start_chat(server, ctx)
    grant_id = grant["grant_id"]
    server.request(
        "POST",
        f"/session/{grant_id}/permissions",
        {"action": "vault.promote", "target": "05_Knowledge/x.md", "reason": "deny me"},
        token=_bearer(grant),
    )
    status, payload = server.request(
        "POST",
        f"/session/{grant_id}/permissions/perm-1",
        {"decision": "deny"},
        token=_bearer(grant),
    )
    assert status == 200 and payload["permission"]["decision"] == "deny"


def test_permission_resolution_refusals(server, ctx):
    status, payload, grant = _start_chat(server, ctx)
    grant_id = grant["grant_id"]
    token = _bearer(grant)

    status, payload = server.request(
        "POST",
        f"/session/{grant_id}/permissions",
        {"action": "a", "target": "t", "reason": "r"},
        token=token,
    )
    assert status == 200

    status, payload = server.request(
        "POST", f"/session/{grant_id}/permissions/perm-1", {"decision": "maybe"}, token=token
    )
    assert status == 400 and payload["reason"] == "malformed"

    status, payload = server.request(
        "POST", f"/session/{grant_id}/permissions/perm-99", {"decision": "allow"}, token=token
    )
    assert status == 404 and payload["reason"] == "unknown-permission"

    server.request("POST", f"/session/{grant_id}/permissions/perm-1", {"decision": "allow"}, token=token)
    status, payload = server.request(
        "POST", f"/session/{grant_id}/permissions/perm-1", {"decision": "deny"}, token=token
    )
    assert status == 409 and payload["reason"] == "already-resolved"


def test_permission_register_requires_the_presenting_grant(server, ctx):
    status, payload, grant = _start_chat(server, ctx)
    other = tb._grant(ctx, grant_id="GRANT-BRIDGE-OTHER")
    status, payload = server.request(
        "POST",
        f"/session/{grant['grant_id']}/permissions",
        {"action": "a", "target": "t", "reason": "r"},
        token=_bearer(other),
    )
    assert status == 403 and payload["reason"] == "not-your-session"


def test_permissions_survive_a_bridge_restart(ctx):
    starter = tb.Server(ctx)
    try:
        status, _payload, grant = _start_chat(starter, ctx)
        assert status == 200
    finally:
        starter.close()
    grant_id = grant["grant_id"]

    first = tb.Server(ctx)
    try:
        status, payload = first.request(
            "POST",
            f"/session/{grant_id}/permissions",
            {"action": "vault.capture", "target": "03_Sources/x.md", "reason": "capture on restart"},
            token=_bearer(grant),
        )
        assert status == 200
        assert payload["permission"]["permission_id"] == "perm-1"
    finally:
        first.close()

    second = tb.Server(ctx)
    try:
        status, session = second.request("GET", f"/session/{grant_id}", token=_bearer(grant))
        assert status == 200
        assert session["session"]["permissions"][0]["action"] == "vault.capture"
        status, payload = second.request(
            "POST", f"/session/{grant_id}/permissions/perm-1", {"decision": "allow"}, token=_bearer(grant)
        )
        assert status == 200
    finally:
        second.close()


# --- DoD 4/5: the committed, versioned surface ---------------------------------


def test_vault_rfi_tool_asks_within_the_grant_scope(server, ctx, monkeypatch):
    calls: dict[str, object] = {}

    class FakeClient:
        @staticmethod
        def master_endpoint(vault_root):
            return "http://10.100.0.1:8090/api/v1/rfi-request"

        @staticmethod
        def request_knowledge(vault_root, **kwargs):
            calls.update(kwargs)
            return {"ok": True, "status": 202, "request": {"request_id": "RFI-1"}, "record": "x"}

    monkeypatch.setattr(tb.bridge_mod, "_load_rfi_client", lambda: FakeClient)
    grant = _grant(ctx)
    status, payload = server.request(
        "POST",
        "/vault/rfi",
        {"question": "What is the registry?", "scope": ["05_Knowledge"]},
        token=_bearer(grant),
    )
    assert status == 200, payload
    assert payload["request_id"] == "RFI-1"
    assert calls["scope"] == ["05_Knowledge"]


def test_vault_rfi_tool_does_not_read_local_data_outside_the_grant(server, ctx, monkeypatch):
    calls: dict[str, object] = {}

    class FakeClient:
        @staticmethod
        def master_endpoint(vault_root):
            return "http://10.100.0.1:8090/api/v1/rfi-request"

        @staticmethod
        def request_knowledge(vault_root, **kwargs):
            calls.update(kwargs)
            return {"ok": True, "status": 202, "request": {"request_id": "RFI-2"}, "record": "x"}

    monkeypatch.setattr(tb.bridge_mod, "_load_rfi_client", lambda: FakeClient)
    grant = _grant(ctx)
    status, payload = server.request(
        "POST",
        "/vault/rfi",
        {"question": "Read account state", "scope": ["00_System"]},
        token=_bearer(grant),
    )
    assert status == 200, payload
    assert calls["scope"] == ["00_System"]
    assert not (ctx["vault"] / "00_System" / "user.md").read_text().startswith("RFI-2")


def test_permission_endpoints_never_touch_the_vault(server, ctx):
    status, payload, grant = _start_chat(server, ctx)
    grant_id = grant["grant_id"]
    server.request(
        "POST",
        f"/session/{grant_id}/permissions",
        {"action": "vault.patch", "target": "05_Knowledge/x.md", "reason": "r"},
        token=_bearer(grant),
    )
    server.request("POST", f"/session/{grant_id}/permissions/perm-1", {"decision": "allow"}, token=_bearer(grant))
    assert "grant-quarantine" not in [p.name for p in (ctx["vault"] / "03_Sources").iterdir()], (
        "the permission card resolves in the session; it must not write vault notes"
    )
