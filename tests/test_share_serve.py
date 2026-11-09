"""The 4201/4202 share listeners.

`services/sparknet-share-serve.py` is deliberately thin: the lifecycle rules
stay in `sparknet-share-regime.py`. These tests drive two service instances as
two nodes and move the offer/accept/close messages over a real signed HTTP
listener, so the thing being proven is the wire and the durable state, not a
second copy of the state machine.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sparknet_loader import load_by_path

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "services" / "sparknet-share-serve.py"
CONTRACTS = ROOT.parent / "contracts"
NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
FAST = {"invite_acceptance": 30, "idle_close": 30, "ratification": 30, "share_absolute": 120}


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    return load_by_path(name, path)


@pytest.fixture(scope="module")
def share() -> Any:
    return _load("sparknet_share_serve_test", SERVICE)


@pytest.fixture(scope="module")
def http() -> Any:
    return _load("sparknet_http_share_serve_test", ROOT.parent / "shared" / "sparknet-http.py")


@pytest.fixture(scope="module")
def authz(http: Any) -> Any:
    return http._load_authz()


@pytest.fixture
def keypair(authz: Any):
    _, private = authz.generate_keypair()
    key = authz.private_key_from_b64(private)
    return key, key.public_key()


def _vault(tmp_path: Path, node_id: str, counterpart: str | None = None) -> Path:
    vault = tmp_path / node_id
    manifests = vault / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "node-identity.json").write_text(json.dumps({"node_id": node_id, "role": "peer"}))
    entries = []
    if counterpart:
        entries.append(
            {
                "fingerprint": "00" * 32,
                "identity": {"node_id": counterpart, "role": "peer"},
                "capabilities": [],
                "department": None,
                "revoked": False,
            }
        )
    (manifests / "node-authorizations.json").write_text(json.dumps({"authorizations": entries}))
    return vault


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Wire:
    """Start one ShareService's listener on request; close it deterministically."""

    def __init__(
        self,
        share: Any,
        service: Any,
        public_key: Any | None = None,
        *,
        public_keys: dict[str, Any] | None = None,
        ports: int = 1,
    ):
        self.http = None
        self.service = service
        self.ports = [_free_port() for _ in range(ports)]
        self.servers = share.bind_servers(
            service,
            bind="127.0.0.1",
            ports=tuple(self.ports),
            public_key=public_key,
            public_keys=public_keys,
        )
        self.threads = [
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
            for server in self.servers
        ]
        for thread in self.threads:
            thread.start()

    def url(self, index: int = 0) -> str:
        return f"http://127.0.0.1:{self.ports[index]}"

    def close(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=3)


@pytest.fixture
def pair(share: Any, http: Any, tmp_path: Path):
    a = share.ShareService(
        _vault(tmp_path, "alice", "bob"),
        state_path=tmp_path / "a.json",
        node_id="alice",
        now=lambda: NOW,
        durations=FAST,
        contracts_dir=CONTRACTS,
    )
    b = share.ShareService(
        _vault(tmp_path, "bob", "alice"),
        state_path=tmp_path / "b.json",
        node_id="bob",
        now=lambda: NOW,
        durations=FAST,
        contracts_dir=CONTRACTS,
    )
    return a, b


def _post(http: Any, url: str, key: Any, payload: dict, path: str = "/v1/share/message"):
    return http.post_json(url, "POST", path, payload, key)


def test_signed_offer_and_accept_travel_over_4201_and_4202(share: Any, http: Any, pair: Any):
    a, b = pair
    authz = http._load_authz()
    _, a_priv_b64 = authz.generate_keypair()
    _, b_priv_b64 = authz.generate_keypair()
    a_key = authz.private_key_from_b64(a_priv_b64)
    b_key = authz.private_key_from_b64(b_priv_b64)

    b_wire = Wire(share, b, a_key.public_key(), ports=2)
    a_wire = Wire(share, a, b_key.public_key(), ports=2)
    try:
        offer = a.offer("kb-view", toward="bob")
        status, body = _post(http, b_wire.url(0), a_key, {"message": offer})
        assert status == 200, body
        assert body["ok"] is True
        assert b.get(offer["share_id"])["state"] == "invite"

        accept = b.accept(offer["share_id"], "kb-view")
        status, body = _post(http, a_wire.url(1), b_key, {"message": accept})
        assert status == 200, body
        assert a.get(offer["share_id"])["state"] == "open"
        assert any(item["action"] == "stanza-up" for item in body["actions"])

        close = a.close(offer["share_id"])
        status, body = _post(http, b_wire.url(1), a_key, {"message": close})
        assert status == 200, body
        assert b.get(offer["share_id"])["state"] == "closed"
    finally:
        a_wire.close()
        b_wire.close()


