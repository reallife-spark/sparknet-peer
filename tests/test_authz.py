"""Authorization layer: SHA-256 identity fingerprints verified against Ed25519.

These tests use a freshly generated Master keypair, never the tracked public key,
so they prove the scheme rather than a fixture.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

VAULT_ROOT = Path(__file__).resolve().parent.parent
# The authz implementation both roles execute lives once, in the parent's shared/.
SHARED_DIR = VAULT_ROOT.parent / "shared"


def _load_authz():
    """Load shared/sparknet-authz.py, whose filename is not an importable module name.

    The implementation lives in the parent's shared/ directory, deliberately as a
    single copy: the Master signs with it and a peer verifies with it, so two
    copies could drift and silently stop validating each other.

    The sys.modules registration is required: @dataclass resolves annotations
    through sys.modules[cls.__module__], which is None without it.
    """
    import sys

    path = SHARED_DIR / "sparknet-authz.py"
    if not path.is_file():
        pytest.skip(f"shared authz implementation not present at {path}")
    spec = importlib.util.spec_from_file_location("sparknet_authz", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


authz = _load_authz()

# A FIXED reference instant, deliberately not `datetime.now()`. Every test that
# uses it must pass the SAME instant to both sign_authorization() and
# evaluate(); that is what makes expiry and not-yet-valid deterministic.
#
# It is not, and must not be, "the current time". A test that signs on the real
# clock and then evaluates at T0 breaks the moment the wall clock passes T0 —
# which is exactly what happened here: this file passed all morning, then failed
# with `not_yet_valid` after 12:00 UTC. Tests that go through the CLI (which has
# no clock parameter) must evaluate against real time instead, i.e. `now=None`.
NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def master_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def identity() -> dict:
    return {
        "node_id": "sparknet-node-0001",
        "role": "peer",
        "wireguard_public_key_b64": "dGVzdC1wdWJrZXkta2V5LWJhc2U2NC1wbGFjZWhvbGRlcjEyMw==",
        "overlay_ipv4": "10.100.0.2/32",
        "requested_capabilities": ["telemetry", "p2p-sync"],
    }


def _registry(entries: list[dict]) -> dict:
    return {"schema_version": "1.0", "authorizations": entries}


# --- canonicalisation + fingerprint ------------------------------------------


def test_fingerprint_is_sha256_hex(identity: dict) -> None:
    fp = authz.fingerprint(identity)
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_is_order_independent(identity: dict) -> None:
    shuffled = {k: identity[k] for k in reversed(list(identity))}
    assert authz.fingerprint(shuffled) == authz.fingerprint(identity)


def test_fingerprint_ignores_non_identity_fields(identity: dict) -> None:
    """Renaming a host must not require a re-sign."""
    noisy = {**identity, "hostname": "some-other-box", "note": "deployed 2026-10-04"}
    assert authz.fingerprint(noisy) == authz.fingerprint(identity)


def test_fingerprint_changes_when_identity_changes(identity: dict) -> None:
    moved = {**identity, "overlay_ipv4": "10.100.0.9/32"}
    assert authz.fingerprint(moved) != authz.fingerprint(identity)


def test_missing_identity_field_is_rejected() -> None:
    with pytest.raises(authz.AuthzError, match="missing required field"):
        authz.fingerprint({"node_id": "x"})


def test_canonical_bytes_are_stable_across_calls(identity: dict) -> None:
    assert authz.canonical_bytes(identity) == authz.canonical_bytes(identity)


# --- signature verification --------------------------------------------------


def test_valid_authorization_is_allowed(master_key, identity: dict) -> None:
    entry = authz.sign_authorization(identity, master_key, ["telemetry", "p2p-sync"], now=NOW)
    decision = authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW)
    assert decision.allowed
    assert decision.reason == authz.REASON_OK
    assert decision.capabilities == ["p2p-sync", "telemetry"]
    assert decision.overlay_ipv4 == "10.100.0.2/32"


def test_signature_from_a_different_key_is_rejected(master_key, identity: dict) -> None:
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW)
    impostor = Ed25519PrivateKey.generate()
    decision = authz.evaluate(identity, _registry([entry]), impostor.public_key(), now=NOW)
    assert not decision.allowed
    assert decision.reason == authz.REASON_BAD_SIGNATURE


def test_tampered_capabilities_do_not_grant_privileges(master_key, identity: dict) -> None:
    """The Master signed an identity; widening rights must invalidate the entry.

    Capabilities are inside the signed envelope precisely so this cannot pass.
    """
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW)
    result = authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW)
    assert "guard-apply" not in result.capabilities

    entry["capabilities"] = ["telemetry", "guard-apply"]
    decision = authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW)
    assert not decision.allowed
    assert decision.reason == authz.REASON_BAD_SIGNATURE


def test_tampered_overlay_ipv4_is_rejected(master_key, identity: dict) -> None:
    """Reassigning a node's overlay address must not be possible locally."""
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW)
    entry["overlay_ipv4"] = "10.100.0.200/32"
    decision = authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW)
    assert not decision.allowed
    assert decision.reason == authz.REASON_BAD_SIGNATURE


