"""The peer issue pusher: local parsing, redaction and the contract payload."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import ModuleType

import pytest
from sparknet_loader import load_by_path

ROLE_ROOT = Path(__file__).resolve().parent.parent
FABRIC_ROOT = ROLE_ROOT.parent
PUSH_PATH = ROLE_ROOT / "services" / "sparknet-issue-push.py"


def _load(name: str, path: Path) -> ModuleType:
    return load_by_path(name, path)


push = _load("sparknet_issue_push_test", PUSH_PATH)


def _write_issue(root: Path, *, body: str = "The console returns 401.", title: str = "Console token") -> Path:
    (root / "manifests").mkdir(parents=True, exist_ok=True)
    (root / "manifests" / "node-identity.json").write_text(
        json.dumps(
            {
                "node_id": "sparknet-node-0002",
                "role": "peer",
                "wireguard_public_key_b64": "x",
                "overlay_ipv4": "10.100.0.2/32",
                "requested_capabilities": ["telemetry", "issue-report"],
            }
        )
    )
    issues = root / ".scratch" / "issues"
    issues.mkdir(parents=True)
    path = issues / "fix-console-token.md"
    body_digest = hashlib.sha256((body + "\n").encode()).hexdigest()
    path.write_text(
        "---\n"
        "schema: sparknet-issue/1\n"
        "id: fix-console-token\n"
        "title: " + title + "\n"
        "status: open\n"
        "labels: [needs-triage]\n"
        "reporter: operator\n"
        "created_at: 2026-11-09T10:00:00Z\n"
        "updated_at: 2026-11-09T10:00:00Z\n"
        "source: .\n"
        f"body_sha256: {body_digest}\n"
        "---\n" + body + "\n",
        encoding="utf-8",
    )
    return path


def test_parses_frontmatter_and_body(tmp_path: Path) -> None:
    path = _write_issue(tmp_path)
    meta, body = push.split_issue(path.read_text())
    assert meta["schema"] == "sparknet-issue/1"
    assert meta["id"] == "fix-console-token"
    assert meta["labels"] == ["needs-triage"]
    assert body.strip() == "The console returns 401."


def test_redaction_refuses_a_secret_shaped_body(tmp_path: Path) -> None:
    path = _write_issue(tmp_path, body="password=supersecret12345")
    meta, body = push.split_issue(path.read_text())
    with pytest.raises(push.Refusal):
        push.redact(meta, body)


def test_build_payload_matches_the_issue_contract(tmp_path: Path) -> None:
    path = _write_issue(tmp_path)
    meta, body = push.split_issue(path.read_text())
    authz = push.load_authz()
    assert authz is not None
    payload = push.build_payload(
        meta,
        body,
        authz=authz,
        node="sparknet-node-0002",
        fingerprint="a" * 64,
    )
    assert tuple(sorted(payload)) == tuple(sorted(authz.ISSUE_REPORT_FIELDS))
    assert payload["schema"] == "sparknet-issue/1"
    assert payload["labels"] == ["needs-triage"]
    assert payload["content_sha256"] == authz.hashlib.sha256(body.encode()).hexdigest()


def test_dry_run_does_not_need_a_key_or_a_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_issue(tmp_path)
    monkeypatch.setattr(push, "ROLE_ROOT", tmp_path)
    monkeypatch.setattr(push, "state_dir", lambda: tmp_path / ".state")
    code = push.push(tmp_path, "http://127.0.0.1:1", dry_run=True, as_json=True)
    assert code == 0
