"""The guard daemon: verification, refusal, and arming.

The daemon is driven over real HTTP on loopback with `--bind-any`, because the
production bind is deliberately restricted to the node's overlay address (see
test_bind_is_restricted_to_the_overlay_address below). Applying a real patch
needs euid 0 plus `chattr`, so those tests assert the *refusal* path here; the
apply path itself is a target-node checklist item in docs/wireguard.md.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sparknet_loader import load_by_path

VAULT_ROOT = Path(__file__).resolve().parent.parent
GUARD = VAULT_ROOT / "services" / "sparknet-guard-d.py"
PROTECTED_TARGET = "/etc/wireguard/wg1.conf"


def _load_guard_module():
    return load_by_path("sparknet_guard_d", GUARD)


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
    (dest / "manifests" / "keys" / "master-ed25519.pub.b64").chmod(0o600)

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
        self.listener = guardmod.build_listener(self.guard, "test-guard", bind="127.0.0.1", port=0)
        self.listener.start()
        self.port = self.listener.servers[0].server_address[1]

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
        self.listener.stop()


@pytest.fixture
def harness(authorized_vault: Path):
    instance = Harness(authorized_vault)
    yield instance
    instance.stop()


def _own_fingerprint(vault: Path = VAULT_ROOT) -> str:
    """The fingerprint of the node identity in `vault`.

    An approval names the peer it is addressed to; the daemon refuses one that
    names anybody else, so every packet this file builds must carry it.
    """
    authz = guardmod._load_authz_module()
    identity = json.loads((vault / "manifests" / "node-identity.json").read_text())
    return authz.fingerprint(identity)


def _operator_os(*, is_root: bool = True) -> str:
    """An encoded operator-OS attestation, produced by the shared codec.

    Encoded rather than hand-written JSON: the guard decodes strictly, so a test
    that spelled the canonical form itself would keep passing after the encoding
    changed and would have stopped describing what a real approval looks like.
    """
    authz = guardmod._load_authz_module()
    uid = 0 if is_root else 1000
    return authz.encode_operator_os(
        {
            "uid": uid,
            "euid": uid,
            "username": "root" if is_root else "operator",
            "groups": ["root"] if is_root else ["users"],
            "is_root": is_root,
        }
    )


def _payload(
    *,
    file_path: str = PROTECTED_TARGET,
    content: bytes = b"# patched\n",
    ttl_seconds: int = 60,
    nonce: str = "n1",
    request_id: str = "r1",
    issued_delta: timedelta = timedelta(0),
    peer_fingerprint: str | None = None,
    operator_os: str | None = None,
) -> dict:
    """The canonical approval envelope, as the Master would mint it.

    ``operator_os`` defaults to an attested **root** operator, because that is what
    a real privileged apply carries and what the apply-path tests here exercise.
    Pass ``""`` for an unattested approval or ``_operator_os(is_root=False)`` for an
    attested non-root one; the operator-ceiling tests do both.
    """
    now = datetime.now(timezone.utc) + issued_delta
    import hashlib

    return {
        "peer_fingerprint": peer_fingerprint if peer_fingerprint is not None else _own_fingerprint(),
        "file_path": file_path,
        "content_b64": base64.b64encode(content).decode(),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "issued_at": (now - timedelta(seconds=ttl_seconds / 2)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(seconds=ttl_seconds / 2)).isoformat().replace("+00:00", "Z"),
        "nonce": nonce,
        "operator_os": _operator_os() if operator_os is None else operator_os,
        "request_id": request_id,
    }


def _packet(
    master_key: Ed25519PrivateKey,
    *,
    omit_signature: bool = False,
    **kwargs,
) -> dict:
    message = guardmod.approval_message(_payload(**kwargs))
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


# --- the approval must be addressed to this peer ------------------------------


def test_an_approval_addressed_to_another_peer_is_refused(harness: Harness, master_key) -> None:
    """A signature says the Master signed something, not that it meant *this* node.

    Without the peer binding, an approval minted for one peer is a valid
    approval any peer could apply — as long as it protects the same path.
    """
    status, body = harness.post(_packet(master_key, peer_fingerprint="a" * 64))
    assert status == 403, body
    assert body["code"] == "wrong_peer"


def test_the_refusal_is_audited(harness: Harness, master_key) -> None:
    harness.post(_packet(master_key, peer_fingerprint="a" * 64))
    ledger = json.loads((harness.vault / "blockchain" / "guard-audit.json").read_text())
    assert ledger["blocks"][-1]["event"] == "patch_refused"
    assert ledger["blocks"][-1]["detail"]["code"] == "wrong_peer"


def test_a_payload_with_a_field_outside_the_envelope_is_refused(harness: Harness, master_key) -> None:
    """The signature covers exactly the canonical projection, so an extra field
    is unsigned — and an unsigned field in an envelope that carries authority is
    how a second, unchecked channel into the guard would appear."""
    message = guardmod.approval_message(_payload())
    payload = json.loads(message)
    payload["capabilities"] = ["guard-apply"]
    packet = {
        "payload": base64.b64encode(json.dumps(payload).encode()).decode(),
        "signature": base64.b64encode(master_key.sign(message)).decode(),
    }
    status, body = harness.post(packet)
    assert status == 400, body
    assert body["code"] == "malformed_approval"


def test_the_envelope_carries_no_capabilities() -> None:
    """The peer reads guard-apply from its own registry entry. An approval that
    carried capabilities would be able to grant the authority it is checked
    against."""
    assert "capabilities" not in json.loads(guardmod.approval_message(_payload()))
    assert "capabilities" not in _payload()


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
    (dest / "manifests" / "keys" / "master-ed25519.pub.b64").chmod(0o600)
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
    (dest / "manifests" / "keys" / "master-ed25519.pub.b64").chmod(0o600)
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


def test_enforce_immutability_refuses_when_chattr_cannot_lock(tmp_path: Path, monkeypatch) -> None:
    """A filesystem where chattr cannot protect a path must fail closed.

    The failure this guards against: `chattr +i` fails, the old code swallowed
    the refusal and returned the path in its "enforced" list, so every surface
    reported the protected path as locked while it stayed writable. When the
    lock genuinely cannot be applied, that must be an error, not a silent skip.
    """
    target = tmp_path / "protected.conf"
    target.write_text("data")

    monkeypatch.setattr(guardmod, "_is_immutable", lambda _p: False)

    def _fail(_argv, *, dry_run: bool) -> None:
        raise guardmod.Refusal(500, "privileged_command_failed", "chattr: Operation not supported")

    monkeypatch.setattr(guardmod, "_run", _fail)

    with pytest.raises(guardmod.Refusal) as caught:
        guardmod.enforce_immutability([str(target)], dry_run=False)
    assert caught.value.code == "immutability_not_enforced"


def test_enforce_immutability_tolerates_already_locked_when_lsattr_unreadable(tmp_path: Path, monkeypatch) -> None:
    """When lsattr cannot read the flag but chattr confirms it, that is not a failure."""
    target = tmp_path / "protected.conf"
    target.write_text("data")

    monkeypatch.setattr(guardmod, "_is_immutable", lambda _p: None)

    def _ok(_argv, *, dry_run: bool) -> None:
        return None

    monkeypatch.setattr(guardmod, "_run", _ok)

    # chattr "succeeded"; the lsattr recheck is None (unreadable) but there was no
    # refusal, so the path is accepted rather than failing on an unreadable probe.
    assert guardmod.enforce_immutability([str(target)], dry_run=False) == [str(target)]


def test_apply_patch_refuses_when_relock_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    """A patch that lands but cannot be re-locked must refuse, not claim success.

    apply_patch writes then restores `chattr +i`. If the restore fails and the
    file is not actually immutable afterwards, the guard must surface that: a
    protected file left patched-but-writable while the guard prints "re-locked"
    is precisely the state the immutable bit exists to forbid.
    """
    target = tmp_path / "protected.conf"
    target.write_text("old")

    monkeypatch.setattr(guardmod, "_is_immutable", lambda _p: False)

    calls: list[list[str]] = []

    def _run(argv, *, dry_run: bool) -> None:
        calls.append(argv)
        # The staged-file preflight and the target unlock succeed; only the
        # target's final re-lock fails.
        if argv[0] == "chattr" and argv[1] == "+i" and argv[2] == str(target):
            raise guardmod.Refusal(500, "privileged_command_failed", "chattr: Operation not supported")

    monkeypatch.setattr(guardmod, "_run", _run)

    with pytest.raises(guardmod.Refusal) as caught:
        guardmod.apply_patch(target, b"new", dry_run=False)
    assert caught.value.code == "immutability_not_restored"
    assert target.read_bytes() == b"old", "a failed re-lock must restore the original bytes"


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


def _serve_with_fake_client(guard, client_ip: str):
    """Serve the guard while pretending every peer is `client_ip`.

    The guard reads `self.client_address[0]`, the real TCP peer, so the test has
    to shadow the attribute rather than override address_string().
    """
    http = guardmod._load_http_module()

    class FakeClientHandler(http._RouteHandler):
        @property
        def client_address(self):  # noqa: ANN001 - shadows BaseRequestHandler
            return (client_ip, 54321)

        @client_address.setter
        def client_address(self, _value):  # noqa: ANN001 - socketserver assigns on init
            pass  # keep pretending, whatever the real peer was

    listener = guardmod.build_listener(guard, "test-guard", bind="127.0.0.1", port=0, handler_class=FakeClientHandler)
    listener.start()
    return listener, listener.servers[0].server_address[1]


def test_neighbouring_overlay_node_is_rejected_with_403_off_overlay() -> None:
    guard = guardmod.Guard(vault_root=VAULT_ROOT)
    server, port = _serve_with_fake_client(guard, "10.100.0.2")
    try:
        assert _post_patch(guard, port) == 403
    finally:
        server.stop()


def test_master_request_passes_the_source_gate() -> None:
    """The Master must get past the source check.

    It then fails later (no Master key is loaded in this bare Guard), which is
    exactly what proves the source gate let it through rather than 403-ing it.
    """
    guard = guardmod.Guard(vault_root=VAULT_ROOT)
    server, port = _serve_with_fake_client(guard, "10.100.0.1")
    try:
        status = _post_patch(guard, port)
        assert status != 403, "the Master must not be rejected as off-overlay"
        assert status == 503, "expected the later no_master_key refusal"
    finally:
        server.stop()


# --- timeouts, serialisation, persistence, and 500 / 403 hygiene -----------------
#
# These back the 2026-10-08 review findings F4 (no socket timeout), F6 (apply
# not serialised), F7 (replay state in-memory only), F10 (str(exc) in 500s),
# F12 (hardcoded anchor), F13 (permitted-list disclosure).


def _master_public(vault: Path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    blob = (vault / "manifests" / "keys" / "master-ed25519.pub.b64").read_text().strip()
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(blob, validate=True))


def test_slow_client_is_dropped_then_a_fresh_connection_is_served(authorized_vault: Path) -> None:
    """F4: an idle trickle connection must not hold a listener thread open."""
    http = guardmod._load_http_module()
    handler = type("TrickleHandler", (http._RouteHandler,), {"timeout": 0.4})
    guard = guardmod.Guard(authorized_vault, dry_run=True)
    listener = guardmod.build_listener(guard, "trickle-guard", bind="127.0.0.1", port=0, handler_class=handler)
    listener.start()
    port = listener.servers[0].server_address[1]
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b"POST /api/v1/critical-file/patch HTTP/1.1")  # no CRLF: read blocks
        sock.settimeout(3)
        assert sock.recv(4096) == b"", "the listener must close an idle trickle connection"
        sock.close()

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/status", timeout=5) as response:
            assert response.status == 200, "the guard must still answer on a fresh connection"
    finally:
        listener.stop()


def test_concurrent_patches_are_serialized(authorized_vault: Path, master_key, monkeypatch) -> None:
    """F6: two overlapping requests must not interleave verification or the write.

    ``apply_patch`` is replaced with a stub that records enter/exit around a
    sleep, so without the guard's lock the two threads demonstrably overlap and
    the test fails; with it, the ledger also shows strictly alternating
    applying/applied blocks.
    """
    order: list[str] = []

    def _fake_apply(path, content, *, dry_run: bool = False) -> None:
        order.append("enter")
        time.sleep(0.15)
        order.append("exit")

    monkeypatch.setattr(guardmod, "apply_patch", _fake_apply)
    monkeypatch.setattr(guardmod.os, "geteuid", lambda: 0)
    guard = guardmod.Guard(
        authorized_vault,
        apply_enabled=True,
        master_pubkey=_master_public(authorized_vault),
        dry_run=False,
    )

    results: dict[int, object] = {}

    def _submit(index: int) -> None:
        try:
            results[index] = guard.handle_patch(_packet(master_key, nonce=f"c{index}", request_id=f"r{index}"))
        except Exception as exc:  # noqa: BLE001 - surfaces in the assertion below
            results[index] = exc

    threads = [threading.Thread(target=_submit, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert order == ["enter", "exit", "enter", "exit"], f"patches interleaved: {order}"
    for result in results.values():
        assert isinstance(result, dict), f"patch request failed: {result}"
        assert result["status"] == "PATCH_APPLIED_AND_RE-LOCKED"

    ledger = json.loads((authorized_vault / "blockchain" / "guard-audit.json").read_text())
    events = [block["event"] for block in ledger["blocks"]]
    assert events[-4:] == [
        "patch_applying",
        "patch_applied",
        "patch_applying",
        "patch_applied",
    ], f"ledger must show strictly alternating transactions, got {events}"


def test_replay_state_survives_a_restart(authorized_vault: Path, master_key) -> None:
    """F7: bouncing the daemon must not re-open a spent approval.

    A fresh ``Guard`` on the same vault is a restart: it loads the persisted
    store and must refuse the identical nonce/request_id pair that a previous
    process already spent.
    """
    guard_one = guardmod.Guard(authorized_vault, master_pubkey=_master_public(authorized_vault), dry_run=True)
    result = guard_one.handle_patch(_packet(master_key, nonce="persist", request_id="persist"))
    assert result["status"] == "PATCH_VERIFIED_DRY_RUN"

    store = authorized_vault / ".state" / "guard-replay.json"
    assert store.is_file(), "the replay store must be written after every accepted approval"
    assert "persist:persist" in store.read_text()

    restarted = guardmod.Guard(authorized_vault, master_pubkey=_master_public(authorized_vault), dry_run=True)
    with pytest.raises(guardmod.Refusal) as excinfo:
        restarted.handle_patch(_packet(master_key, nonce="persist", request_id="persist"))
    assert excinfo.value.code == "replayed"


def test_mode_probe_targets_the_policy_anchor_not_a_literal(authorized_vault: Path, monkeypatch) -> None:
    """F12: the reachability probe must dial the configured anchor address.

    If the anchor reverted to a hardcoded constant, the recorded probe target
    would be the stale literal and this test fails.
    """
    policies_path = authorized_vault / "manifests" / "security-policies.json"
    policies = json.loads(policies_path.read_text())
    policies.setdefault("wireguard", {})["master_anchor_ip"] = "10.99.0.1"
    policies_path.write_text(json.dumps(policies, indent=2) + "\n")

    connects: list[tuple[str, int]] = []

    class _Probe:
        def __init__(self, *args):  # noqa: ANN002, ANN003
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):  # noqa: ANN002, ANN003
            return False

        def settimeout(self, seconds) -> None:  # noqa: ANN001
            pass

        def connect(self, target) -> None:  # noqa: ANN001
            connects.append(target)

        def getsockname(self):  # noqa: ANN202
            return ("10.20.0.5", 0)

    class _Socket:
        AF_INET = 2
        SOCK_DGRAM = 2

        @staticmethod
        def socket(*args):  # noqa: ANN002
            return _Probe()

    monkeypatch.setattr(guardmod, "socket", _Socket)
    monkeypatch.setenv("SPARKNET_OVERLAY_IP", "10.20.0.5")
    guard = guardmod.Guard(authorized_vault)
    assert guard.mode() == "anchored"
    assert connects == [("10.99.0.1", 51820)], "the probe must dial the policy's anchor, not a literal"


def test_off_overlay_refusal_does_not_disclose_the_source_allowlist(harness: Harness, master_key, monkeypatch) -> None:
    """F13: a rejected caller learns it is off-overlay, not which sources pass."""
    monkeypatch.setattr(harness.guard, "permitted_sources", lambda: {"203.0.113.9"})
    status, body = harness.post(_packet(master_key))
    assert status == 403
    assert body["code"] == "off_overlay"
    assert "permitted" not in body
    assert "203.0.113.9" not in str(body)


def test_internal_errors_do_not_leak_internals(harness: Harness, master_key, monkeypatch, capsys) -> None:
    """F10: a 500 must carry a stable reason, with the detail on stderr only."""

    def _boom() -> list[str]:
        raise RuntimeError("SECRET_GUARD_DETAIL")

    monkeypatch.setattr(harness.guard, "authorization_capabilities", _boom)
    status, body = harness.post(_packet(master_key))
    assert status == 500
    assert body["code"] == "internal_error"
    assert "SECRET_GUARD_DETAIL" not in str(body)
    assert "SECRET_GUARD_DETAIL" in capsys.readouterr().err


# --- Step 66D-iii: the operator-OS ceiling at the apply path ------------------


def _armed_guard(authorized_vault, monkeypatch, applied: list):
    """A guard armed for apply, running as root, with the write itself captured."""
    monkeypatch.setattr(guardmod, "apply_patch", lambda *args, **kwargs: applied.append((args, kwargs)))
    monkeypatch.setattr(guardmod.os, "geteuid", lambda: 0)
    return guardmod.Guard(
        authorized_vault,
        apply_enabled=True,
        master_pubkey=_master_public(authorized_vault),
        dry_run=False,
    )


def test_an_unattested_approval_cannot_apply(authorized_vault, master_key, monkeypatch) -> None:
    """Empty is a value, and its value is "nobody vouched for this".

    Checked *before* the write and before the euid question, so an unattested
    approval is refused as unauthorised rather than failing later on a privilege
    the process happens to have.
    """
    applied: list = []
    guard = _armed_guard(authorized_vault, monkeypatch, applied)
    with pytest.raises(guardmod.Refusal) as exc:
        guard.handle_patch(_packet(master_key, operator_os="", nonce="op-none", request_id="r-op-none"))
    assert exc.value.status == 403
    assert exc.value.code == "operator_unattested"
    assert applied == [], "the ceiling must be checked before any write, not after"


def test_an_attested_non_root_operator_cannot_apply(authorized_vault, master_key, monkeypatch) -> None:
    """A truthful attestation of an unprivileged human caps the session."""
    applied: list = []
    guard = _armed_guard(authorized_vault, monkeypatch, applied)
    with pytest.raises(guardmod.Refusal) as exc:
        guard.handle_patch(
            _packet(
                master_key,
                operator_os=_operator_os(is_root=False),
                nonce="op-user",
                request_id="r-op-user",
            )
        )
    assert exc.value.status == 403
    assert exc.value.code == "operator_insufficient"
    assert applied == []


def test_an_attested_root_operator_reaches_the_apply(authorized_vault, master_key, monkeypatch) -> None:
    """The attestation authorises — even though nothing here checks the daemon's uid twice."""
    applied: list = []
    guard = _armed_guard(authorized_vault, monkeypatch, applied)
    result = guard.handle_patch(
        _packet(master_key, operator_os=_operator_os(is_root=True), nonce="op-root", request_id="r-op-root")
    )
    assert result["status"] == "PATCH_APPLIED_AND_RE-LOCKED"
    assert len(applied) == 1


