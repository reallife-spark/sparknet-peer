"""Signed session grants: mint, verify, scope, and grant-scoped vault writes.

A grant answers ``which user, on which node, may reach which vault pages, until
when`` (contracts/identity-schema.json). These tests prove that

* the one verification path — shared/sparknet-authz.py — checks grants in
  guard-protocol order, with no second crypto implementation in provenance,
* every refusal (forged, expired, not-yet-valid, replayed, unknown/revoked
  node, widened capability, wrong presenter) fails closed,
* an out-of-scope write is *quarantined* under ``_raw/`` rather than refused or
  placed, and
* every use lands in the ledger as a dual-signed grant_use block attributed to
  the acting session, with its parent grant, so audit is finer than authority.

The Master does not exist yet, so every fixture mints with an offline operator
key: exactly what ``sparknet-authz.py grant-mint --master-key`` runs with no
Master present (PLAN Step 3 DoD 3).
"""

from __future__ import annotations

import base64
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sparknet_loader import load_by_path

VAULT_ROOT = Path(__file__).resolve().parent.parent
SHARED_DIR = VAULT_ROOT.parent / "shared"
CONTRACT = json.loads((VAULT_ROOT.parent / "contracts" / "identity-schema.json").read_text())


def _load(name: str, label: str) -> ModuleType:
    """Load a shared/ implementation whose filename is not importable."""
    path = SHARED_DIR / f"{name}.py"
    if not path.is_file():
        pytest.skip(f"shared implementation not present at {path}")
    return load_by_path(label, path)


authz = _load("sparknet-authz", "sparknet_authz_grants")
prov = _load("sparknet-provenance", "sparknet_provenance_grants")