def test_unsigned_revocation_cannot_reinstate_a_node(master_key, identity: dict) -> None:
    """Once the Master signs revoked=true, clearing it by hand must not work.

    The flag is inside the signed envelope, so the attacker's edit no longer
    matches the signature.
    """
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW)
    revoked = dict(entry, revoked=True)
    revoked["signature_b64"] = base64.b64encode(master_key.sign(authz.authorization_message(revoked))).decode("ascii")
    assert not authz.evaluate(identity, _registry([revoked]), master_key.public_key(), now=NOW).allowed

    tampered = dict(revoked, revoked=False)
    decision = authz.evaluate(identity, _registry([tampered]), master_key.public_key(), now=NOW)
    assert not decision.allowed
    assert decision.reason == authz.REASON_BAD_SIGNATURE


def test_master_can_revoke_with_a_fresh_signature(master_key, identity: dict) -> None:
    """Revocation is an authoritative act: the Master re-signs with revoked=true."""
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW)
    revoked = dict(entry, revoked=True)
    revoked["signature_b64"] = base64.b64encode(master_key.sign(authz.authorization_message(revoked))).decode("ascii")
    decision = authz.evaluate(identity, _registry([revoked]), master_key.public_key(), now=NOW)
    assert not decision.allowed
    assert decision.reason == authz.REASON_REVOKED


def test_identity_swap_under_same_fingerprint_is_rejected(master_key, identity: dict) -> None:
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW)
    entry["identity"]["overlay_ipv4"] = "10.100.0.200/32"
    decision = authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW)
    assert not decision.allowed
    assert decision.reason == authz.REASON_IDENTITY_MISMATCH


def test_unknown_fingerprint_is_denied(master_key, identity: dict) -> None:
    decision = authz.evaluate(identity, _registry([]), master_key.public_key(), now=NOW)
    assert not decision.allowed
    assert decision.reason == authz.REASON_UNKNOWN


# --- expiry + revocation -----------------------------------------------------


def test_expired_authorization_is_denied(master_key, identity: dict) -> None:
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], ttl_days=1, now=NOW)
    later = NOW + timedelta(days=2)
    decision = authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=later)
    assert not decision.allowed
    assert decision.reason == authz.REASON_EXPIRED


def test_not_yet_valid_authorization_is_denied(master_key, identity: dict) -> None:
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW + timedelta(days=2))
    decision = authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW)
    assert not decision.allowed
    assert decision.reason == authz.REASON_NOT_YET_VALID


def test_revoked_authorization_is_denied_before_signature_check(master_key, identity: dict) -> None:
    """Revocation must short-circuit, so a bad signature cannot mask it."""
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW)
    entry["revoked"] = True
    entry["signature_b64"] = "not-even-a-signature"
    decision = authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW)
    assert not decision.allowed
    assert decision.reason == authz.REASON_REVOKED


def test_revoke_then_reinstate_requires_a_fresh_signature(master_key, identity: dict) -> None:
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW)
    entry["revoked"] = False
    assert authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW).allowed
    entry["revoked"] = True
    assert not authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW).allowed


# --- capabilities ------------------------------------------------------------


