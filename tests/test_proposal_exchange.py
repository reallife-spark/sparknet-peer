"""The proposal exchange over the share ports (fabric PLAN Step 36).

`peer-node/services/sparknet-proposal-exchange.py` carries a proposal round trip
between two nodes as a bundle over the share ports 4201/4202
(`contracts/proposal-schema.json` ``transport``), verifying with the shared
ladder — `shared/sparknet-proposal.py` `verify_proposal_chain` — before it signs
anything. These tests drive two in-process ``Exchange`` instances as a node pair
and prove the round trip, the authorship split and every refusal.

Everything is offline: each vault is generated per test with its own key, so a
passing run proves nothing about any real node's keys and cannot leak one. The
live cross-node leg over the overlay is `DoD: confirm: user`.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

VAULT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = VAULT_ROOT.parent
CONTRACTS = REPO_ROOT / "contracts"
EXCHANGE_PATH = VAULT_ROOT / "services" / "sparknet-proposal-exchange.py"
PROPOSAL_PATH = REPO_ROOT / "shared" / "sparknet-proposal.py"
PROVENANCE_PATH = REPO_ROOT / "shared" / "sparknet-provenance.py"
TRANSPORT = json.loads((CONTRACTS / "proposal-schema.json").read_text())["transport"]

A_NODE = "sparknet-node-0009"
B_NODE = "sparknet-node-0010"
A_OWNER = "U-10421"
B_OWNER = "U-33007"

CONVERSATION = [
    {"id": "conv-1", "turn": 1, "agent": "plan", "model": "m-plan", "at": "2026-10-13T00:00:00Z"},
    {"id": "conv-1", "turn": 2, "agent": "build", "model": "m-build", "at": "2026-10-13T00:01:00Z"},
]


def _load(name: str, path: Path, *, keep: bool) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if not keep:
            sys.modules.pop(name, None)
    return module


@pytest.fixture(scope="module")
def provenance() -> ModuleType:
    return _load("sparknet_provenance_exchange_test", PROVENANCE_PATH, keep=False)


@pytest.fixture(scope="module")
def exchange() -> ModuleType:
    return _load("sparknet_exchange_under_test", EXCHANGE_PATH, keep=True)


@pytest.fixture(scope="module")
def proposal() -> ModuleType:
    return _load("sparknet_proposal_under_test_exchange", PROPOSAL_PATH, keep=False)


def user_record_template() -> str:
    return (
        "---\n"
        "type: user-record\n"
        "schema: sparknet-user/1\n"
        "status: unfilled\n"
        'owner_id: ""\n'
        'owner_node: ""\n'
        "role: owner\n"
        "immutable: true\n"
        "authorised_senders: []\n"
        "key:\n"
        "  alg: ed25519\n"
        '  public_key_b64: ""\n'
        '  fingerprint: ""\n'
        '  key_ref: ""\n'
        '  created: ""\n'
        "---\n"
    )


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _make_vault(root: Path, provenance: ModuleType, node_id: str, owner_id: str) -> Path:
    (root / "00_System").mkdir(parents=True)
    (root / "blockchain").mkdir()
    (root / "manifests").mkdir()
    write(root / "00_System" / "user.md", user_record_template())
    write(root / "manifests" / "node-identity.json", json.dumps({"node_id": node_id}))
    provenance.user_init(root, owner_id=owner_id)
    authz = provenance._load_authz()
    private_b64, _ = authz.generate_keypair()
    node_key = root / ".state" / "keys" / "ed25519-node.key.b64"
    node_key.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    node_key.write_text(private_b64 + "\n")
    node_key.chmod(0o600)
    return root


@pytest.fixture()
def pair(tmp_path: Path, provenance: ModuleType, exchange: ModuleType, monkeypatch: pytest.MonkeyPatch):
    """Two independent nodes: A proposes, B reviews, A decides.

    ``SPARKNET_STATE_DIR`` is unset so each vault keeps its key under its own
    ``<vault>/.state`` — the two nodes have separate user and node keys.
    """
    monkeypatch.delenv("SPARKNET_STATE_DIR", raising=False)
    monkeypatch.setenv("SPARKNET_CONTRACTS_DIR", str(CONTRACTS))
    a = _make_vault(tmp_path / "a", provenance, A_NODE, A_OWNER)
    b = _make_vault(tmp_path / "b", provenance, B_NODE, B_OWNER)
    return exchange.Exchange(a, contracts_dir=CONTRACTS), exchange.Exchange(b, contracts_dir=CONTRACTS)


def _round_trip(a, b) -> tuple[dict, dict, dict]:
    m1 = a.submit(
        title="Adopt the proposal contract",
        summary="Introduce proposal/review/decision records over the share ports.",
        agent="plan",
        model="m-plan",
        conversation=CONVERSATION,
    )
    m2 = b.review(m1, verdict="approve", reason="reads well", agent="vault", model="m-vault")
    m3 = a.decide(m2, decision="approved", reason="ship it", agent="plan", model="m-plan")
    return m1, m2, m3


# --- the round trip -----------------------------------------------------------


def test_the_round_trip_crosses_two_nodes(pair):
    a, b = pair
    m1, m2, m3 = _round_trip(a, b)
    pid = m1["records"][0]["proposal_id"]

    assert [r["event"] for r in m1["records"]] == ["proposal"]
    assert [r["event"] for r in m2["records"]] == ["proposal", "review"]
    assert [r["event"] for r in m3["records"]] == ["proposal", "review", "decision"]
    assert m3["msg_type"] == "proposal.decide"
    assert pid.startswith("PRO-")

    # The shared ladder accepts the union bundle both ends built.
    assert a.verify(m3) == []
    assert b.verify(m2) == []
    assert b.verify(m3) == []

    # Each record attributes its own author's node and user.
    review = m2["records"][1]
    decision = m3["records"][2]
    assert review["owner_node"] == B_NODE and review["owner_id"] == B_OWNER
    assert decision["owner_node"] == A_NODE and decision["owner_id"] == A_OWNER


def test_each_node_records_only_its_own_records(pair, proposal: ModuleType, provenance: ModuleType):
    a, b = pair
    m1, m2, m3 = _round_trip(a, b)
    pid = m1["records"][0]["proposal_id"]

    a_events = [(r["event"], r["owner_node"]) for r in proposal.proposal_records(a.vault, pid)]
    b_events = [(r["event"], r["owner_node"]) for r in proposal.proposal_records(b.vault, pid)]
    assert a_events == [("proposal", A_NODE), ("decision", A_NODE)]
    assert b_events == [("review", B_NODE)]
    assert proposal.proposal_state(a.vault, pid) == "decided"
    # A review with no proposal is invisible to the reviewer's own per-vault state.
    assert proposal.proposal_state(b.vault, pid) == "absent"

    # No node ever records a foreign author's block, so each ledger still verifies:
    # verify_approval_blocks checks each peer-event record's user signature against
    # *this* node's user key.
    for vault in (a.vault, b.vault):
        assert provenance.verify_approval_blocks(vault) == []
        ledger = provenance.load_ledger(vault / "blockchain" / "ledger.json")
        ok, detail = provenance.verify_chain(ledger)
        assert ok, detail


def test_the_route_comes_from_the_contract(pair):
    a, _ = pair
    ports = set(TRANSPORT["ports"])
    assert set(a.regime.proposal_message_types) == set(TRANSPORT["message_types"])
    for msg_type in a.regime.proposal_message_types:
        assert a.regime.proposal_route(msg_type) in ports


# --- refusals -----------------------------------------------------------------


def test_a_tampered_conversation_is_refused_before_signing(pair, proposal: ModuleType):
    a, b = pair
    m1 = a.submit(title="T", summary="S", agent="plan", model="m-plan", conversation=CONVERSATION)
    tampered = copy.deepcopy(m1)
    tampered["records"][0]["conversation"].append(
        {"id": "conv-1", "turn": 3, "agent": "evil", "model": "m", "at": "2026-10-13T01:00:00Z"}
    )
    before = proposal.proposal_records(b.vault)
    with pytest.raises(Exception) as exc:
        b.review(tampered, verdict="approve", reason="ok", agent="vault", model="m")
    assert "does not verify" in str(exc.value)
    assert proposal.proposal_records(b.vault) == before  # nothing was appended


def test_a_forged_signature_is_refused(pair):
    a, b = pair
    m1 = a.submit(title="T", summary="S", agent="plan", model="m-plan", conversation=CONVERSATION)
    forged = copy.deepcopy(m1)
    forged["records"][0]["signatures"]["node"] = "AAAA"
    with pytest.raises(Exception):
        b.review(forged, verdict="approve", reason="ok", agent="vault", model="m")


def test_a_reordered_bundle_is_refused(pair):
    a, b = pair
    m1, m2, _ = _round_trip(a, b)
    reordered = copy.deepcopy(m2)
    reordered["records"] = [m2["records"][1], m2["records"][0]]  # review before proposal
    assert b.verify(reordered) != []
    with pytest.raises(Exception):
        a.decide(reordered, decision="approved", reason="ok", agent="plan", model="m")


def test_a_decision_needs_a_review(pair, proposal: ModuleType):
    a, b = pair
    m1 = a.submit(title="T", summary="S", agent="plan", model="m-plan", conversation=CONVERSATION)
    before = proposal.proposal_records(a.vault)
    with pytest.raises(Exception) as exc:
        a.decide(m1, decision="approved", reason="rush", agent="plan", model="m")
    assert "no review yet" in str(exc.value)
    assert proposal.proposal_records(a.vault) == before


def test_a_second_decision_is_refused(pair, proposal: ModuleType):
    a, b = pair
    _, _, m3 = _round_trip(a, b)
    before = proposal.proposal_records(a.vault)
    with pytest.raises(Exception) as exc:
        a.decide(m3, decision="denied", reason="changed my mind", agent="plan", model="m")
    assert "already decided" in str(exc.value)
    assert proposal.proposal_records(a.vault) == before


def test_an_unknown_message_family_is_refused(pair):
    a, b = pair
    with pytest.raises(Exception):
        b.receive({"msg_type": "share.offer", "records": []})
    with pytest.raises(Exception):
        b.receive({"msg_type": "proposal.nonsense", "records": []})


# --- the regime's half --------------------------------------------------------


def test_the_regime_accepts_the_family_and_routes_it(exchange: ModuleType):
    regime = exchange._load_regime()
    node = regime.Regime("alice", contracts_dir=CONTRACTS)
    for msg_type in TRANSPORT["message_types"]:
        assert node.accepts_message(msg_type) is True
        assert node.proposal_route(msg_type) in set(TRANSPORT["ports"])
    assert node.accepts_message("share.offer") is True
    assert node.accepts_message("proposal.nonsense") is False
    assert node.accepts_message("nonsense") is False
    with pytest.raises(regime.ShareError):
        node.proposal_route("proposal.nonsense")


def test_the_regime_refuses_without_the_proposal_contract(tmp_path: Path, exchange: ModuleType):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    for name in ("capabilities.json", "overlay.json", "ports.json"):
        shutil.copy(CONTRACTS / name, contracts / name)
    # proposal-schema.json is deliberately absent: the route must not be invented.
    with pytest.raises(FileNotFoundError):
        exchange._load_regime().Regime("alice", contracts_dir=contracts)


# --- the contract pin and the CLI ---------------------------------------------


def test_the_contract_names_the_message_family(exchange: ModuleType):
    assert set(TRANSPORT["message_types"]) == set(exchange.PROPOSAL_MESSAGE_TYPES)
    assert set(TRANSPORT["ports"]) == {4201, 4202}


def test_the_cli_verifies_a_bundle(pair):
    a, b = pair
    _, _, m3 = _round_trip(a, b)
    bundle = a.vault.parent / "bundle.json"
    bundle.write_text(json.dumps(m3["records"]))
    assert a._proposal.verify_proposal_chain(json.loads(bundle.read_text())) == []

    import subprocess

    result = subprocess.run(
        [sys.executable, str(EXCHANGE_PATH), "verify", str(bundle)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout

    tampered = copy.deepcopy(m3["records"])
    tampered[-1]["decision"] = "denied"  # break the signature over the record
    bundle.write_text(json.dumps(tampered))
    result = subprocess.run(
        [sys.executable, str(EXCHANGE_PATH), "verify", str(bundle)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
