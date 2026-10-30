"""The dispatch bridge becomes a server (peer PLAN Step 4).

Step 4 does not chase the agent runtime — it builds the surface the later steps
stand on, and these tests prove each DoD:

* DoD 1 — the bridge answers OpenAI-compatible HTTP on a live loopback socket
  and `/v1/models` lists the node's agents (the ones declared on disk).
* DoD 2 — sessions are persistent and keyed by `grant_id`; no conversation
  identity derives from content hashing (the session file names the grant, and
  the conversation is plain turns).
* DoD 3 — a session survives a bridge restart (a second server over the same
  state dir still reads the conversation).
* DoD 4 — `/vault/*` is grant-checked (mint-then-present, replay refused),
  canonicalises paths so nothing escapes the vault or a sub-vault mount, and
  every mutation appends a dual-signed `grant_use` block — through the one
  shared implementation, which this suite checks by watching the ledger, not by
  trusting the bridge.
* DoD 5 — `manifests/vault-openapi.json` describes the vault tools.

All fixtures mirror `test_grants.py`: a relocatable vault copy, a fresh node
key and user record in an isolated state dir, a freshly Master-signed registry,
and grants minted at the fixed instant so verification is deterministic.
"""

from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import shutil
import sys
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest
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


authz = _load("sparknet-authz", "sparknet_authz_bridge")
prov = _load("sparknet-provenance", "sparknet_provenance_bridge")