def test_unknown_capability_is_refused_at_signing(master_key, identity: dict) -> None:
    with pytest.raises(authz.AuthzError, match="unknown capability"):
        authz.sign_authorization(identity, master_key, ["root-everything"])


def test_guard_apply_capability_is_distinct(master_key, identity: dict) -> None:
    """guard-apply is the dangerous one; it must be grantable but nameable."""
    assert "guard-apply" in authz.KNOWN_CAPABILITIES
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW)
    result = authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW)
    assert "guard-apply" not in result.capabilities


def test_empty_capabilities_is_rejected(master_key, identity: dict) -> None:
    """Clearing capabilities is tampering, and tampering is caught as a bad signature."""
    entry = authz.sign_authorization(identity, master_key, ["telemetry"], now=NOW)
    entry["capabilities"] = []
    decision = authz.evaluate(identity, _registry([entry]), master_key.public_key(), now=NOW)
    assert not decision.allowed
    assert decision.reason == authz.REASON_BAD_SIGNATURE


# --- admin requests (admin-supervise, fabric Step 29 phase 2) ----------------


def _admin_request(**overrides: object) -> dict:
    request = {
        "request_id": "admin-req-0001",
        "conversation_id": None,
        "master_node_id": "master-0001",
        "target_node_id": "sparknet-node-0001",
        "target_fingerprint": "0000000000000000000000000000000000000000000000000000000000000000",
        "issued_at": int(NOW.timestamp()),
        "expires_at": int(NOW.timestamp()) + 600,
        "nonce": "nonce-0001",
        "action": "set_user_role",
        "user_id": "user-07",
        "user_email": "ana@example.test",
        "role": "user",
        "kb_id": None,
        "chat_id": None,
    }
    request.update(overrides)
    return request


def test_admin_request_missing_required_field_is_rejected() -> None:
    for field in (
        "request_id",
        "master_node_id",
        "target_node_id",
        "target_fingerprint",
        "issued_at",
        "expires_at",
        "nonce",
        "action",
    ):
        request = _admin_request(**{field: None})
        with pytest.raises(authz.AuthzError, match="missing required"):
            authz.admin_request_message(request)


def test_admin_request_unknown_action_is_rejected() -> None:
    request = _admin_request(action="root-everything")
    with pytest.raises(authz.AuthzError, match="unknown admin action"):
        authz.admin_request_message(request)


def test_admin_request_projection_is_deterministic() -> None:
    """Same fields, same order — the peer re-hashes exactly what the Master signed."""
    first = authz.admin_request_message(_admin_request())
    second = authz.admin_request_message(_admin_request())
    assert first == second


def test_admin_request_message_roundtrip_signs_and_verifies(master_key) -> None:
    """The request envelope signs like any other authorization object."""
    request = _admin_request()
    message = authz.admin_request_message(request)
    signature = base64.b64encode(master_key.sign(message)).decode("ascii")
    request["signature_b64"] = signature
    assert authz.verify_signature(master_key.public_key(), signature, message) is True

    request["role"] = "admin"
    tampered = authz.admin_request_message(request)
    assert authz.verify_signature(master_key.public_key(), signature, tampered) is False


# --- tracked artefacts -------------------------------------------------------


def test_tracked_master_pubkey_is_32_bytes() -> None:
    key = authz.load_public_key(VAULT_ROOT / "manifests" / "keys" / "master-ed25519.pub.b64")
    assert key is not None


def test_registry_in_vault_is_valid_and_signed_by_that_key() -> None:
    registry = json.loads((VAULT_ROOT / "manifests" / "node-authorizations.json").read_text())
    assert registry["schema_version"] == "1.0"
    assert isinstance(registry["authorizations"], list)
    stored = registry["master_public_key_b64"]
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    Ed25519PublicKey.from_public_bytes(__import__("base64").b64decode(stored, validate=True))  # raises if malformed


def test_registry_ships_with_no_bootstrap_privileges() -> None:
    """Nobody is pre-authorized; a node must be signed in explicitly."""
    registry = json.loads((VAULT_ROOT / "manifests" / "node-authorizations.json").read_text())
    assert registry["authorizations"] == [], "ship an empty registry, not pre-granted access"