def test_share_state_survives_a_restart(share: Any, http: Any, pair: Any, tmp_path: Path):
    a, b = pair
    authz = http._load_authz()
    _, a_priv_b64 = authz.generate_keypair()
    a_key = authz.private_key_from_b64(a_priv_b64)
    b_wire = Wire(share, b, a_key.public_key())
    try:
        offer = a.offer("session-handoff", toward="bob")
        status, _ = _post(http, b_wire.url(), a_key, {"message": offer})
        assert status == 200
    finally:
        b_wire.close()

    restarted = share.ShareService(
        _vault(tmp_path, "bob", "alice"),
        state_path=tmp_path / "b.json",
        node_id="bob",
        now=lambda: NOW,
        durations=FAST,
        contracts_dir=CONTRACTS,
    )
    assert restarted.get(offer["share_id"])["state"] == "invite"


def test_listener_refuses_unsigned_and_wrong_key(share: Any, http: Any, pair: Any):
    a, b = pair
    authz = http._load_authz()
    _, a_priv_b64 = authz.generate_keypair()
    a_key = authz.private_key_from_b64(a_priv_b64)
    _, wrong_b64 = authz.generate_keypair()
    wrong_key = authz.private_key_from_b64(wrong_b64)
    offer = a.offer("kb-view", toward="bob")
    b_wire = Wire(share, b, a_key.public_key())
    try:
        body = json.dumps({"message": offer}).encode()
        request = urllib.request.Request(
            b_wire.url() + "/v1/share/message", data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=3)
        assert exc.value.code == 401

        status, payload = _post(http, b_wire.url(), wrong_key, {"message": offer})
        assert status == 401
        assert payload["reason"] == "bad-signature"
    finally:
        b_wire.close()


def test_listener_returns_a_stable_refusal(share: Any, http: Any, pair: Any):
    a, _b = pair
    authz = http._load_authz()
    _, a_priv_b64 = authz.generate_keypair()
    a_key = authz.private_key_from_b64(a_priv_b64)
    b_wire = Wire(share, _b, a_key.public_key())
    try:
        status, body = _post(http, b_wire.url(), a_key, {"message": {"msg_type": "share.unknown", "share_id": "nope"}})
        assert status == 400
        assert body["reason"] == "unknown-message"
    finally:
        b_wire.close()


def test_corrupt_state_refuses_startup(share: Any, tmp_path: Path):
    vault = _vault(tmp_path, "carol", "dave")
    state = tmp_path / "broken.json"
    state.write_text("{not-json")
    with pytest.raises(share.ShareServeError) as exc:
        share.ShareService(vault, state_path=state, node_id="carol")
    assert exc.value.reason == "state-corrupt"


def test_a_master_governed_open_needs_a_signed_ratification(share: Any, http: Any, tmp_path: Path):
    authz = http._load_authz()
    master = authz.private_key_from_b64(authz.generate_keypair()[1])
    _, a_priv_b64 = authz.generate_keypair()
    _, b_priv_b64 = authz.generate_keypair()
    a_key = authz.private_key_from_b64(a_priv_b64)
    b_key = authz.private_key_from_b64(b_priv_b64)

    def three(node_id: str, others: tuple[str, ...]) -> Path:
        vault = _vault(tmp_path, node_id)
        registry = {
            "authorizations": [
                {"fingerprint": f"{index:02d}" * 32, "identity": {"node_id": other}, "capabilities": []}
                for index, other in enumerate(others, start=1)
            ]
        }
        (vault / "manifests" / "node-authorizations.json").write_text(json.dumps(registry))
        return vault

    a = share.ShareService(
        three("alice", ("bob", "carol")),
        state_path=tmp_path / "a3.json",
        node_id="alice",
        now=lambda: NOW,
        durations=FAST,
        contracts_dir=CONTRACTS,
        master_public_key=master.public_key(),
    )
    b = share.ShareService(
        three("bob", ("alice", "carol")),
        state_path=tmp_path / "b3.json",
        node_id="bob",
        now=lambda: NOW,
        durations=FAST,
        contracts_dir=CONTRACTS,
        master_public_key=master.public_key(),
    )
    a_wire = Wire(share, a, b_key.public_key())
    b_wire = Wire(share, b, a_key.public_key())
    try:
        offer = a.offer("kb-share", toward="bob")
        assert _post(http, b_wire.url(), a_key, {"message": offer})[0] == 200
        accept = b.accept(offer["share_id"], "kb-share")
        assert _post(http, a_wire.url(), b_key, {"message": accept})[0] == 200
        assert a.get(offer["share_id"])["endpoints"]["bob"]["trust"] == "flagged"

        unsigned = {
            "msg_type": "share.ratify",
            "share_id": offer["share_id"],
            "decision": True,
            "by": "carol",
            "at": NOW.isoformat(),
        }
        status, body = _post(http, a_wire.url(), b_key, {"message": unsigned})
        assert status == 403 and body["reason"] == "unsigned-ratification"

        envelope = {
            "schema_version": authz.SHARE_RATIFICATION_SCHEMA,
            "share_id": offer["share_id"],
            "node_id": "bob",
            "node_fingerprint": "00" * 32,
            "grant_id": "grant-1",
            "decision": "ratified",
            "reason": "approved",
            "accepted_at": NOW.isoformat(),
            "deadline": NOW.isoformat(),
            "ratified_at": NOW.isoformat(),
            "flag_retained": True,
        }
        signed = authz.sign_share_ratification(envelope, master)
        ratify = {**unsigned, "ratification": signed}
        status, body = _post(http, a_wire.url(), b_key, {"message": ratify})
        assert status == 200, body
        assert a.get(offer["share_id"])["endpoints"]["alice"]["trust"] == "authenticated"
    finally:
        a_wire.close()
        b_wire.close()


