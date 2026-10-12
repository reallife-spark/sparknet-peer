"""The signed admin surface (fabric Step 29 phase 2: admin-supervise).

The bridge answers `/admin/<action>` on the plain personality. These tests
prove the guard-protocol ladder `handle_admin` runs: transport shape, Master
signature, the node's own authorization, node binding, capability gating,
replay/expiry, action + entity whitelist, then execution against the node's
own Open WebUI with the node's own token.

Three things stay visible through the whole suite:

* the registry is freshly Master-signed per test, so a capability absent from
  it means the route must answer `capability_missing`, never execute;
* the WebUI is a fake loopback server, because nothing here may depend on a
  real Open WebUI install;
* the bridge never signs anything — every request in this file is signed by the
  Master fixture the way the real Master's signer would.
"""

from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import shutil
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

VAULT_ROOT = Path(__file__).resolve().parent.parent
SHARED_DIR = VAULT_ROOT.parent / "shared"
NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _load(name: str, label: str):
    path = SHARED_DIR / f"{name}.py"
    if not path.is_file():
        pytest.skip(f"shared implementation not present at {path}")
    spec = importlib.util.spec_from_file_location(label, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


authz = _load("sparknet-authz", "sparknet_authz_admin_test")
prov = _load("sparknet-provenance", "sparknet_provenance_admin_test")


def _load_bridge():
    path = VAULT_ROOT / "services" / "sparknet-dispatch-bridge.py"
    spec = importlib.util.spec_from_file_location("sparknet_dispatch_bridge_admint", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge_mod = _load_bridge()


def _master_pub_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def master_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


class FakeWebUI:
    """A minimal Open WebUI 0.12 admin surface on a loopback port pytest owns.

    Sigimport surface: signin mints a token, the users/knowledge/chats endpoints
    answer the 0.12 envelopes the bridge's closed action mapping targets. Role
    changes are recorded so a test can assert what was actually sent.
    """

    def __init__(self, tmp_path: Path):
        self.recorded_roles: list[dict] = []
        self.recorded_calls: list[tuple[str, str]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self):
        webui = self

        class Handler(BaseHTTPRequestHandler):
            def _read(self) -> dict | None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                return json.loads(raw) if raw else None

            def _send(self, payload: Any, code: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self, method: str) -> None:
                webui.recorded_calls.append((method, self.path))
                if method == "POST" and self.path == "/api/v1/auths/signin":
                    self._send({"token": "fake-webui-token"})
                    return
                if method == "GET" and self.path == "/api/v1/users/":
                    self._send({"users": [{"id": "user-07", "name": "Ana", "role": "user"}]})
                    return
                if self.path.startswith("/api/v1/users/") and self.path.endswith("/update"):
                    body = self._read() or {}
                    webui.recorded_roles.append({"user": self.path.split("/")[4], "role": body.get("role")})
                    if body.get("role") == "superuser":
                        self._send({"detail": "that role is not assignable here"}, 403)
                        return
                    self._send({"id": self.path.split("/")[4], "role": body.get("role")})
                    return
                if self.path.startswith("/api/v1/users/"):
                    self._send({"id": self.path.rsplit("/", 1)[-1], "name": "Ana", "role": "user"})
                    return
                if method == "GET" and self.path == "/api/v1/knowledge/":
                    self._send({"items": []})
                    return
                if self.path.startswith("/api/v1/chats/"):
                    self._send([])
                    return
                self._send({"detail": "not found"}, 404)

            def do_GET(self) -> None:  # noqa: N802
                self._route("GET")


            def do_POST(self) -> None:  # noqa: N802
                self._route("POST")


            def do_DELETE(self) -> None:  # noqa: N802
                self._route("DELETE")


            def log_message(self, *_: Any) -> None:
                pass

        return Handler

    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


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

    # The Master's public key is a manifest the guard trusts; publish the fixture's.
    raw = master_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub_manifest = vault / "manifests" / "keys" / "master-ed25519.pub.b64"
    pub_manifest.parent.mkdir(parents=True, exist_ok=True)
    pub_manifest.write_text(base64.b64encode(raw).decode("ascii") + "\n")

    identity = json.loads((vault / "manifests" / "node-identity.json").read_text())
    registry = {
        "schema_version": "1.0",
        "master_public_key_b64": _master_pub_b64(master_key),
        "description": "test registry",
        "authorizations": [],
    }
    registry_path = vault / "manifests" / "node-authorizations.json"
    registry_path.write_text(json.dumps(registry, indent=2) + "\n")

    webui = FakeWebUI(tmp_path)
    pw_file = tmp_path / "webui-admin-password"
    pw_file.write_text("test-password\n")
    monkeypatch.setenv("SPARKNET_WEBUI", webui.base())
    monkeypatch.setenv("SPARKNET_WEBUI_ADMIN_EMAIL", "admin@sparknet.local")
    monkeypatch.setenv("SPARKNET_WEBUI_ADMIN_PASSWORD_FILE", str(pw_file))

    return {
        "vault": vault,
        "identity": identity,
        "registry_path": registry_path,
        "webui": webui,
        "master_key": master_key,
    }


@pytest.fixture
def grant_registry(ctx: dict, master_key: Ed25519PrivateKey) -> dict:
    """Give this node admin-supervise (plus telemetry) in its own registry."""

    def _set(capabilities: list[str]) -> dict:
        identity = ctx["identity"]
        entry = authz.sign_authorization(identity, master_key, capabilities, now=NOW)
        registry = json.loads(ctx["registry_path"].read_text())
        registry["authorizations"] = [entry]
        ctx["registry_path"].write_text(json.dumps(registry, indent=2) + "\n")
        return registry

    return _set


def _admin_request(master_key: Ed25519PrivateKey, *, action: str = "set_user_role", **overrides) -> dict:
    """A signed admin request envelope, the way the Master's signer would produce it."""
    unsigned = {
        "request_id": "admin-req-0001",
        "conversation_id": None,
        "master_node_id": "master-0001",
        "target_node_id": None,  # filled by the test from the fixture identity
        "target_fingerprint": None,
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "nonce": "nonce-0001",
        "action": action,
        "user_id": "user-07",
        "user_email": "ana@example.test",
        "role": "user",
        "kb_id": None,
        "chat_id": None,
    }
    unsigned.update(overrides)
    message = authz.admin_request_message(unsigned)
    signature = base64.b64encode(master_key.sign(message)).decode("ascii")
    return {**unsigned, "signature_b64": signature}


class Server:
    """A live loopback bridge (the plain personality), port owned by pytest."""

    def __init__(self, ctx: dict):
        self.bridge = bridge_mod.Bridge(
            ctx["vault"], registry=ctx["registry_path"], clock=lambda: NOW
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            bridge_mod._make_handler(self.bridge, bridge_mod.STATES, bridge_mod._make_router(bridge_mod.STATES)),
        )
        self.server.router = bridge_mod._make_router(bridge_mod.STATES)
        self.server.public_key = None
        self.server.clock = lambda: NOW
        self.server.max_age_seconds = 3600
        self.server.loopback_only_routes = ()
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def post(self, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = token
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", path, body=json.dumps(body).encode(), headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw)
        finally:
            conn.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def server(ctx: dict):
    srv = Server(ctx)
    try:
        yield srv
    finally:
        srv.close()


def _target(ctx: dict) -> dict:
    identity = ctx["identity"]
    return {
        "target_node_id": identity["node_id"],
        "target_fingerprint": authz.fingerprint(identity),
    }


# --- the guard-protocol ladder -----------------------------------------------


def test_the_signed_admin_route_needs_no_bearer_grant(server, ctx, master_key, grant_registry):
    """/admin/* is Master-signed, so a presented grant must not be required."""
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="set_user_role", **_target(ctx))
    status, payload = server.post("/admin/set_user_role", body)
    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["webui_status"] == 200
    assert payload["result"]["role"] == "user"


def test_set_user_role_executes_against_the_node_own_webui(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="set_user_role", role="admin", **_target(ctx))
    status, payload = server.post("/admin/set_user_role", body)
    assert status == 200, payload
    assert payload["result"]["role"] == "admin"
    assert ctx["webui"].recorded_roles == [
        {"user": "user-07", "role": "admin"}
    ], "the role change must land on the node's own WebUI with the node's own token"


def test_approve_user_is_a_role_user_update(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="approve_user", **_target(ctx))
    status, payload = server.post("/admin/approve_user", body)
    assert status == 200, payload
    assert ctx["webui"].recorded_roles == [{"user": "user-07", "role": "user"}]


def test_list_users_roundtrips_the_open_webui_envelope(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="list_users", **_target(ctx))
    status, payload = server.post("/admin/list_users", body)
    assert status == 200, payload
    assert payload["result"]["users"][0]["id"] == "user-07"


def test_a_tampered_entity_field_invalidates_the_signature(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="set_user_role", role="user", **_target(ctx))
    body["role"] = "admin"  # not what was signed
    status, payload = server.post("/admin/set_user_role", body)
    assert status == 403
    assert payload["reason"] == "bad-signature"


def test_without_the_capability_the_route_stays_inert(server, ctx, master_key, grant_registry):
    """grant-registry without admin-supervise: nothing runs, even though the
    signature verifies and the trust root binds."""
    grant_registry(["telemetry"])
    body = _admin_request(master_key, action="set_user_role", **_target(ctx))
    status, payload = server.post("/admin/set_user_role", body)
    assert status == 403
    assert payload["reason"] == "capability_missing"
    assert ctx["webui"].recorded_roles == []


def test_without_a_master_key_the_route_is_unavailable(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    (ctx["vault"] / "manifests" / "keys" / "master-ed25519.pub.b64").unlink()
    body = _admin_request(master_key, action="list_users", **_target(ctx))
    status, payload = server.post("/admin/list_users", body)
    assert status == 503
    assert payload["reason"] == "no-master-key"


def test_a_request_bound_to_another_node_is_refused(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(
        master_key,
        action="list_users",
        target_node_id="peer-far-away",
        target_fingerprint="f" * 64,
    )
    status, payload = server.post("/admin/list_users", body)
    assert status == 403
    assert payload["reason"] == "wrong-target"


def test_a_smuggled_signature_is_refused_as_malformed(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="list_users", **_target(ctx))
    del body["signature_b64"]
    status, payload = server.post("/admin/list_users", body)
    assert status == 400
    assert payload["reason"] == "malformed-request"


def test_a_replayed_request_id_nonce_is_refused(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="set_user_role", **_target(ctx))
    status, payload = server.post("/admin/set_user_role", body)
    assert status == 200, payload
    status, payload = server.post("/admin/set_user_role", body)
    assert status == 409
    assert payload["reason"] == "replayed"


def test_an_expired_admin_request_is_refused(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(
        master_key,
        action="set_user_role",
        **_target(ctx),
        issued_at=(NOW - timedelta(hours=2)).isoformat(),
        expires_at=(NOW - timedelta(hours=1)).isoformat(),
    )
    status, payload = server.post("/admin/set_user_role", body)
    assert status == 403
    assert payload["reason"] == "expired"


def test_a_missing_entity_field_is_refused_before_the_webui_is_touched(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="set_user_role", role=None, **_target(ctx))
    status, payload = server.post("/admin/set_user_role", body)
    assert status == 400
    assert payload["reason"] == "missing-field"
    assert ctx["webui"].recorded_roles == []


def test_an_action_outside_the_open_vocabulary_is_refused(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = {
        "request_id": "admin-req-0001",
        "master_node_id": "master-0001",
        "target_node_id": ctx["identity"]["node_id"],
        "target_fingerprint": authz.fingerprint(ctx["identity"]),
        "issued_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "nonce": "nonce-0001",
        "action": "root-everything",
        "signature_b64": base64.b64encode(b"garbage").decode(),
    }
    status, payload = server.post("/admin/root-everything", body)
    assert status == 400
    assert payload["reason"] == "not-canonicalisable"


def test_a_path_action_that_conflicts_with_the_signed_action_is_refused(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="get_user", user_id="user-07", **_target(ctx))
    status, payload = server.post("/admin/set_user_role", body)  # path disagrees with body
    assert status == 400
    assert payload["reason"] == "action-mismatch"


def test_the_webui_refusal_is_surfaced_verbatim(server, ctx, master_key, grant_registry):
    """A 4xx from the node's own WebUI is the peer's answer, not a reparse.

    The fake WebUI refuses `role: superuser`; the bridge must hand that refusal
    through unchanged rather than manufacture its own success or failure.
    """
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="set_user_role", role="superuser", **_target(ctx))
    status, payload = server.post("/admin/set_user_role", body)
    assert status == 403
    assert payload["reason"] == "webui-refused"
    assert payload["webui_status"] == 403
    assert payload["detail"] == {"detail": "that role is not assignable here"}


# --- the event feed (list_events) --------------------------------------------


def _spool(ctx: dict, rows: list[dict]) -> None:
    """Write the node's auth-event spool where the bridge reads it."""
    spool = bridge_mod._state_dir(ctx["vault"]) / "admin-events" / "events.jsonl"
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def test_list_events_reads_the_node_own_spool(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    _spool(ctx, [
        {"event": "auth.signup", "ts": "t1", "actor": "ana@example.test", "node": "n"},
        {"event": "auth.login", "ts": "t2", "actor": "ana@example.test", "node": "n"},
    ])
    body = _admin_request(master_key, action="list_events", **_target(ctx))
    status, payload = server.post("/admin/list_events", body)
    assert status == 200, payload
    assert payload["ok"] is True
    assert payload["action"] == "list_events"
    assert [e["event"] for e in payload["events"]] == ["auth.signup", "auth.login"]
    assert payload["count"] == 2 and payload["truncated"] is False


def test_list_events_on_an_empty_node_is_an_empty_feed_not_an_error(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    body = _admin_request(master_key, action="list_events", **_target(ctx))
    status, payload = server.post("/admin/list_events", body)
    assert status == 200, payload
    assert payload["events"] == [] and payload["count"] == 0


def test_list_events_needs_the_capability_like_any_other_action(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry"])
    body = _admin_request(master_key, action="list_events", **_target(ctx))
    status, payload = server.post("/admin/list_events", body)
    assert status == 403
    assert payload["reason"] == "capability_missing"


def test_list_events_skips_corrupt_rows_and_caps_the_response(server, ctx, master_key, grant_registry):
    grant_registry(["telemetry", "admin-supervise"])
    good = [{"event": "auth.login", "ts": f"t{i}", "actor": "a@b.test", "node": "n"}
            for i in range(bridge_mod.ADMIN_EVENTS_MAX + 3)]
    spool = bridge_mod._state_dir(ctx["vault"]) / "admin-events" / "events.jsonl"
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text("not json\n" + "".join(json.dumps(r) + "\n" for r in good), encoding="utf-8")
    body = _admin_request(master_key, action="list_events", **_target(ctx))
    status, payload = server.post("/admin/list_events", body)
    assert status == 200, payload
    assert payload["count"] == bridge_mod.ADMIN_EVENTS_MAX
    assert payload["total"] == bridge_mod.ADMIN_EVENTS_MAX + 3
    assert payload["truncated"] is True
    assert payload["events"][-1]["ts"] == good[-1]["ts"]