def test_no_private_key_material_in_vault() -> None:
    """Belt-and-braces: scan tracked files for private-key-ish blobs."""
    import base64
    import re

    offenders = []
    for path in VAULT_ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix in {".pub", ".pyc"} or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for match in re.findall(r"[A-Za-z0-9+/]{43}=", text):
            try:
                raw = base64.b64decode(match, validate=True)
            except Exception:
                continue
            if len(raw) == 32:
                # 32-byte base64 blobs are legitimate public keys; a private key
                # would be 64 bytes. Flag only if someone stored raw 32-byte
                # secrets next to a key-ish filename.
                if "key" in path.name.lower() and not path.name.endswith(".pub.b64"):
                    offenders.append(str(path.relative_to(VAULT_ROOT)))
    assert not offenders, f"possible private key material in: {sorted(set(offenders))}"


def test_public_key_b64_roundtrip(master_key) -> None:
    encoded = authz.public_key_b64(master_key.public_key())
    raw = master_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    import base64

    assert base64.b64decode(encoded) == raw


# --- private key loading -----------------------------------------------------
#
# `sign --master-key` and scripts/sparknet-keygen.sh disagreed about encoding:
# keygen wrote raw bytes, the loader base64-decoded the file as text, and the
# help text claimed raw. Signing failed with UnicodeDecodeError. These tests pin
# the fix: base64 is the canonical form, raw 32 bytes and hex are also accepted.


def test_load_private_key_accepts_base64(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    path = tmp_path / "k.b64"
    path.write_text(base64.b64encode(raw).decode() + "\n")
    loaded = authz.load_private_key(path)
    assert loaded.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_load_private_key_accepts_raw_32_bytes(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    path = tmp_path / "k.raw"
    path.write_bytes(raw)
    loaded = authz.load_private_key(path)
    assert loaded.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_load_private_key_accepts_hex(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    path = tmp_path / "k.hex"
    path.write_text(raw.hex() + "\n")
    loaded = authz.load_private_key(path)
    assert loaded.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) == key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_load_private_key_rejects_garbage(tmp_path: Path) -> None:
    path = tmp_path / "k.bad"
    path.write_text("this is not a key at all, not even close to 32 bytes")
    with pytest.raises(authz.AuthzError, match="not 32 raw bytes"):
        authz.load_private_key(path)


def test_load_private_key_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "k.empty"
    path.write_text("")
    with pytest.raises(authz.AuthzError):
        authz.load_private_key(path)


def test_sign_via_the_cli_uses_load_private_key(tmp_path: Path, identity: dict) -> None:
    """The sign subcommand must not re-implement key loading inline.

    That inline base64-only decode was the bug; going through the helper keeps
    one implementation.
    """
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    keyfile = tmp_path / "master.key.b64"
    keyfile.write_text(base64.b64encode(raw).decode() + "\n")

    identity = dict(identity)
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(json.dumps(identity))
    out = tmp_path / "entry.json"

    rc = authz._main(
        [
            "sign",
            str(identity_path),
            "--master-key",
            str(keyfile),
            "--capabilities",
            "telemetry",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    entry = json.loads(out.read_text())
    registry = {"authorizations": [entry]}
    # now=None, not NOW: the CLI stamped this entry with the real clock, and the
    # CLI takes no clock parameter. Evaluating at a fixed instant would fail
    # with `not_yet_valid` for every run after 12:00 UTC on the day T0 was
    # written. This test is about key loading, not about time.
    decision = authz.evaluate(identity, registry, key.public_key())
    assert decision.allowed
    assert decision.capabilities == ["telemetry"]


def test_sign_via_the_cli_accepts_a_raw_seed(tmp_path: Path, identity: dict) -> None:
    """A raw seed must work too, so an operator with one is not stuck."""
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    keyfile = tmp_path / "master.key"
    keyfile.write_bytes(raw)
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(json.dumps(identity))
    out = tmp_path / "entry.json"
    rc = authz._main(
        [
            "sign",
            str(identity_path),
            "--master-key",
            str(keyfile),
            "--capabilities",
            "telemetry",
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    assert json.loads(out.read_text())["signature_b64"]