# A FIXED reference instant. Mint and verify MUST pass the same instant (or a
# deliberately-earlier one, for not-yet-valid), exactly as test_authz.py demands:
# signing on the real clock and evaluating at a fixed T0 breaks after the wall
# clock passes T0.
NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _master_pub_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def master_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def ctx(tmp_path: Path, master_key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A relocatable vault copy with fresh keys and a Master-signed registry.

    The user record is force-filled (a fresh U-### with a fresh key in an
    isolated state dir), the node gets a fresh node key in the same state dir,
    and the registry is re-signed by this test's Master so it can never be
    confused with whatever real key was tracked yesterday.
    """
    vault = tmp_path / "vault"
    shutil.copytree(
        VAULT_ROOT,
        vault,
        ignore=shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "references", ".opencode"),
    )
    # copytree dereferences the `_raw -> 03_Sources` shim into a real (empty)
    # directory; restore the symlink so quarantine reads through it the way a
    # real node does.
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
    return {
        "vault": vault,
        "state": state,
        "identity": identity,
        "registry": registry,
        "master_key": master_key,
        "master_public_key": master_key.public_key(),
    }


def _grant(ctx: dict, **overrides) -> dict:
    params = dict(
        node_identity=ctx["identity"],
        grant_id="GRANT-0001",
        user_identity="U-20161",
        conversation_key="01010101-0101-0101-0101-010101010101-1",
        capabilities=["p2p-sync"],
        vault_scope=["05_Knowledge"],
        ttl_minutes=480,
        nonce="nonce-0001",
        master_private_key=ctx["master_key"],
        registry=ctx["registry"],
        now=NOW,
    )
    params.update(overrides)
    return authz.mint_grant(**params)


def _verify(ctx: dict, grant: dict, **overrides) -> authz.Decision:
    kwargs = dict(
        master_public_key=ctx["master_public_key"],
        registry=ctx["registry"],
        node_identity=ctx["identity"],
        consumed_nonces=set(),
        now=NOW,
    )
    kwargs.update(overrides)
    return authz.verify_grant(grant, **kwargs)


def _write(ctx: dict, *, target: str, session: str = "subagent-b/0001", grant: dict | None = None, **kw) -> tuple:
    g = grant or _grant(ctx, parent_grant_id="GRANT-0000")
    body = b"# Hello grant\n\nThis note was written under a signed session grant.\n"
    return prov.write_granted_note(
        ctx["vault"],
        g,
        target=target,
        body=body,
        session=session,
        registry=ctx["registry"],
        node_identity=ctx["identity"],
        clock=lambda: NOW,
        **kw,
    )


# --- mint --------------------------------------------------------------------


def test_minted_grant_verifies_ok(ctx):
    grant = _grant(ctx)
    decision = _verify(ctx, grant)
    assert decision.allowed
    assert decision.reason == authz.REASON_OK
    assert decision.fingerprint == grant["node_fingerprint"]
    assert decision.capabilities == ["p2p-sync"]


def test_mint_attaches_master_signature_over_the_projection(ctx):
    grant = _grant(ctx)
    message = authz.grant_message({k: v for k, v in grant.items() if k != "signature_b64"})
    assert authz.verify_signature(ctx["master_public_key"], grant["signature_b64"], message)


def test_mint_refuses_to_widen_the_node(ctx):
    with pytest.raises(authz.AuthzError, match="widen"):
        _grant(ctx, capabilities=["guard-apply"])


def test_mint_refuses_unknown_capability(ctx):
    with pytest.raises(authz.AuthzError, match="unknown capability"):
        _grant(ctx, capabilities=["warp-drive"])


# --- verify: the refusal ladder, in guard protocol order -----------------------


def test_contract_verification_order_is_guard_order(ctx):
    order = CONTRACT["grant"]["verification_order"]
    assert order == [
        "signature verifies under the node's authorised signing key",
        "fingerprint appears in the Master-signed registry, and is not revoked",
        "capabilities are a subset of those the registry grants that node",
        "expires_at is in the future; issued_at is not in the future beyond clock skew",
        "nonce has not been seen before on this node",
        "the peer presenting the grant is the peer named in it",
    ]


def test_forged_signature_refused(ctx):
    forged = _grant(ctx)
    sig = forged["signature_b64"]
    forged["signature_b64"] = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert _verify(ctx, forged).reason == authz.REASON_BAD_SIGNATURE


def test_expired_grant_refused(ctx):
    assert _verify(ctx, _grant(ctx, ttl_minutes=-10)).reason == authz.REASON_GRANT_EXPIRED


def test_not_yet_valid_grant_refused(ctx):
    earlier = _verify(ctx, _grant(ctx), now=NOW - timedelta(minutes=10))
    assert earlier.reason == authz.REASON_GRANT_NOT_YET_VALID


def test_nonce_reuse_refused(ctx):
    assert _verify(ctx, _grant(ctx), consumed_nonces={"nonce-0001"}).reason == authz.REASON_GRANT_NONCE_REUSED


def test_unknown_node_refused(ctx):
    empty = {**ctx["registry"], "authorizations": []}
    assert _verify(ctx, _grant(ctx), registry=empty).reason == authz.REASON_GRANT_UNKNOWN_NODE


def test_revoked_node_refused(ctx):
    entry = dict(ctx["registry"]["authorizations"][0], revoked=True)
    revoked = {**ctx["registry"], "authorizations": [entry]}
    assert _verify(ctx, _grant(ctx), registry=revoked).reason == authz.REASON_GRANT_REVOKED


def test_capability_widen_refused(ctx):
    # Minted without the registry (the verifier is the ceiling), then the
    # registry must reject the widened grant at verify time.
    get = _grant(ctx, capabilities=["guard-apply"], registry=None)
    assert _verify(ctx, get).reason == authz.REASON_GRANT_CAPABILITY_WIDEN


def test_malformed_grant_refused(ctx):
    stripped = _grant(ctx)
    del stripped["nonce"]
    assert _verify(ctx, stripped).reason == authz.REASON_GRANT_MALFORMED


def test_wrong_presenter_refused(ctx):
    other = dict(ctx["identity"], node_id="sparknet-node-0003")
    assert _verify(ctx, _grant(ctx), node_identity=other).reason == authz.REASON_GRANT_NODE_MISMATCH


def test_order_expiry_precedes_nonce(ctx):
    """A grant that is expired *and* replayed reports the earlier check."""
    decision = _verify(
        ctx,
        _grant(ctx, nonce="double-tap"),
        consumed_nonces={"double-tap"},
        now=NOW + timedelta(days=100),
    )
    assert decision.reason == authz.REASON_GRANT_EXPIRED


def test_order_nonce_precedes_presenter(ctx):
    decision = _verify(
        ctx,
        _grant(ctx, nonce="presenter-test"),
        node_identity=dict(ctx["identity"], node_id="sparknet-node-0003"),
        consumed_nonces={"presenter-test"},
    )
    assert decision.reason == authz.REASON_GRANT_NONCE_REUSED


def test_no_second_crypto_implementation():
    source = (SHARED_DIR / "sparknet-provenance.py").read_text()
    assert "from cryptography" not in source, "provenance must not implement crypto itself"
    assert "import cryptography" not in source


# --- scope containment ---------------------------------------------------------


def test_scope_containment_is_path_parts_not_strings():
    assert prov.grant_scope_allows(["05_Knowledge/concepts"], "05_Knowledge/concepts/grant-note.md")
    assert not prov.grant_scope_allows(["05_Knowledge/con"], "05_Knowledge/concepts/grant-note.md")
    assert not prov.grant_scope_allows([], "05_Knowledge/concepts/grant-note.md")
    assert not prov.grant_scope_allows(["05_Knowledge/concepts"], "00_System/user.md")
    assert prov.grant_scope_allows(["05_Knowledge"], "05_Knowledge/concepts/deep/grant-note.md")


def test_scope_refuses_paths_that_climb_out():
    with pytest.raises(prov.ProvenanceError):
        prov.grant_scope_allows(["05_Knowledge"], "05_Knowledge/../../etc/passwd")
    with pytest.raises(prov.ProvenanceError):
        prov.grant_scope_allows(["05_Knowledge"], "/etc/passwd")


# --- grant-scoped writes -------------------------------------------------------


def test_write_in_scope_lands_with_full_provenance(ctx):
    path, outcome, block = _write(ctx, target="05_Knowledge/concepts/grant-note.md", session="subagent-b/0001")

    assert outcome == "granted"
    assert path == ctx["vault"] / "05_Knowledge" / "concepts" / "grant-note.md"
    state, detail = prov.verify_note(path, vault=ctx["vault"])
    assert state == "valid", detail
    assert prov.verify_approval_blocks(ctx["vault"]) == []

    front, _ = prov.split_note(path.read_bytes())
    tracking = front["tracking"]
    assert tracking["owner_id"] == "U-20161"
    grant = tracking["grant"]
    assert grant["grant_id"] == "GRANT-0001"
    assert grant["parent_grant_id"] == "GRANT-0000"
    assert grant["session"] == "subagent-b/0001"
    assert grant["scope"] == ["05_Knowledge"]

    data = block["data"]
    assert data["event"] == "grant_use"
    assert data["grant_id"] == "GRANT-0001"
    assert data["parent_grant_id"] == "GRANT-0000"
    assert data["session"] == "subagent-b/0001"
    assert data["decision"] == "granted"
    assert data["nonce"] == "nonce-0001"
    assert data["note_refs"] == ["05_Knowledge/concepts/grant-note.md"]
    assert set(data["signatures"]) == {"user", "node"}
    node_pub = authz.public_key_from_b64(block["node_pubkey_b64"])
    assert authz.verify_signature(node_pub, block["signature"], prov.block_header_signable(block)), (
        "the block header signature must verify against the block's own node key"
    )


def test_write_child_session_attribution_is_finer_than_authority(ctx):
    """The ledger records WHO used the grant, not just who held it (DoD 5)."""
    path, outcome, block = _write(ctx, target="05_Knowledge/concepts/delegated.md", session="child-session-77")
    assert outcome == "granted"
    assert block["data"]["session"] == "child-session-77"
    assert block["data"]["parent_grant_id"] == "GRANT-0000"
    assert block["data"]["grant_id"] == "GRANT-0001"


def test_write_out_of_scope_is_quarantined_under_raw(ctx):
    target = "00_System/out-of-scope.md"
    path, outcome, block = _write(ctx, target=target)

    assert outcome == "quarantined"
    assert not (ctx["vault"] / target).exists(), "an out-of-scope write must never land at its target"
    assert path == ctx["vault"] / "03_Sources" / "grant-quarantine" / "GRANT-0001" / "out-of-scope.md"
    assert path.is_file()
    state, detail = prov.verify_note(path, vault=ctx["vault"])
    assert state == "valid", detail
    assert block["data"]["decision"] == "quarantined"
    # The `_raw/` shim still names the same file that landed under 03_Sources.
    assert (ctx["vault"] / "_raw" / "grant-quarantine" / "GRANT-0001" / "out-of-scope.md").is_file()
    assert prov.verify_approval_blocks(ctx["vault"]) == []


def test_write_refuses_forged_grant_before_touching_the_fs(ctx):
    forged = _grant(ctx)
    sig = forged["signature_b64"]
    forged["signature_b64"] = ("A" if sig[0] != "A" else "B") + sig[1:]
    with pytest.raises(prov.ProvenanceError, match="grant refused"):
        _write(ctx, target="05_Knowledge/concepts/hot.md", grant=forged)
    assert not (ctx["vault"] / "05_Knowledge" / "concepts" / "hot.md").exists()
    blocks = json.loads((ctx["vault"] / "blockchain" / "ledger.json").read_text())["blocks"]
    assert not any(b["data"].get("event") == "grant_use" for b in blocks)


def test_replayed_grant_is_spent_and_refused(ctx):
    _write(ctx, target="05_Knowledge/concepts/grant-note.md")
    with pytest.raises(prov.ProvenanceError, match="grant refused"):
        _write(ctx, target="05_Knowledge/concepts/second.md", grant=_grant(ctx))
    assert not (ctx["vault"] / "05_Knowledge" / "concepts" / "second.md").exists()
    blocks = json.loads((ctx["vault"] / "blockchain" / "ledger.json").read_text())["blocks"]
    uses = [b for b in blocks if b["data"].get("event") == "grant_use"]
    assert len(uses) == 1


def test_write_refuses_overwrite_of_existing_note(ctx):
    _write(ctx, target="05_Knowledge/concepts/grant-note.md")
    with pytest.raises(prov.ProvenanceError, match="already exists"):
        _write(ctx, target="05_Knowledge/concepts/grant-note.md", grant=_grant(ctx, nonce="fresh-nonce"))
    assert prov.verify_approval_blocks(ctx["vault"]) == []


def test_write_chained_ledger_stays_intact_across_uses(ctx):
    _write(ctx, target="05_Knowledge/concepts/a.md", session="subagent-a/1")
    _write(ctx, target="05_Knowledge/concepts/b.md", session="subagent-b/2", grant=_grant(ctx, nonce="nonce-0002"))
    ledger = json.loads((ctx["vault"] / "blockchain" / "ledger.json").read_text())
    assert prov.verify_chain(ledger)[0]


# --- the offline CLI (no Master running) --------------------------------------


def test_cli_grant_mint_and_verify_offline(ctx, tmp_path: Path):
    key_file = tmp_path / "master.key"
    seed = ctx["master_key"].private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    key_file.write_text(base64.b64encode(seed).decode() + "\n")

    grant_out = tmp_path / "grant.json"
    rc = authz._main(
        [
            "grant-mint",
            "--node-identity",
            str(ctx["vault"] / "manifests" / "node-identity.json"),
            "--master-key",
            str(key_file),
            "--grant-id",
            "GRANT-CLI-1",
            "--user-identity",
            "U-20161",
            "--conversation-key",
            "ck-1",
            "--capabilities",
            "p2p-sync",
            "--vault-scope",
            "05_Knowledge",
            "--ttl-minutes",
            "30",
            "--nonce",
            "nonce-cli-1",
            "--registry",
            str(ctx["vault"] / "manifests" / "node-authorizations.json"),
            "--out",
            str(grant_out),
        ]
    )
    assert rc == 0
    assert json.loads(grant_out.read_text())["grant_id"] == "GRANT-CLI-1"

    rc = authz._main(
        [
            "grant-verify",
            "--grant",
            str(grant_out),
            "--registry",
            str(ctx["vault"] / "manifests" / "node-authorizations.json"),
            "--node-identity",
            str(ctx["vault"] / "manifests" / "node-identity.json"),
        ]
    )
    assert rc == 0


def test_cli_grant_verify_derives_master_key_from_registry(ctx, tmp_path: Path):
    grant_out = tmp_path / "grant.json"
    grant_out.write_text(json.dumps(_grant(ctx, now=None), indent=2) + "\n")
    rc = authz._main(
        [
            "grant-verify",
            "--grant",
            str(grant_out),
            "--registry",
            str(ctx["vault"] / "manifests" / "node-authorizations.json"),
        ]
    )
    assert rc == 0


def test_cli_grant_write_offline(ctx, tmp_path: Path, capsys: pytest.CaptureFixture):
    grant_out = tmp_path / "grant.json"
    grant_out.write_text(json.dumps(_grant(ctx, now=None), indent=2) + "\n")
    body = tmp_path / "body.md"
    body.write_text("# CLI note\n\nwritten through the offline grant path.\n")

    rc = prov.main(
        [
            "--vault",
            str(ctx["vault"]),
            "grant-write",
            "--grant",
            str(grant_out),
            "--target",
            "05_Knowledge/concepts/cli-note.md",
            "--body-file",
            str(body),
            "--session",
            "cli-session-1",
        ]
    )
    assert rc == 0
    assert "granted" in capsys.readouterr().out
    out = ctx["vault"] / "05_Knowledge" / "concepts" / "cli-note.md"
    assert out.is_file()
    state, detail = prov.verify_note(out, vault=ctx["vault"])
    assert state == "valid", detail