def test_a_kb_share_payload_lands_in_the_review_inbox(share: Any, http: Any, pair: Any):
    a, b = pair
    authz = http._load_authz()
    _, a_priv_b64 = authz.generate_keypair()
    _, b_priv_b64 = authz.generate_keypair()
    a_key = authz.private_key_from_b64(a_priv_b64)
    b_key = authz.private_key_from_b64(b_priv_b64)
    a_wire = Wire(share, a, b_key.public_key())
    b_wire = Wire(share, b, a_key.public_key())
    try:
        offer = a.offer("kb-share", toward="bob")
        assert _post(http, b_wire.url(), a_key, {"message": offer})[0] == 200
        accept = b.accept(offer["share_id"], "kb-share")
        assert _post(http, a_wire.url(), b_key, {"message": accept})[0] == 200

        status, body = _post(
            http,
            b_wire.url(),
            a_key,
            {"share_id": offer["share_id"], "files": {"notes/shared.md": "# shared\n"}},
            path="/v1/share/payload",
        )
        assert status == 200, body
        assert body["additions"] == ["notes/shared.md"]
        staged = Path(b.vault_root) / "01_Inbox" / f"share-{offer['share_id']}" / "notes" / "shared.md"
        assert staged.read_text() == "# shared\n"

        status, body = _post(
            http,
            b_wire.url(),
            a_key,
            {"share_id": offer["share_id"], "files": {"../escape.md": "no"}},
            path="/v1/share/payload",
        )
        assert status == 400
        assert body["reason"] == "unsafe-path"
    finally:
        a_wire.close()
        b_wire.close()


def test_a_key_ring_binds_the_actor_to_the_signing_key(share: Any, http: Any, pair: Any):
    a, b = pair
    authz = http._load_authz()
    _, a_priv_b64 = authz.generate_keypair()
    a_key = authz.private_key_from_b64(a_priv_b64)
    offer = a.offer("kb-view", toward="bob")
    offer["offered_by"] = "mallory"
    b_wire = Wire(share, b, public_keys={"alice": a_key.public_key()})
    try:
        status, body = _post(http, b_wire.url(), a_key, {"message": offer})
        assert status == 403
        assert body["reason"] == "key-mismatch"
    finally:
        b_wire.close()


def test_the_control_socket_creates_and_reports_a_share(share: Any, pair: Any, tmp_path: Path):
    a, _b = pair
    control_path = tmp_path / "control.sock"
    control = share.ShareControl(a, control_path)
    stopping = threading.Event()
    thread = threading.Thread(target=control.serve_forever, args=(stopping,), daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if control_path.exists():
                break
            time.sleep(0.01)

        def call(payload: dict) -> dict:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(control_path))
                client.sendall(json.dumps(payload).encode() + b"\n")
                return json.loads(client.recv(65536).decode().split("\n", 1)[0])

        response = call({"action": "offer", "kind": "kb-view", "toward": "bob"})
        assert response["ok"] is True
        assert response["share"]["state"] == "invite"
        state = call({"action": "state"})
        assert state["shares"][0]["share_id"] == response["message"]["share_id"]
        closed = call({"action": "close", "share_id": response["message"]["share_id"]})
        assert closed["ok"] is True
    finally:
        stopping.set()
        thread.join(timeout=3)


def test_delivery_rejects_a_share_id_that_escapes_the_inbox(pair: Any):
    _a, b = pair
    with pytest.raises(Exception) as exc:
        b.deliver("../../manifests", {"x.md": "no"})
    assert getattr(exc.value, "reason", "") == "bad-share-id"
    assert not (Path(b.vault_root) / "x.md").exists()
