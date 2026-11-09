"""The peer can ask the Master for scoped knowledge, and records the request."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sparknet_loader import load_by_path

VAULT_ROOT = Path(__file__).resolve().parent.parent
CLIENT = VAULT_ROOT / "services" / "sparknet-rfi-client.py"


def _load() -> object:
    return load_by_path("sparknet_rfi_client_test", CLIENT)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "peer-node"
    (root / "manifests").mkdir(parents=True)
    (root / "manifests" / "node-identity.json").write_text(
        (VAULT_ROOT / "manifests" / "node-identity.json").read_text(encoding="utf-8")
    )
    return root


def test_a_request_is_built_with_a_scope_and_the_node_fingerprint(vault: Path) -> None:
    module = _load()
    request = module.build_request(vault, question="What is the registry?", scope=["contracts", "contracts"])
    assert request["scope"] == ["contracts"]
    assert len(request["peer_fingerprint"]) == 64
    assert request["request_id"].startswith("RFI-")
    assert request["expires_at"] > request["issued_at"]


def test_a_request_is_posted_and_recorded(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    monkeypatch.setenv("SPARKNET_STATE_DIR", str(tmp_path / "state"))
    seen: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            seen["body"] = json.loads(self.rfile.read(length))
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"queued"}')

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/api/v1/rfi-request"
        result = module.request_knowledge(vault, question="What is a peer?", scope=["docs"], endpoint=endpoint)
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert result["ok"] is True
    assert result["status"] == 202
    assert seen["body"]["question"] == "What is a peer?"
    record = Path(result["record"])
    assert record.is_file()
    assert record.stat().st_mode & 0o777 == 0o600
    assert json.loads(record.read_text())["request"]["scope"] == ["docs"]


def test_the_cli_help_does_nothing() -> None:
    module = _load()
    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])
    assert exc.value.code == 0
