"""Step 6 — intra-node conversation passing, exercised against a real server.

peer PLAN Step 6 hands a conversation between the node's own agents using only
native opencode/OpenAI verbs; the bridge never invents a passing mechanism:

* DoD 1 — ``POST /session/:id/message`` accepts an ``agent``: the session
  survives, every earlier turn (and its handling agent) stays, and only the
  routing agent changes;
* DoD 2 — ``POST /session/:id/fork`` branches at a message index into a child
  session carrying the shared prefix; the original stays whole;
* DoD 3 — ``GET /session/:id/children`` lists the branch and the share verbs
  (``POST/GET/DELETE /session/:id/share[/:id]``) draft a share through the
  Step 8 regime engine. On this solo node a created share is an *invite* (a
  draft with a 1 h window); opening one needs a second node to accept it, and
  the full clocked lifecycle is proven in ``test_share_regime.py``;
* DoD 4 — the orchestrator's delegation and hand-off verbs run against a live
  loopback bridge (``tb.Server`` spins a real ``ThreadingHTTPServer``), not an
  asserted stub.

History surviving a hand-off is the point: the ``:id`` routes are grant-owned
(the session is loaded and checked, so a fork's sibling sessions are reachable
by the same grant and by nobody else).
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


authz = _load("sparknet-authz", "sparknet_authz_conv")
prov = _load("sparknet-provenance", "sparknet_provenance_conv")


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
        grant_id="GRANT-CONV-1",
        user_identity="U-20161",
        conversation_key="02020202-0202-0202-0202-020202020202-3",
        capabilities=["p2p-sync"],
        vault_scope=["05_Knowledge"],
        ttl_minutes=480,
        nonce="conv-nonce-0001",
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


def _start(server, ctx, model: str = "chat"):
    grant = _grant(ctx)
    status, payload = server.request(
        "POST",
        "/v1/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": f"start on {model}"}]},
        token=_bearer(grant),
    )
    assert status == 200
    return grant


def _message(server, token, session_id, agent, content):
    return server.request(
        "POST",
        f"/session/{session_id}/message",
        {"agent": agent, "role": "user", "content": content},
        token=token,
    )


# --- DoD 1: message keeps the session and changes the agent --------------------


def test_message_hands_off_keeping_history(server, ctx):
    grant = _start(server, ctx)
    token = _bearer(grant)
    sid = grant["grant_id"]

    status, payload = _message(server, token, sid, "plan", "plan the change")
    assert status == 200 and payload["ok"] is True
    assert payload["turn"]["agent"] == "plan"

    status, payload = _message(server, token, sid, "vault", "capture the note")
    assert status == 200
    status, payload = _message(server, token, sid, "build", "implement it")
    assert status == 200

    status, payload = server.request("GET", f"/session/{sid}", token=token)
    assert status == 200
    session = payload["session"]
    turns = session["conversation"]
    assert [t["content"] for t in turns if t["role"] == "user"] == [
        "start on chat",
        "plan the change",
        "capture the note",
        "implement it",
    ], "history must survive every hand-off intact"
    assert [t.get("agent") for t in turns] == ["chat", "plan", "vault", "build"]
    assert session["agent"] == "build", "the session's current agent is the last hand-off"
    assert session["schema"] == "sparknet-session/2"


def test_message_refuses_an_unknown_agent(server, ctx):
    grant = _start(server, ctx)
    status, payload = _message(server, _bearer(grant), grant["grant_id"], "warp-drive", "hi")
    assert status == 400 and payload["reason"] == "unknown-model"


def test_message_requires_agent_role_and_content(server, ctx):
    grant = _start(server, ctx)
    token = _bearer(grant)
    for body in ({"agent": "plan"}, {"agent": "plan", "role": "user"}, {"agent": "", "role": "user", "content": "x"}):
        status, payload = server.request("POST", f"/session/{grant['grant_id']}/message", body, token=token)
        assert status == 400 and payload["reason"] == "malformed"


def test_message_requires_an_existing_session(server, ctx):
    status, payload = _message(server, _bearer(_grant(ctx)), "GRANT-CONV-NOSESSION", "plan", "hi")
    assert status == 404 and payload["reason"] == "no-session"


def test_message_is_owned_by_the_presenting_grant(server, ctx):
    grant = _start(server, ctx)
    other = _grant(ctx, grant_id="GRANT-CONV-OTHER", nonce="conv-nonce-other")
    status, payload = _message(server, _bearer(other), grant["grant_id"], "plan", "hi")
    assert status == 403 and payload["reason"] == "not-your-session"


# --- DoD 2: fork branches at a message, the original stays whole ---------------


def test_fork_branches_at_a_message_and_keeps_the_original_whole(server, ctx):
    grant = _start(server, ctx)
    token = _bearer(grant)
    sid = grant["grant_id"]
    _message(server, token, sid, "plan", "option A context")
    _message(server, token, sid, "build", "original line")

    status, payload = server.request("POST", f"/session/{sid}/fork", {"at": 1}, token=token)
    assert status == 200
    child = payload["session"]
    assert payload["parent_turns"] == 3
    assert child["session_id"] == f"{sid}-f1"
    assert child["parent_id"] == sid
    assert child["fork_at"] == 1
    assert child["owner_grant_id"] == grant["grant_id"]
    assert [t["content"] for t in child["conversation"]] == [
        "start on chat",
        "option A context",
    ], "the child carries the shared prefix up to and including the fork message"
    assert [t.get("agent") for t in child["conversation"]] == ["chat", "plan"]
    assert child["agent"] == "plan"
    assert child["permissions"] == [], "pending permission cards do not branch"

    status, payload = server.request("GET", f"/session/{sid}", token=token)
    original = payload["session"]
    assert len(original["conversation"]) == 3, "the original stays whole"
    assert [t["content"] for t in original["conversation"]] == [
        "start on chat",
        "option A context",
        "original line",
    ]
    assert original["children"] == [f"{sid}-f1"]

    status, payload = server.request("GET", f"/session/{child['session_id']}", token=token)
    assert status == 200 and payload["session"]["session_id"] == child["session_id"]


def test_a_fork_defaults_to_the_last_message_and_second_forks_get_a_new_sibling(server, ctx):
    grant = _start(server, ctx)
    token = _bearer(grant)
    sid = grant["grant_id"]
    _message(server, token, sid, "build", "one")

    status, payload = server.request("POST", f"/session/{sid}/fork", {}, token=token)
    assert status == 200 and payload["session"]["fork_at"] == 1

    status, payload = server.request("POST", f"/session/{sid}/fork", {"at": 0}, token=token)
    assert status == 200 and payload["session"]["session_id"] == f"{sid}-f2"

    status, payload = server.request("GET", f"/session/{sid}", token=token)
    assert payload["session"]["children"] == [f"{sid}-f1", f"{sid}-f2"]


def test_fork_refuses_an_out_of_range_or_non_int_at(server, ctx):
    grant = _start(server, ctx)
    token = _bearer(grant)
    sid = grant["grant_id"]
    for bad in (99, -1, "1", 1.5):
        status, payload = server.request("POST", f"/session/{sid}/fork", {"at": bad}, token=token)
        assert status == 400 and payload["reason"] == "malformed", f"at={bad!r} must be refused"


# --- DoD 3: children and the share endpoints ----------------------------------


def test_children_lists_the_branch(server, ctx):
    grant = _start(server, ctx)
    token = _bearer(grant)
    sid = grant["grant_id"]
    server.request("POST", f"/session/{sid}/fork", {"at": 0}, token=token)
    server.request("POST", f"/session/{sid}/fork", {"at": 0}, token=token)

    status, payload = server.request("GET", f"/session/{sid}/children", token=token)
    assert status == 200
    assert payload["children"] == [f"{sid}-f1", f"{sid}-f2"]


def test_share_endpoints_declare_and_close_a_session_share(server, ctx):
    grant = _start(server, ctx)
    token = _bearer(grant)
    sid = grant["grant_id"]

    status, payload = server.request("POST", f"/session/{sid}/share", {"kind": "session-view"}, token=token)
    assert status == 200
    share = payload["share"]
    # Step 8: a created share is an *invite* (a 1 h draft), not already open —
    # opening one needs a second node to accept it, which this host lacks.
    assert share["share_id"] == "share-1" and share["kind"] == "session-view" and share["status"] == "invite"
    assert share["accept_deadline"], "every invite carries its clock"

    status, payload = server.request("POST", f"/session/{sid}/share", {"kind": "session-handoff"}, token=token)
    assert status == 200 and payload["share"]["share_id"] == "share-2"

    status, payload = server.request("GET", f"/session/{sid}/shares", token=token)
    assert status == 200 and [s["share_id"] for s in payload["shares"]] == ["share-1", "share-2"]

    status, payload = server.request("DELETE", f"/session/{sid}/share/share-1", token=token)
    assert status == 200 and payload["share"]["status"] == "closed"
    assert payload["share"]["closed_at"]

    status, payload = server.request("DELETE", f"/session/{sid}/share/share-1", token=token)
    assert status == 409 and payload["reason"] == "already-closed"
    status, payload = server.request("DELETE", f"/session/{sid}/share/share-99", token=token)
    assert status == 404 and payload["reason"] == "unknown-share"


def test_share_verbs_now_accept_all_five_step8_kinds(server, ctx):
    """Step 6 refused kb-view/kb-share/full as "Step 8, not Step 6". Step 8 wires
    the five contract share types into the verbs, so every one drafts an invite;
    an unknown kind is still refused."""
    grant = _start(server, ctx)
    token = _bearer(grant)
    sid = grant["grant_id"]
    for kind in ("kb-view", "kb-share", "session-view", "session-handoff", "full"):
        status, payload = server.request("POST", f"/session/{sid}/share", {"kind": kind}, token=token)
        assert status == 200 and payload["share"]["kind"] == kind, f"{kind} is a Step 8 share type"
        assert payload["share"]["status"] == "invite"
    status, payload = server.request("POST", f"/session/{sid}/share", {"kind": "not-a-kind"}, token=token)
    assert status == 400 and payload["reason"] == "malformed"


# --- DoD 4: the orchestrator's delegation and hand-off, on a real server ------


def test_the_orchestrator_hand_off_runs_against_a_real_server(server, ctx):
    """The node-orchestrator delegates, hands off, forks and shares — actually
    over the loopback server, not an asserted stub (DoD 4)."""
    grant = _start(server, ctx)
    token = _bearer(grant)
    sid = grant["grant_id"]

    status, payload = _message(server, token, sid, "node-orchestrator", "delegate this")
    assert status == 200 and payload["turn"]["agent"] == "node-orchestrator"
    _message(server, token, sid, "build", "build it")
    _message(server, token, sid, "vault", "file it")

    status, payload = server.request("POST", f"/session/{sid}/fork", {"at": 1}, token=token)
    assert status == 200
    child_id = payload["session"]["session_id"]

    status, payload = server.request("GET", f"/session/{sid}/children", token=token)
    assert status == 200 and payload["children"] == [child_id]

    status, payload = server.request("POST", f"/session/{sid}/share", {"kind": "session-handoff"}, token=token)
    assert status == 200 and payload["share"]["status"] == "invite"

    child_file = ctx["state"] / "sessions" / f"{child_id}.json"
    parent_file = ctx["state"] / "sessions" / f"{sid}.json"
    assert child_file.is_file() and parent_file.is_file(), "the real server persists the whole branch"
    child = json.loads(child_file.read_text())
    assert child["parent_id"] == sid and child["conversation"][1]["agent"] == "node-orchestrator"


# --- DoD 5-of-the-fabric: the verbs are declared config, not a UI click --------


def test_the_surface_config_declares_the_step6_verbs(server):
    manifest = json.loads((VAULT_ROOT / "manifests" / "ai-stack.json").read_text())
    passing = manifest["conversation_passing"]
    assert "/session/:id/message" in passing["message"]
    assert "/session/:id/fork" in passing["fork"]
    assert "/session/:id/children" in passing["children_and_shares"]
    assert passing["native_primitives"] and "no custom passing mechanism" in passing["native_primitives"]