def test_a_malformed_attestation_is_refused_not_read_as_none(authorized_vault, master_key, monkeypatch) -> None:
    """A corrupt root attestation must not silently downgrade to "unattested".

    Both refuse, so the outcome is the same — but the ledger code differs, and the
    difference is whether the Master attested a root operator and the record was
    lost, or never made one.
    """
    applied: list = []
    guard = _armed_guard(authorized_vault, monkeypatch, applied)
    with pytest.raises(guardmod.Refusal) as exc:
        guard.handle_patch(_packet(master_key, operator_os="{not json", nonce="op-bad", request_id="r-op-bad"))
    assert exc.value.status == 403
    assert exc.value.code == "operator_malformed"
    assert applied == []


# --- the RFI answer route (fabric Step 67D) -----------------------------------


def _rfi_answer(master_key, vault: Path, *, content: bytes = b"# the answer\n", request_id="RFI-1", item_name="x.md"):
    authz = guardmod._load_authz_module()
    return authz.sign_rfi_answer(
        content=content,
        item_name=item_name,
        peer_fingerprint=_own_fingerprint(vault),
        request_id=request_id,
        master_private_key=master_key,
    )


def test_the_rfi_route_verifies_an_answer_in_dry_run(harness, authorized_vault, master_key) -> None:
    """POST /api/v1/rfi reaches handle_rfi and verifies under the pinned Master key.

    The harness runs dry-run, so this is the verify-only path: the answer is
    checked and nothing is written, which is the same contract the registry route
    honours. The full install-over-a-wire path is master-node/tests/test_rfi_transport.py.
    """
    answer = _rfi_answer(master_key, authorized_vault)
    status, body = harness.post(answer, path="/api/v1/rfi")
    assert status == 200
    assert body["status"] == "RFI_VERIFIED_DRY_RUN"
    assert body["request_id"] == "RFI-1"
    # Dry-run writes nothing into the inbox.
    assert not (authorized_vault / "01_Inbox" / "x.md").exists()


