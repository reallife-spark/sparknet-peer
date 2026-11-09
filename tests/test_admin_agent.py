"""Node admin agent: auth-event ingest, webhook registration, pending supervision.

The agent (`services/sparknet-admin-agent.py`, peer PLAN Step 12) gives each node
a local, honest record of Open WebUI auth activity and makes signup
approval-mediated: `DEFAULT_USER_ROLE=pending` in the ai-stack compose, with the
auth-event webhooks recorded to a 0600 spool the Master's future console reads.
These tests pin down the parts a file can prove:

* the **allow-list** is exactly the auth/user events, so the spool never records
  a failure (auth.login fires on success) and never records noise;
* the **normalised record** is stable and carries the role transition a pending
  approval has to be decided from;
* the **spool** is append-only JSONL, 0600, in the node state dir;
* the **listener** accepts webhook POSTs and acknowledges without noise;
* **webhook registration** is idempotent against the node's own Open WebUI
  admin API (create when absent, update when the URL already exists);
* the **snapshot** preserves `pending` so unapproved signups stay visible;
* `--help` answers without doing anything (fabric-wide rule).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from sparknet_loader import load_by_path

VAULT_ROOT = Path(__file__).resolve().parent.parent
AGENT = VAULT_ROOT / "services" / "sparknet-admin-agent.py"


def _run(argv: list[str], cwd: Path | None = None, env: dict[str, str] | None = None):
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, str(AGENT), *argv],
        cwd=str(cwd) if cwd else None,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def import_source():

    return load_by_path("sparknet_admin_agent", str(AGENT))


def test_help_exits_zero_and_does_nothing(tmp_path: Path) -> None:
    proc = _run(["--help"])
    assert proc.returncode == 0
    assert "sparknet-admin-agent.py" in proc.stdout
    assert proc.stderr == ""


def test_allowlist_is_exactly_the_auth_user_events(tmp_path: Path) -> None:
    mod = import_source()
    assert mod.EVENTS_ALLOWLIST == {
        "auth.signup",
        "auth.login",
        "auth.logout",
        "auth.password_changed",
        "user.role_updated",
    }


def test_normalize_event_maps_known_names_and_rejects_everything_else(tmp_path: Path) -> None:
    mod = import_source()
    rec = mod.normalize_event({"event": "auth.login", "actor": {"email": "u@n"}})
    assert rec is not None
    assert rec["event"] == "auth.login"
    assert rec["actor"] == "u@n"
    assert rec["node"]

    assert mod.normalize_event({"event": "auth.signup"}) is not None
    assert mod.normalize_event({"event": "chat.finished"}) is None
    assert mod.normalize_event({"foo": "bar"}) is None
    assert mod.normalize_event("not a dict") is None


def test_normalize_event_carries_the_role_transition(tmp_path: Path) -> None:
    mod = import_source()
    rec = mod.normalize_event(
        {
            "name": "user.role_updated",
            "data": {"id": "user-9", "role": "pending", "new_role": "user"},
        }
    )
    assert rec is not None
    assert rec["event"] == "user.role_updated"
    assert rec["subject_id"] == "user-9"
    assert rec["role"] == "pending"
    assert rec["new_role"] == "user"


def test_spool_is_jsonl_0600_under_the_state_dir(tmp_path: Path) -> None:
    mod = import_source()
    state = tmp_path / "state"
    spool = mod.spool_path(state)
    assert spool == state / "admin-events" / "events.jsonl"
    rec = mod.normalize_event({"name": "auth.login"})
    assert rec is not None
    mod.append_event(spool, rec)
    lines = spool.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "auth.login"
    assert os.stat(spool).st_mode & 0o077 == 0
    mod.append_event(spool, {"event": "auth.signup", "ts": "now"})
    assert len(mod.read_spool(spool)) == 2


def test_listener_roundtrip(tmp_path: Path) -> None:
    mod = import_source()
    spool = tmp_path / "state" / "spool.jsonl"
    listener = mod.build_listener(spool, "127.0.0.1", 0)
    listener.start()
    port = listener.servers[0].server_address[1]
    try:

        def post(payload: dict):
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/events",
                data=data,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status

        assert post({"name": "auth.login", "actor": {"email": "a@b"}}) == 204
        assert post({"name": "chat.finished"}) == 202, "an out-of-list event is acknowledged, not retried"
        assert post({"name": "auth.signup"}) == 204
        req = urllib.request.Request(f"http://127.0.0.1:{port}/events", data=b"not-json", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=10)
        assert exc_info.value.code == 400, "a malformed body is a real error, not silently ignored"
        rows = mod.read_spool(spool)
        assert [r["event"] for r in rows] == ["auth.login", "auth.signup"]
    finally:
        listener.stop()


def test_webhook_registration_is_idempotent(tmp_path: Path) -> None:
    mod = import_source()
    calls: list[tuple[str, str]] = []

    def fake_http(url, method, token, body=None):
        calls.append((method, url))
        if method == "GET":
            return 200, {"webhooks": []}
        if method == "POST":
            assert url.endswith("/api/events/webhooks")
            return 200, {"id": "w1"}
        raise AssertionError(method)

    orig = mod._http_json
    mod._http_json = fake_http
    try:
        action, wid = mod.ensure_auth_webhooks("http://x", "tok", "http://127.0.0.1:4130/events")
    finally:
        mod._http_json = orig
    assert action == "created" and wid == "w1"
    assert ("POST", "http://x/api/events/webhooks") in calls


def test_webhook_registration_updates_an_existing_url(tmp_path: Path) -> None:
    mod = import_source()
    calls: list[tuple[str, str]] = []

    def fake_http(url, method, token, body=None):
        calls.append((method, url))
        if method == "GET":
            return 200, {"webhooks": [{"id": "w9", "url": "http://127.0.0.1:4130/events"}]}
        if method == "PUT":
            assert url.endswith("/api/events/webhooks/w9")
            assert body and "events" in body
            return 200, {"id": "w9"}
        raise AssertionError(method)

    orig = mod._http_json
    mod._http_json = fake_http
    try:
        action, wid = mod.ensure_auth_webhooks("http://x", "tok", "http://127.0.0.1:4130/events")
    finally:
        mod._http_json = orig
    assert action == "updated" and wid == "w9"


def test_snapshot_preserves_pending_across_roles(tmp_path: Path) -> None:
    mod = import_source()
    state = tmp_path / "state"

    def fake_fetch(base, token):
        return [{"id": "u1", "role": "pending"}, {"id": "u2", "role": "user"}, {"id": "u3", "role": "admin"}]

    orig = mod.fetch_users
    mod.fetch_users = fake_fetch
    try:
        out = mod.snapshot_users("http://x", "tok", state)
    finally:
        mod.fetch_users = orig
    data = json.loads(out.read_text())
    assert data["tally"] == {"total": 3, "pending": 1, "user": 1, "admin": 1}
    assert os.stat(out).st_mode & 0o077 == 0


def test_service_imports_only_stdlib(tmp_path: Path) -> None:
    """The node runs Python 3.8.10; a new dependency would fail at deploy."""
    import ast

    tree = ast.parse(AGENT.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    local = {"sparknet_loader"}  # the one by-path loader, a fabric module
    offenders = sorted(name for name in found if name not in sys.stdlib_module_names and name not in local)
    assert not offenders, f"admin agent imports non-stdlib modules: {offenders}"
