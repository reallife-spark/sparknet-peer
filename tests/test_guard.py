"""The guard daemon: verification, refusal, and arming.

The daemon is driven over real HTTP on loopback with `--bind-any`, because the
production bind is deliberately restricted to the node's overlay address (see
test_bind_is_restricted_to_the_overlay_address below). Applying a real patch
needs euid 0 plus `chattr`, so those tests assert the *refusal* path here; the
apply path itself is a target-node checklist item in docs/wireguard.md.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

VAULT_ROOT = Path(__file__).resolve().parent.parent
GUARD = VAULT_ROOT / "services" / "sparknet-guard-d.py"
PROTECTED_TARGET = "/etc/wireguard/wg0.conf"


def _load_guard_module():
    spec = importlib.util.spec_from_file_location("sparknet_guard_d", GUARD)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guardmod = _load_guard_module()


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def master_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def authorized_vault(tmp_path: Path, master_key: Ed25519PrivateKey) -> Path:
    """A vault copy whose registry grants this node guard-apply."""
    import shutil

    dest = tmp_path / "vault"
    shutil.copytree(
        VAULT_ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "references", ".opencode"),
    )
    # Publish the test Master's public key into the copy.
    from cryptography.hazmat.primitives import serialization

    raw = master_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    (dest / "manifests" / "keys" / "master-ed25519.pub.b64").write_text(base64.b64encode(raw).decode() + "\n")

    # Sign this node for guard-apply using the real authz implementation.
    sys.path.insert(0, str(VAULT_ROOT / "services"))
    sparknet_authz = guardmod._load_authz_module()
    identity = json.loads((dest / "manifests" / "node-identity.json").read_text())
    entry = sparknet_authz.sign_authorization(identity, master_key, ["telemetry", "p2p-sync", "guard-apply"])
    registry = json.loads((dest / "manifests" / "node-authorizations.json").read_text())
    registry["authorizations"] = [entry]
    registry["master_public_key_b64"] = base64.b64encode(raw).decode()
    (dest / "manifests" / "node-authorizations.json").write_text(json.dumps(registry, indent=2) + "\n")
    return dest


class Harness:
    """A guard instance serving on loopback, for request-level assertions."""

    def __init__(self, vault: Path, *, apply_enabled: bool = False, dry_run: bool = True) -> None:
        self.vault = vault
        master_pubkey = None
        pubkey_file = vault / "manifests" / "keys" / "master-ed25519.pub.b64"
        if pubkey_file.is_file():
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            master_pubkey = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(pubkey_file.read_text().strip(), validate=True)
            )
        self.guard = guardmod.Guard(
            vault,
            apply_enabled=apply_enabled,
            master_pubkey=master_pubkey,
            dry_run=dry_run,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), guardmod._make_handler(self.guard, "test-guard"))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def post(self, packet: dict, path: str = "/api/v1/critical-file/patch") -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(packet).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def get(self, path: str = "/api/v1/status") -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def harness(authorized_vault: Path):
    instance = Harness(authorized_vault)
    yield instance
    instance.stop()


def _packet(
    master_key: Ed25519PrivateKey,
    *,
    file_path: str = PROTECTED_TARGET,
    content: bytes = b"# patched\n",
    ttl_seconds: int = 60,
    nonce: str = "n1",
    request_id: str = "r1",
    issued_delta: timedelta = timedelta(0),
    omit_signature: bool = False,
) -> dict:
    now = datetime.now(timezone.utc) + issued_delta
    import hashlib

    payload = {
        "file_path": file_path,
        "content_b64": base64.b64encode(content).decode(),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "issued_at": (now - timedelta(seconds=ttl_seconds / 2)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(seconds=ttl_seconds / 2)).isoformat().replace("+00:00", "Z"),
        "nonce": nonce,
        "request_id": request_id,
    }
    message = guardmod.approval_message(payload)
    packet = {"payload": base64.b64encode(message).decode()}
    if not omit_signature:
        packet["signature"] = base64.b64encode(master_key.sign(message)).decode()
    return packet


# --- positive path ------------------------------------------------------------


def test_valid_signed_patch_is_verified(harness: Harness, master_key) -> None:
    status, body = harness.post(_packet(master_key))
    assert status == 200, body
    assert body["status"] == "PATCH_VERIFIED_DRY_RUN"
    assert body["file_path"] == PROTECTED_TARGET


def test_status_endpoint_reports_capabilities(harness: Harness) -> None:
    status, body = harness.get()
    assert status == 200
    assert body["protected_paths"] >= 1
    assert "guard-apply" in body["capabilities"]


def test_audit_ledger_records_the_decision(harness: Harness, master_key) -> None:
    harness.post(_packet(master_key))
    ledger = json.loads((harness.vault / "blockchain" / "guard-audit.json").read_text())
    assert ledger["blocks"], "a verified patch must leave an audit block"
    assert ledger["blocks"][-1]["event"] in ("patch_verified_dry_run", "patch_applied")


def test_audit_ledger_is_hash_chained(harness: Harness, master_key) -> None:
    for index in range(3):
        harness.post(_packet(master_key, nonce=f"n{index}", request_id=f"r{index}"))
    ledger = json.loads((harness.vault / "blockchain" / "guard-audit.json").read_text())
    blocks = ledger["blocks"]
    assert len(blocks) >= 3
    for i in range(1, len(blocks)):
        assert blocks[i]["prev_hash"] == blocks[i - 1]["hash"], f"broken chain at {i}"


# --- refusals -----------------------------------------------------------------


def test_missing_signature_is_a_malformed_packet(harness: Harness) -> None:
    """No signature field at all is a protocol error, not an authorization failure."""
    status, body = harness.post(_packet(None, omit_signature=True))
    assert status == 400
    assert body["code"] == "malformed_packet"


def test_garbage_signature_is_a_bad_signature(harness: Harness, master_key) -> None:
    status, body = harness.post(_packet(master_key) | {"signature": "bm90LWEtc2lnbmF0dXJl"})
    assert status == 403
    assert body["code"] == "bad_signature"


def test_signature_from_impostor_is_rejected(harness: Harness, master_key) -> None:
    packet = _packet(master_key)
    impostor = Ed25519PrivateKey.generate()
    message = base64.b64decode(packet["payload"])
    packet["signature"] = base64.b64encode(impostor.sign(message)).decode()
    status, body = harness.post(packet)
    assert status == 403
    assert body["code"] == "bad_signature"


def test_replayed_nonce_request_id_is_rejected(harness: Harness, master_key) -> None:
    first, body = harness.post(_packet(master_key, nonce="dup", request_id="dup"))
    assert first == 200, body
    status, body = harness.post(_packet(master_key, nonce="dup", request_id="dup"))
    assert status == 409
    assert body["code"] == "replayed"


def test_expired_approval_is_rejected(harness: Harness, master_key) -> None:
    packet = _packet(master_key)
    status, body = harness.post(packet)
    assert status == 200
    # Same nonce/request_id would trip replay first, so use a fresh one far in the past.
    packet = _packet(master_key, nonce="old", request_id="old", issued_delta=timedelta(hours=-2))
    status, body = harness.post(packet)
    assert status == 403
    assert body["code"] == "expired"


def test_not_yet_valid_approval_is_rejected(harness: Harness, master_key) -> None:
    packet = _packet(master_key, nonce="future", request_id="future", issued_delta=timedelta(hours=2))
    status, body = harness.post(packet)
    assert status == 403
    assert body["code"] == "not_yet_valid"


def test_wide_approval_window_is_rejected(harness: Harness, master_key) -> None:
    packet = _packet(master_key, nonce="wide", request_id="wide", ttl_seconds=6000)
    status, body = harness.post(packet)
    assert status == 403
    assert body["code"] in ("window_too_wide", "expired")


def test_content_hash_mismatch_is_rejected(harness: Harness, master_key) -> None:
    packet = _packet(master_key, nonce="mm", request_id="mm")
    message = json.loads(base64.b64decode(packet["payload"]))
    message["content_sha256"] = "0" * 64
    raw = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    packet["payload"] = base64.b64encode(raw).decode()
    packet["signature"] = base64.b64encode(master_key.sign(raw)).decode()
    status, body = harness.post(packet)
    assert status == 400
    assert body["code"] == "content_hash_mismatch"


def test_tampered_payload_invalidates_signature(harness: Harness, master_key) -> None:
    """Change the target after signing; the signature must no longer verify."""
    packet = _packet(master_key)
    message = json.loads(base64.b64decode(packet["payload"]))
    message["file_path"] = "/etc/passwd"
    raw = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    packet["payload"] = base64.b64encode(raw).decode()
    status, body = harness.post(packet)
    assert status == 403
    assert body["code"] == "bad_signature"


def test_unregistered_target_is_rejected(harness: Harness, master_key) -> None:
    status, body = harness.post(_packet(master_key, file_path="/etc/passwd", nonce="np", request_id="np"))
    assert status == 403
    assert body["code"] == "not_protected"


def test_unarmed_guard_refuses_to_apply(harness: Harness, master_key) -> None:
    """Default posture: verify and audit, never write."""
    status, body = harness.post(_packet(master_key))
    assert status == 200
    # Apply-enabled is False on this harness, yet the request still resolves to
    # a *dry run* rather than an apply — verify the guard never mutated anything.
    assert harness.guard.dry_run is True


def test_missing_guard_apply_capability_is_rejected(tmp_path: Path, master_key) -> None:
    """A node signed for telemetry only must not be able to patch /etc."""
    import shutil

    dest = tmp_path / "vault-nocap"
    shutil.copytree(
        VAULT_ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "references", ".opencode"),
    )
    from cryptography.hazmat.primitives import serialization

    raw = master_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    (dest / "manifests" / "keys" / "master-ed25519.pub.b64").write_text(base64.b64encode(raw).decode() + "\n")
    sparknet_authz = guardmod._load_authz_module()
    identity = json.loads((dest / "manifests" / "node-identity.json").read_text())
    entry = sparknet_authz.sign_authorization(identity, master_key, ["telemetry"])
    (dest / "manifests" / "node-authorizations.json").write_text(
        json.dumps({"schema_version": "1.0", "authorizations": [entry]}, indent=2) + "\n"
    )

    instance = Harness(dest)
    try:
        status, body = instance.post(_packet(master_key))
        assert status == 403
        assert body["code"] == "capability_missing"
    finally:
        instance.stop()


def test_revoked_node_cannot_apply(tmp_path: Path, master_key) -> None:
    import shutil

    dest = tmp_path / "vault-revoked"
    shutil.copytree(
        VAULT_ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "references", ".opencode"),
    )
    from cryptography.hazmat.primitives import serialization

    raw = master_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    (dest / "manifests" / "keys" / "master-ed25519.pub.b64").write_text(base64.b64encode(raw).decode() + "\n")
    sparknet_authz = guardmod._load_authz_module()
    identity = json.loads((dest / "manifests" / "node-identity.json").read_text())
    entry = sparknet_authz.sign_authorization(identity, master_key, ["guard-apply"])
    entry["revoked"] = True
    entry["signature_b64"] = base64.b64encode(master_key.sign(sparknet_authz.authorization_message(entry))).decode()
    (dest / "manifests" / "node-authorizations.json").write_text(
        json.dumps({"schema_version": "1.0", "authorizations": [entry]}, indent=2) + "\n"
    )

    instance = Harness(dest)
    try:
        status, body = instance.post(_packet(master_key))
        assert status == 403
        assert body["code"] == "capability_missing"
    finally:
        instance.stop()


def test_wrong_path_is_a_404(harness: Harness, master_key) -> None:
    status, _ = harness.post(_packet(master_key), path="/api/v1/nope")
    assert status == 404


def test_garbage_body_is_a_400(harness: Harness) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{harness.port}/api/v1/critical-file/patch",
        data=b"not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 400


# --- binding posture ----------------------------------------------------------


def test_bind_is_restricted_to_the_overlay_address() -> None:
    """Production must not listen on an arbitrary address."""
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--listen", "0.0.0.0", "--port", "19099"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "SPARKNET_VAULT_ROOT": str(VAULT_ROOT)},
    )
    assert proc.returncode != 0
    assert "overlay" in (proc.stdout + proc.stderr).lower()


def test_guard_refuses_to_listen_while_standalone() -> None:
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--port", "19099"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
        env={**os.environ, "SPARKNET_VAULT_ROOT": str(VAULT_ROOT)},
    )
    assert proc.returncode == 3
    assert "standalone" in (proc.stdout + proc.stderr).lower()


def test_apply_enabled_requires_root(tmp_path: Path) -> None:
    """--apply-enabled is refused for a non-root euid, and for root without --bind-any."""
    if os.geteuid() == 0:
        pytest.skip("running as root: the non-root guard cannot be exercised")
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--apply-enabled", "--bind-any", "--port", "19099"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "SPARKNET_VAULT_ROOT": str(VAULT_ROOT)},
    )
    assert proc.returncode == 2
    assert "euid 0" in (proc.stdout + proc.stderr)


# --- CLI modes ----------------------------------------------------------------


def test_check_manifest_passes() -> None:
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--check-manifest"], capture_output=True, text=True, check=False, timeout=30
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_status_is_valid_json() -> None:
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--status"], capture_output=True, text=True, check=False, timeout=30
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["mode"] in ("anchored", "standalone")


def test_enforce_immutability_dry_run_changes_nothing() -> None:
    """--enforce-immutability --dry-run must not actually lock anything."""
    proc = subprocess.run(
        [sys.executable, str(GUARD), "--enforce-immutability", "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- the earlier stub is gone -------------------------------------------------


def test_guard_is_no_longer_a_stub() -> None:
    """The 81-line read-only stub must not creep back."""
    source = GUARD.read_text()
    assert "Template mode: no action taken" not in source
    assert "REFUSAL: apply path not implemented" not in source
    for required in ("approval_message", "ReplayGuard", "apply_patch", "enforce_immutability", "AuditLedger"):
        assert required in source, f"guard lost {required}"


# --- overlay source authorization -------------------------------------------
#
# Regression tests for a real bug: the server binds the *node's* overlay
# address, so a legitimate request arrives with the *Master's* source address.
# The original check compared the client against the node's own address, which
# meant every genuine Master request would have been rejected with 403. The
# suite never caught it because it only ever exercised loopback.


def test_master_anchor_is_a_permitted_source() -> None:
    guard = guardmod.Guard(vault_root=VAULT_ROOT)
    assert guard.master_anchor_address() == "10.100.0.1"
    assert "10.100.0.1" in guard.permitted_sources()


def test_loopback_is_permitted_for_local_tooling() -> None:
    guard = guardmod.Guard(vault_root=VAULT_ROOT)
    permitted = guard.permitted_sources()
    assert "127.0.0.1" in permitted
    assert "::1" in permitted


def test_master_address_is_read_from_security_policies() -> None:
    """Not hardcoded: the topology lives in the policy file."""
    policies = json.loads((VAULT_ROOT / "manifests" / "security-policies.json").read_text())
    guard = guardmod.Guard(vault_root=VAULT_ROOT)
    assert guard.master_anchor_address() == policies["wireguard"]["master_anchor_ip"]


def test_master_address_falls_back_when_policy_is_unreadable(tmp_path: Path) -> None:
    empty = tmp_path / "vault"
    (empty / "manifests").mkdir(parents=True)
    (empty / "manifests" / "security-policies.json").write_text("{ not json")
    guard = guardmod.Guard(vault_root=empty)
    assert guard.master_anchor_address() == "10.100.0.1"


def test_other_overlay_clients_are_refused() -> None:
    """A neighbouring node must not be able to submit patches."""
    guard = guardmod.Guard(vault_root=VAULT_ROOT)
    permitted = guard.permitted_sources()
    for neighbour in ("10.100.0.2", "10.100.0.99", "192.168.1.10", "10.0.0.1"):
        assert neighbour not in permitted, f"{neighbour} must not be a permitted patch source"


def test_the_node_own_overlay_address_is_not_a_permitted_source() -> None:
    """A local process must not be able to spoof a source and get through."""
    guard = guardmod.Guard(vault_root=VAULT_ROOT)
    own = guard.overlay_address()
    if own is None:
        pytest.skip("node-identity.json has no overlay_ipv4 yet")
    assert own not in guard.permitted_sources(), (
        f"the node's own overlay address {own} must not be accepted as a patch source"
    )


def _post_patch(handler_cls, port: int) -> int:
    """POST an empty patch and return the HTTP status."""
    body = json.dumps({"payload": "", "signature": ""}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v1/critical-file/patch",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as exc:
        return exc.code
    return 200


def _serve_with_fake_client(handler_cls, client_ip: str):
    """Serve the guard while pretending every peer is `client_ip`.

    The guard reads `self.client_address[0]`, the real TCP peer, so the test has
    to shadow the attribute rather than override address_string().
    """

    class FakeClientHandler(handler_cls):  # type: ignore[misc, valid-type]
        @property
        def client_address(self):  # noqa: ANN001 - shadows BaseRequestHandler
            return (client_ip, 54321)

        @client_address.setter
        def client_address(self, _value):  # noqa: ANN001 - socketserver assigns on init
            pass  # keep pretending, whatever the real peer was

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeClientHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def test_neighbouring_overlay_node_is_rejected_with_403_off_overlay() -> None:
    guard = guardmod.Guard(vault_root=VAULT_ROOT)
    handler = guardmod._make_handler(guard, "test-guard")
    server, port = _serve_with_fake_client(handler, "10.100.0.2")
    try:
        assert _post_patch(handler, port) == 403
    finally:
        server.shutdown()
        server.server_close()


def test_master_request_passes_the_source_gate() -> None:
    """The Master must get past the source check.

    It then fails later (no Master key is loaded in this bare Guard), which is
    exactly what proves the source gate let it through rather than 403-ing it.
    """
    guard = guardmod.Guard(vault_root=VAULT_ROOT)
    handler = guardmod._make_handler(guard, "test-guard")
    server, port = _serve_with_fake_client(handler, "10.100.0.1")
    try:
        status = _post_patch(handler, port)
        assert status != 403, "the Master must not be rejected as off-overlay"
        assert status == 503, "expected the later no_master_key refusal"
    finally:
        server.shutdown()
        server.server_close()