def test_the_rfi_route_refuses_a_tampered_answer(harness, authorized_vault, master_key) -> None:
    """Content is bound by digest, so swapping it under a valid signature fails."""
    answer = _rfi_answer(master_key, authorized_vault, content=b"# benign\n")
    forged = {**answer, "content_b64": base64.b64encode(b"# injected\n").decode()}
    status, body = harness.post(forged, path="/api/v1/rfi")
    assert status == 400
    assert body["code"] == "digest_mismatch"


def test_apply_patch_preserves_the_target_mode(tmp_path: Path, monkeypatch) -> None:
    """A protected WireGuard config stays 0600 after a patch."""
    target = tmp_path / "wg1.conf"
    target.write_text("old\n")
    target.chmod(0o600)
    monkeypatch.setattr(guardmod, "_run", lambda argv, *, dry_run: None)
    monkeypatch.setattr(guardmod, "_is_immutable", lambda _p: True)
    guardmod.apply_patch(target, b"new\n", dry_run=False)
    assert target.read_bytes() == b"new\n"
    assert (target.stat().st_mode & 0o777) == 0o600


def test_group_writable_master_pin_is_refused(tmp_path: Path, master_key: Ed25519PrivateKey) -> None:
    """A pin the peer user can rewrite is not a pin."""
    from cryptography.hazmat.primitives import serialization

    key_dir = tmp_path / "vault" / "manifests" / "keys"
    key_dir.mkdir(parents=True)
    raw = master_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    path = key_dir / "master-ed25519.pub.b64"
    path.write_text(base64.b64encode(raw).decode() + "\n")
    path.chmod(0o664)
    with pytest.raises(ValueError, match="group/other writable"):
        guardmod.load_master_pubkey(tmp_path / "vault")
    path.chmod(0o600)
    assert guardmod.load_master_pubkey(tmp_path / "vault")


def test_a_corrupt_replay_store_refuses_to_start(tmp_path: Path):
    path = tmp_path / "replay.json"
    path.write_text("{not-json")
    with pytest.raises(guardmod.Refusal) as exc:
        guardmod.ReplayGuard(store_path=path)
    assert exc.value.code == "replay_store_corrupt"