def _load_bridge() -> ModuleType:
    path = VAULT_ROOT / "services" / "sparknet-dispatch-bridge.py"
    spec = importlib.util.spec_from_file_location("sparknet_dispatch_bridge_t", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bridge_mod = _load_bridge()


def _b64(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def _bearer(grant: dict) -> str:
    return f"Bearer {_b64(grant)}"


def _master_pub_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


# --- fixtures ---------------------------------------------------------------


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
        grant_id="GRANT-BRIDGE-1",
        user_identity="U-20161",
        conversation_key="01010101-0101-0101-0101-010101010101-1",
        capabilities=["p2p-sync"],
        vault_scope=["05_Knowledge"],
        ttl_minutes=480,
        nonce="bridge-nonce-0001",
        master_private_key=ctx["master_key"],
        registry=ctx["registry"],
        now=NOW,
    )
    params.update(overrides)
    return authz.mint_grant(**params)


class Server:
    """A live loopback bridge, cheaply and on a port pytest owns."""

    def __init__(self, ctx: dict):
        self.bridge = bridge_mod.Bridge(
            ctx["vault"], registry=ctx["vault"] / "manifests" / "node-authorizations.json", clock=lambda: NOW
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

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def request(self, method: str, path: str, body: object | None = None, token: str | None = None):
        headers = {}
        if token:
            headers["Authorization"] = token
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        else:
            payload = None
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = raw
            return resp.status, data
        finally:
            conn.close()


@pytest.fixture
def server(ctx) -> Server:
    srv = Server(ctx)
    try:
        yield srv
    finally:
        srv.close()


# --- DoD 1: OpenAI-compatible surface, /v1/models lists the node's agents -----


def test_models_lists_the_declared_agents(server, ctx):
    status, payload = server.request("GET", "/v1/models", token=_bearer(_grant(ctx)))
    assert status == 200
    ids = [m["id"] for m in payload["data"]]
    assert "chat" in ids, "the .opencode/agents/chat.md agent must be a model"
    assert all(isinstance(i, str) and i for i in ids)
    declared = [i for m in payload["data"] for i in [m["id"]] if m.get("selectable")]
    discovered = [i for m in payload["data"] if not m.get("selectable") for i in [m["id"]]]
    assert declared[:4] == ["plan", "build", "vault", "node-orchestrator"], (
        "the Step 5 declared roles lead the picker, in manifest order"
    )
    assert discovered == sorted(discovered), "discovered agents stay sorted among themselves"


def test_plain_routes_require_a_presented_grant(server):
    status, payload = server.request("GET", "/v1/models")
    assert status == 401
    assert payload["reason"] == "missing-grant"


def test_openapi_spec_is_served_on_the_plain_surface(server, ctx):
    """Open WebUI fetches the vault tool spec from the bridge itself.

    The tool-server connection points at ``GET /v1/openapi.json``, so the spec
    must be served as a normal grant-checked plain route — matching the
    committed manifest exactly.
    """
    committed = json.loads((VAULT_ROOT / "manifests" / "vault-openapi.json").read_text())
    status, payload = server.request("GET", "/v1/openapi.json")
    assert status == 401
    assert payload["reason"] == "missing-grant"

    status, payload = server.request("GET", "/v1/openapi.json", token=_bearer(_grant(ctx)))
    assert status == 200
    assert payload == committed
    assert "/vault/capture" in payload["paths"]
    assert payload["servers"][0]["url"] == "http://127.0.0.1:4096"


def test_unknown_bearer_is_refused_before_any_route(server):
    grant = {"grant_id": "forged", "signature_b64": "AAAA"}
    status, payload = server.request("GET", "/v1/models", token=_bearer(grant))
    assert status == 403
    assert payload["reason"] == "grant-refused"


def test_signed_status_surface_does_not_leak_into_the_plain_surface(server):
    """Unsigned traffic to /v1/status must still hit the signed personality."""
    status, payload = server.request("POST", "/v1/status", {"probe": 1})
    assert status == 401
    assert payload["reason"] == "unsigned"


# --- DoD 2 + 3: persistent grant-keyed sessions, restart survival ------------


def test_chat_completions_create_a_grant_keyed_session(server, ctx):
    grant = _grant(ctx)
    status, payload = server.request(
        "POST",
        "/v1/chat/completions",
        {"model": "chat", "messages": [{"role": "user", "content": "hello bridge"}]},
        token=_bearer(grant),
    )
    assert status == 200
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "chat"
    session_file = ctx["state"] / "sessions" / f"{grant['grant_id']}.json"
    assert session_file.is_file(), "the session must be written to the state dir, keyed by grant_id"
    session = json.loads(session_file.read_text())
    assert session["grant_id"] == grant["grant_id"]
    assert session["conversation"][0]["content"] == "hello bridge"
    assert "chatcmpl-" in payload["id"]


def test_unknown_model_is_refused(server, ctx):
    status, payload = server.request(
        "POST",
        "/v1/chat/completions",
        {"model": "warp-drive", "messages": [{"role": "user", "content": "x"}]},
        token=_bearer(_grant(ctx)),
    )
    assert status == 400
    assert payload["reason"] == "unknown-model"


def test_session_survives_a_bridge_restart(ctx):
    first = Server(ctx)
    grant = _grant(ctx)
    status, _ = first.request(
        "POST",
        "/v1/chat/completions",
        {"model": "chat", "messages": [{"role": "user", "content": "persist me"}]},
        token=_bearer(grant),
    )
    assert status == 200
    first.close()

    second = Server(ctx)
    try:
        status, payload = second.request("GET", f"/session/{grant['grant_id']}", token=_bearer(_grant(ctx)))
        assert status == 200
        turns = payload["session"]["conversation"]
        assert [t["content"] for t in turns if t["role"] == "user"] == ["persist me"]
    finally:
        second.close()


def test_a_session_cannot_be_read_by_another_grant(server, ctx):
    first = _grant(ctx, grant_id="GRANT-ALPHA", nonce="alpha-nonce")
    server.request(
        "POST",
        "/v1/chat/completions",
        {"model": "chat", "messages": [{"role": "user", "content": "secret"}]},
        token=_bearer(first),
    )
    other = _grant(ctx, grant_id="GRANT-BETA", nonce="beta-nonce")
    status, payload = server.request("GET", "/session/GRANT-ALPHA", token=_bearer(other))
    assert status == 403
    assert payload["reason"] == "not-your-session"


# --- DoD 4: /vault/* grant-checked, canonicalised, ledger-appended ------------


def test_vault_list_and_cat_stay_within_scope(server, ctx):
    grant = _grant(ctx, vault_scope=["05_Knowledge"])
    status, payload = server.request("GET", "/vault/list?recursive=1", token=_bearer(grant))
    assert status == 200
    assert payload["paths"], "the vault copy has 05_Knowledge notes"
    assert all(rel.startswith("05_Knowledge/") for rel in payload["paths"]), "listing must not leak other mounts"

    status, payload = server.request(
        "GET",
        f"/vault/cat?path={payload['paths'][0]}",
        token=_bearer(_grant(ctx, nonce="list-nonce-2")),
    )
    assert status == 200
    assert isinstance(payload["content"], str) and payload["content"], "cat returns the note's real content"


def test_vault_list_refuses_to_list_outside_scope(server, ctx):
    grant = _grant(ctx, vault_scope=["03_Sources"])
    status, payload = server.request("GET", "/vault/list?recursive=1", token=_bearer(grant))
    assert status == 200
    assert all(rel.startswith("03_Sources/") for rel in payload["paths"])


def test_capture_writes_a_signed_note_and_a_grant_use_block(server, ctx):
    grant = _grant(ctx)
    status, payload = server.request(
        "POST",
        "/vault/capture",
        {
            "target": "05_Knowledge/concepts/bridge-capture.md",
            "body": "# bridge capture\n\nHTTP captured.\n",
            "session": "webui-7",
        },
        token=_bearer(grant),
    )
    assert status == 200, payload
    assert payload["decision"] == "granted"
    note = ctx["vault"] / "05_Knowledge" / "concepts" / "bridge-capture.md"
    assert note.is_file()
    outcome, detail = prov.verify_note(note, None, ctx["vault"])
    assert outcome == "valid", detail

    ledger = json.loads((ctx["vault"] / "blockchain" / "ledger.json").read_text())
    uses = [
        b
        for b in ledger["blocks"]
        if b["data"].get("event") == "grant_use" and b["data"].get("grant_id") == grant["grant_id"]
    ]
    assert len(uses) == 1
    assert uses[0]["data"]["decision"] == "granted"
    assert uses[0]["data"]["session"] == "webui-7"
    assert prov.verify_approval_blocks(ctx["vault"]) == []
    assert prov.verify_chain(ledger)[0]


def test_out_of_scope_capture_is_quarantined_not_placed(server, ctx):
    grant = _grant(ctx, vault_scope=["03_Sources"])
    status, payload = server.request(
        "POST",
        "/vault/capture",
        {"target": "05_Knowledge/concepts/bridge-sneak.md", "body": "# sneak\n", "session": "webui-7"},
        token=_bearer(grant),
    )
    assert status == 200, payload
    assert payload["decision"] == "quarantined"
    assert not (ctx["vault"] / "05_Knowledge" / "concepts" / "bridge-sneak.md").exists()
    quarantined = ctx["vault"] / "03_Sources" / "grant-quarantine" / grant["grant_id"] / "bridge-sneak.md"
    assert quarantined.is_file(), "material is never silently lost"


def test_replaying_a_spent_grant_is_refused(server, ctx):
    grant = _grant(ctx)
    first = server.request(
        "POST",
        "/vault/capture",
        {"target": "05_Knowledge/concepts/one.md", "body": "# one\n", "session": "webui-7"},
        token=_bearer(grant),
    )
    assert first[0] == 200
    second = server.request(
        "POST",
        "/vault/capture",
        {"target": "05_Knowledge/concepts/two.md", "body": "# two\n", "session": "webui-8"},
        token=_bearer(grant),  # same grant, same nonce — spent by the first capture
    )
    assert second[0] == 403
    assert not (ctx["vault"] / "05_Knowledge" / "concepts" / "two.md").exists()


def test_paths_cannot_escape_the_vault(server, ctx):
    grant = _grant(ctx)
    for bad in ("../escape.md", "/etc/passwd", "05_Knowledge/../../outside.md"):
        status, payload = server.request(
            "POST",
            "/vault/capture",
            {"target": bad, "body": "# escape\n", "session": "webui-7"},
            token=_bearer(grant),
        )
        assert status == 400, bad
        assert payload["reason"] == "bad-path"
    status, payload = server.request("GET", "/vault/cat?path=../../etc/passwd", token=_bearer(grant))
    assert status == 400
    assert payload["reason"] == "bad-path"


def test_promote_moves_a_pending_draft_inside_scope(server, ctx):
    (ctx["vault"] / "03_Sources" / "waiting-draft.md").write_text("# waiting-draft\n\nraw capture.\n")
    grant = _grant(ctx, vault_scope=["03_Sources"])
    status, payload = server.request(
        "POST",
        "/vault/promote",
        {"source": "03_Sources/waiting-draft.md", "target": "03_Sources/waiting-draft-final.md", "session": "webui-7"},
        token=_bearer(grant),
    )
    assert status == 200, payload
    assert not (ctx["vault"] / "03_Sources" / "waiting-draft.md").exists()
    assert (ctx["vault"] / "03_Sources" / "waiting-draft-final.md").is_file()
    uses = [
        b["data"]
        for b in json.loads((ctx["vault"] / "blockchain" / "ledger.json").read_text())["blocks"]
        if b["data"].get("event") == "grant_use"
    ]
    assert any(u["decision"] == "granted" and u["note_refs"] == ["03_Sources/waiting-draft-final.md"] for u in uses)


def test_promote_refuses_a_signed_note(server, ctx):
    existing = ctx["vault"] / "03_Sources" / "arch_blueprint.md"
    rel = "03_Sources/arch_blueprint.md"
    grant = _grant(ctx, vault_scope=["03_Sources"])
    status, payload = server.request(
        "POST",
        "/vault/promote",
        {"source": rel, "target": "03_Sources/reblueprinted.md", "session": "webui-7"},
        token=_bearer(grant),
    )
    assert status == 400
    assert "draft" in payload["detail"] and "refuses" in payload["detail"]
    assert existing.is_file(), "nothing may move for a refused promote"


def test_patch_re_signs_and_records_the_mutation(server, ctx):
    grant = _grant(ctx)
    server.request(
        "POST",
        "/vault/capture",
        {
            "target": "05_Knowledge/concepts/patchable.md",
            "body": "# patchable\n\nKeep this line.\n",
            "session": "webui-7",
        },
        token=_bearer(grant),
    )
    patch_grant = _grant(ctx, grant_id="GRANT-PATCH", nonce="patch-nonce")
    status, payload = server.request(
        "POST",
        "/vault/patch",
        {
            "target": "05_Knowledge/concepts/patchable.md",
            "patch": {"old": "Keep this line.", "new": "Edited line."},
            "session": "webui-7",
        },
        token=_bearer(patch_grant),
    )
    assert status == 200, payload
    note = ctx["vault"] / "05_Knowledge" / "concepts" / "patchable.md"
    assert "Edited line." in note.read_text()
    outcome, detail = prov.verify_note(note, None, ctx["vault"])
    assert outcome == "valid", "a patched note must be re-signed, not left stale:content"


def test_patch_must_match_exactly_one_occurrence(server, ctx):
    grant = _grant(ctx)
    server.request(
        "POST",
        "/vault/capture",
        {"target": "05_Knowledge/concepts/ambig.md", "body": "# ambig\n\nsame same\n", "session": "webui-7"},
        token=_bearer(grant),
    )
    status, payload = server.request(
        "POST",
        "/vault/patch",
        {
            "target": "05_Knowledge/concepts/ambig.md",
            "patch": {"old": "same", "new": "different"},
            "session": "webui-7",
        },
        token=_bearer(_grant(ctx, nonce="ambig-2")),
    )
    assert status == 400
    assert "exactly one" in payload["detail"]


# --- the session "all vault tools" toggle -----------------------------------


def _open_session(server, ctx, grant) -> None:
    server.request(
        "POST",
        "/v1/chat/completions",
        {"model": "chat", "messages": [{"role": "user", "content": "open"}]},
        token=_bearer(grant),
    )


def test_vault_access_toggle_rejects_an_unknown_mode(server, ctx):
    grant = _grant(ctx)
    status, payload = server.request(
        "POST", f"/session/{grant['grant_id']}/vault-access", {"mode": "sometimes"}, token=_bearer(grant)
    )
    assert status == 400
    assert payload["reason"] == "malformed"


def test_vault_access_all_requires_a_scope(server, ctx):
    grant = _grant(ctx, vault_scope=[])
    status, payload = server.request(
        "POST", f"/session/{grant['grant_id']}/vault-access", {"mode": "all"}, token=_bearer(grant)
    )
    assert status == 403
    assert payload["reason"] == "no-scope"


def test_vault_access_all_permits_repeated_writes_and_reads(server, ctx):
    grant = _grant(ctx)
    _open_session(server, ctx, grant)
    status, payload = server.request(
        "POST", f"/session/{grant['grant_id']}/vault-access", {"mode": "all"}, token=_bearer(grant)
    )
    assert status == 200, payload
    assert payload["vault_access"] == "all"

    first = server.request(
        "POST",
        "/vault/capture",
        {"target": "05_Knowledge/concepts/all-1.md", "body": "# all-1\n", "session": "webui-all"},
        token=_bearer(grant),
    )
    assert first[0] == 200, first[1]
    second = server.request(
        "POST",
        "/vault/capture",
        {"target": "05_Knowledge/concepts/all-2.md", "body": "# all-2\n", "session": "webui-all"},
        token=_bearer(grant),
    )
    assert second[0] == 200, second[1]
    assert (ctx["vault"] / "05_Knowledge" / "concepts" / "all-1.md").is_file()
    assert (ctx["vault"] / "05_Knowledge" / "concepts" / "all-2.md").is_file()

    read = server.request("GET", "/vault/cat?path=05_Knowledge/concepts/all-1.md", token=_bearer(grant))
    assert read[0] == 200, "a session in all-mode must still read after its first write"


def test_vault_access_per_tool_restores_the_one_shot_rule(server, ctx):
    grant = _grant(ctx)
    _open_session(server, ctx, grant)
    server.request("POST", f"/session/{grant['grant_id']}/vault-access", {"mode": "all"}, token=_bearer(grant))
    first = server.request(
        "POST",
        "/vault/capture",
        {"target": "05_Knowledge/concepts/toggle-1.md", "body": "# toggle-1\n", "session": "webui-all"},
        token=_bearer(grant),
    )
    assert first[0] == 200, first[1]
    back = server.request(
        "POST", f"/session/{grant['grant_id']}/vault-access", {"mode": "per-tool"}, token=_bearer(grant)
    )
    assert back[0] == 200 and back[1]["vault_access"] == "per-tool"
    second = server.request(
        "POST",
        "/vault/capture",
        {"target": "05_Knowledge/concepts/toggle-2.md", "body": "# toggle-2\n", "session": "webui-all"},
        token=_bearer(grant),
    )
    assert second[0] == 403, "switching back to per-tool must re-spend the nonce"
    assert not (ctx["vault"] / "05_Knowledge" / "concepts" / "toggle-2.md").exists()


# --- DoD 5: the OpenAPI spec is present and describes the tools ---------------


def test_openapi_spec_describes_the_vault_tools() -> None:
    spec = json.loads((VAULT_ROOT / "manifests" / "vault-openapi.json").read_text())
    operations = {
        op.get("operationId")
        for item in spec["paths"].values()
        for op in item.values()
        if isinstance(op, dict) and op.get("operationId")
    }
    for tool in ("vault_list", "vault_cat", "vault_capture", "vault_promote", "vault_patch", "listModels"):
        assert tool in operations, f"OpenAPI must describe the {tool} operation"
    assert "/v1/models" in spec["paths"]
    assert "BearerAuth" in spec.get("components", {}).get("securitySchemes", {})


# --- Step 66B: the department boundary is wired to the signed registry ---------


def _identity(node_id: str, overlay: str) -> dict:
    return {
        "node_id": node_id,
        "role": "peer",
        "wireguard_public_key_b64": base64.b64encode(b"fake-wg-public-key-for-tests!!".ljust(32, b"0")[:32]).decode(),
        "overlay_ipv4": overlay,
        "requested_capabilities": ["telemetry"],
    }


def _registry_with_departments(ctx: dict, *, self_caps: list[str], self_dept, other_dept) -> None:
    """Rewrite the node's registry so two nodes carry Master-attested departments."""
    caps = list(self_caps)
    entries = [authz.sign_authorization(ctx["identity"], ctx["master_key"], caps, department=self_dept)]
    other = _identity("node-b", "10.100.0.9")
    entries.append(authz.sign_authorization(other, ctx["master_key"], ["telemetry"], department=other_dept))
    registry = {
        "schema_version": "1.0",
        "master_public_key_b64": _master_pub_b64(ctx["master_key"]),
        "description": "test registry with attested departments",
        "authorizations": entries,
    }
    path = ctx["vault"] / "manifests" / "node-authorizations.json"
    path.write_text(json.dumps(registry, indent=2) + "\n")
    # Every entry must still verify: the point of the field is that it is signed,
    # so an attestation that broke the signature would prove nothing.
    public_key = ctx["master_key"].public_key()
    for entry in registry["authorizations"]:
        assert authz.verify_signature(public_key, entry["signature_b64"], authz.authorization_message(entry))


def _bridge(ctx: dict):
    return bridge_mod.Bridge(
        ctx["vault"], registry=ctx["vault"] / "manifests" / "node-authorizations.json", clock=lambda: NOW
    )


def test_the_bridge_reads_departments_from_the_signed_registry(ctx: dict) -> None:
    """Attestation comes from the registry, not from anything a node claims."""
    _registry_with_departments(ctx, self_caps=["telemetry"], self_dept="research", other_dept="finance")
    bridge = _bridge(ctx)
    own_id = str(bridge.node_identity()["node_id"])
    assert bridge._department_of(own_id) == "research"
    assert bridge._department_of("node-b") == "finance"
    assert bridge._department_of("nobody") is None, "an unknown node is unattested, not assumed"


def test_an_unattested_department_is_none_not_empty(ctx: dict) -> None:
    _registry_with_departments(ctx, self_caps=["telemetry"], self_dept=None, other_dept=None)
    bridge = _bridge(ctx)
    own_id = str(bridge.node_identity()["node_id"])
    assert bridge._department_of(own_id) is None
    assert bridge._department_of("node-b") is None


def test_the_regime_the_bridge_builds_refuses_a_cross_department_offer(ctx: dict) -> None:
    """End to end: signed registry -> resolver -> engine -> refusal.

    Without this the engine's rule would be correct and inert, because the bridge
    is what constructs the regime a real request reaches.
    """
    _registry_with_departments(ctx, self_caps=["telemetry"], self_dept="research", other_dept="finance")
    bridge = _bridge(ctx)
    regime = bridge._share_regime()
    with pytest.raises(Exception) as exc:
        regime.offer("kb-view", toward="node-b")
    assert exc.value.status == 403 and exc.value.reason == "cross-department"


def test_the_regime_allows_it_once_the_registry_grants_the_capability(ctx: dict) -> None:
    xdept = "cross-department-share"
    _registry_with_departments(ctx, self_caps=["telemetry", xdept], self_dept="research", other_dept="finance")
    bridge = _bridge(ctx)
    regime = bridge._share_regime()
    own_id = str(bridge.node_identity()["node_id"])
    assert bridge._holds_capability(own_id, xdept) is True
    offer = regime.offer("kb-view", toward="node-b")
    assert regime.get(offer["share_id"])["departments"]["crosses"] is True
    assert regime.get(offer["share_id"])["departments"]["permitted_by"] == xdept


def test_the_regime_binds_nothing_when_no_department_is_attested(ctx: dict) -> None:
    """A deployment whose Master attests no departments shares as it always did."""
    _registry_with_departments(ctx, self_caps=["telemetry"], self_dept=None, other_dept=None)
    bridge = _bridge(ctx)
    regime = bridge._share_regime()
    offer = regime.offer("kb-view", toward="node-b")
    recorded = regime.get(offer["share_id"])["departments"]
    assert recorded["crosses"] is False
    assert recorded["mine"] is None and recorded["theirs"] is None


def test_the_governance_mode_tracks_a_registry_change_without_a_restart(ctx: dict) -> None:
    """A third node joining must move the mode, or ratification is skipped.

    `peer_pair` is symmetric and has **no Master arbitration**; `master_governed`
    requires the Master to ratify every cross-node share. A bridge that froze the
    mesh size at construction kept answering `peer_pair` after a third node
    joined, which is the one direction a stale cache can err in that *removes a
    check*. The guard installs registry publications under a running bridge, so
    this is a reachable state rather than a hypothetical one.
    """
    bridge = _bridge(ctx)
    regime = bridge._share_regime()
    assert bridge._mesh_count() == 2, "ctx ships one authorization plus this node"
    assert regime.mode_for() == "peer_pair"

    # A second authorization lands on disk while the bridge is still running.
    _registry_with_departments(ctx, self_caps=["telemetry"], self_dept=None, other_dept=None)
    assert bridge._mesh_count() == 3
    assert regime.mode_for() == "master_governed", (
        "the same regime object must see the third node; a cached count would still say peer_pair"
    )


def test_a_department_attested_after_startup_is_seen(ctx: dict) -> None:
    """Attestation is read live too, so granting a department needs no restart."""
    bridge = _bridge(ctx)
    own_id = str(bridge.node_identity()["node_id"])
    assert bridge._department_of(own_id) is None, "the ctx registry attests no department"

    _registry_with_departments(ctx, self_caps=["telemetry"], self_dept="research", other_dept="research")
    assert bridge._department_of(own_id) == "research"
    assert bridge._department_of("node-b") == "research"


def test_an_unreadable_registry_still_propagates(ctx: dict) -> None:
    """The freshness fix changes *when* the file is read, not what a failure does."""
    bridge = _bridge(ctx)
    bridge.registry()
    ctx["vault"].joinpath("manifests", "node-authorizations.json").unlink()
    with pytest.raises(OSError):
        bridge.registry()
