"""The node's reflex surface: loopback-only, grant-checked, local by default.

`/v1/reflex/*` rides the same plain personality as `/v1/models` and `/vault/*`:
loopback-only, and refused without a grant. Local mode is the default and never
opens a socket; broker mode is opt-in and can only reach the Master's 4224
endpoint. In both cases doubt abstains and the route has no authority.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

VAULT_ROOT = Path(__file__).resolve().parent.parent
SHARED_DIR = VAULT_ROOT.parent / "shared"
BRIDGE_PATH = VAULT_ROOT / "services" / "sparknet-dispatch-bridge.py"
OPENAPI = VAULT_ROOT / "manifests" / "vault-openapi.json"
PORTS = VAULT_ROOT.parent / "contracts" / "ports.json"


@pytest.fixture(scope="module")
def bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sparknet_dispatch_bridge_reflex", BRIDGE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class Stub:
    def __init__(self, bridge: ModuleType) -> None:
        self.reflex = bridge._load_shared("sparknet-reflex", "sparknet_reflex_surface_test", ("sparknet-reflex.py",))
        self._bridge_reflex_decide = bridge.Bridge.reflex_decide
        self._bridge_reflex_mode = bridge.Bridge.reflex_mode

    def reflex_decide(self, body: dict, grant: dict) -> dict:
        return self._bridge_reflex_decide(self, body, grant)

    def reflex_mode(self) -> str:
        return self._bridge_reflex_mode(self)

    def node_identity(self) -> dict:
        return {"node_id": "peer-0001"}


def _request(model: str = "local") -> dict:
    return {
        "schema_version": "1.0",
        "question_class": "rag.relevance",
        "state": "alpha",
        "questions": {"q": {"type": "noul", "instructions": "relevant?"}},
        "model": model,
        "policy_version": "policy-1",
        "node_id": "spoofed",
        "request_id": "req-1",
    }


def test_reflex_is_a_plain_loopback_only_prefix(bridge: ModuleType) -> None:
    assert "/v1/reflex/" in bridge.PLAIN_PREFIXES
    assert "/v1/reflex/" in bridge.LOCAL_ONLY_PREFIXES
    # Not an admin route, so the plain handler's Bearer check applies: no grant,
    # no reflex. (The `/admin/*` exception is the only one.)
    assert not "/v1/reflex/decide".startswith(bridge.ADMIN_PREFIX)
    http = bridge._load_http()
    assert http.check_source_policy("/v1/reflex/decide", "overlay", bridge.LOCAL_ONLY_PREFIXES) == "not-loopback"
    assert http.check_source_policy("/v1/reflex/decide", "loopback", bridge.LOCAL_ONLY_PREFIXES) is None


def test_the_route_is_wired_and_documented(bridge: ModuleType) -> None:
    source = BRIDGE_PATH.read_text()
    assert 'path == "/v1/reflex/decide"' in source
    spec = json.loads(OPENAPI.read_text())
    assert "/v1/reflex/decide" in spec["paths"]


def test_no_new_published_port_is_added() -> None:
    ports = json.loads(PORTS.read_text())
    assert "4224" in ports["ports"], "the broker port is a fabric port"
    assert "4224" not in ports.get("node_ports", {}), "reflex must never be host-published"
    assert ports["ports"]["4224"]["bound_by"] == "master"


def test_local_is_the_default_and_never_leaves_the_node(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.delenv("SPARKNET_REFLEX_MODE", raising=False)
    monkeypatch.delenv("SPARKNET_REFLEX_BACKEND", raising=False)
    stub = Stub(bridge)
    assert stub.reflex_mode() == "local"
    decision = stub.reflex_decide(_request(), {})
    assert decision["verdict"] in ("accept", "reject", "abstain")


def test_off_mode_abstains(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("SPARKNET_REFLEX_MODE", "off")
    assert Stub(bridge).reflex_decide(_request(), {})["_reason"] == "off"


def test_broker_mode_forwards_and_binds_node_id(bridge: ModuleType, monkeypatch) -> None:
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib name
            length = int(self.headers.get("Content-Length") or 0)
            seen.append(json.loads(self.rfile.read(length)))
            body = json.dumps(
                {
                    "verdict": "accept",
                    "confidence": 0.9,
                    "backend": "broker",
                    "model": "local",
                    "usage": None,
                    "latency_ms": 1.0,
                    "cached": False,
                    "state_digest": "x",
                    "model_drift": False,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("SPARKNET_REFLEX_MODE", "broker")
        monkeypatch.setenv("SPARKNET_REFLEX_BACKEND", "broker")
        monkeypatch.setenv("SPARKNET_REFLEX_BROKER_URL", f"http://127.0.0.1:{server.server_address[1]}")
        decision = Stub(bridge).reflex_decide(_request(), {})
        assert decision["verdict"] == "accept"
        assert seen[0]["node_id"] == "peer-0001", "the caller's node_id must not be spoofable"
    finally:
        server.shutdown()
        server.server_close()


def test_broker_failure_abstains(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("SPARKNET_REFLEX_MODE", "broker")
    monkeypatch.setenv("SPARKNET_REFLEX_BACKEND", "broker")
    monkeypatch.setenv("SPARKNET_REFLEX_BROKER_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("SPARKNET_REFLEX_TIMEOUT", "0.05")
    reason = Stub(bridge).reflex_decide(_request(), {})["_reason"]
    assert reason in ("broker-error", "local-error")


def test_authority_fields_are_refused_at_the_route(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("SPARKNET_REFLEX_MODE", "local")
    monkeypatch.delenv("SPARKNET_REFLEX_BACKEND", raising=False)
    body = _request()
    body["capability"] = "guard-apply"
    with pytest.raises(Stub(bridge).reflex.ReflexPolicyError, match="forbidden field"):
        Stub(bridge).reflex_decide(body, {})


def test_local_mode_uses_the_local_llm_backend(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("SPARKNET_REFLEX_MODE", "local")
    monkeypatch.setenv("SPARKNET_REFLEX_BACKEND", "local-llm")
    stub = Stub(bridge)
    monkeypatch.setattr(
        stub.reflex,
        "local_llm_backend",
        lambda model=None, timeout_s=5.0: lambda _request: {"type": "noul", "noul": 0.95},
    )
    body = _request(model="local")
    body["question_class"] = "intent.route"
    decision = stub.reflex_decide(body, {})
    assert decision["verdict"] == "accept"
    assert decision["backend"] == "local-llm"


def test_local_mode_never_calls_the_broker(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("SPARKNET_REFLEX_MODE", "local")
    monkeypatch.setenv("SPARKNET_REFLEX_BACKEND", "local")
    monkeypatch.delenv("SPARKNET_REFLEX_BROKER_URL", raising=False)

    def explode(*_args, **_kwargs):
        raise AssertionError("local mode must not open a broker socket")

    monkeypatch.setattr(bridge.urllib.request, "urlopen", explode)
    decision = Stub(bridge).reflex_decide(_request(), {})
    assert decision["verdict"] in ("accept", "reject", "abstain")


def _relevance(state: object, model: str = "local") -> dict:
    body = _request(model=model)
    body["state"] = state
    return body


def test_local_rerank_accepts_query_coverage(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("SPARKNET_REFLEX_MODE", "local")
    monkeypatch.delenv("SPARKNET_REFLEX_BACKEND", raising=False)
    decision = Stub(bridge).reflex_decide(
        _relevance({"query": "guard nonce", "chunk": "the guard envelope nonce expired"}), {}
    )
    assert decision["verdict"] == "accept"
    assert decision["backend"] == "local-rerank"


def test_local_rerank_abstains_on_silence_and_on_a_malformed_state(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("SPARKNET_REFLEX_MODE", "local")
    monkeypatch.delenv("SPARKNET_REFLEX_BACKEND", raising=False)

    silent = Stub(bridge).reflex_decide(_relevance({"query": "guard nonce", "chunk": "a recipe for soup"}), {})
    assert silent["verdict"] == "abstain"
    assert silent["_reason"] == "low-signal", "no coverage is no signal, never a confident reject"

    malformed = Stub(bridge).reflex_decide(_relevance("alpha"), {})
    assert malformed["verdict"] == "abstain"
    assert malformed["_reason"] == "low-signal"


def test_local_mode_refuses_the_broker_backend_without_opening_a_socket(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("SPARKNET_REFLEX_MODE", "local")
    monkeypatch.setenv("SPARKNET_REFLEX_BACKEND", "broker")

    def explode(*_args, **_kwargs):
        raise AssertionError("local mode must not open a socket, whatever the backend env says")

    monkeypatch.setattr(bridge.urllib.request, "urlopen", explode)
    decision = Stub(bridge).reflex_decide(_request(), {})
    assert decision["verdict"] == "abstain"
    assert decision["_reason"] == "backend-misconfigured"


def test_an_off_backend_abstains_in_local_mode(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("SPARKNET_REFLEX_MODE", "local")
    monkeypatch.setenv("SPARKNET_REFLEX_BACKEND", "off")
    decision = Stub(bridge).reflex_decide(
        _relevance({"query": "guard nonce", "chunk": "the guard envelope nonce expired"}), {}
    )
    assert decision["verdict"] == "abstain"
    assert decision["_reason"] == "off"


def test_the_local_llm_timeout_falls_back_on_a_bad_value(bridge: ModuleType, monkeypatch) -> None:
    monkeypatch.setenv("SPARKNET_REFLEX_LOCAL_TIMEOUT", "not-a-number")
    assert bridge._reflex_local_timeout() == 10.0
    monkeypatch.setenv("SPARKNET_REFLEX_LOCAL_TIMEOUT", "-1")
    assert bridge._reflex_local_timeout() == 10.0
    monkeypatch.setenv("SPARKNET_REFLEX_LOCAL_TIMEOUT", "3.5")
    assert bridge._reflex_local_timeout() == 3.5
