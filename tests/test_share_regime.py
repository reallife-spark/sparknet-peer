"""The clocked, contract-driven sharing regime (peer PLAN Step 8).

The engine ``services/sparknet-share-regime.py`` implements the cross-node share
conversation from ``contracts/share-protocol.md`` by reading the *machine*
contracts (capabilities/overlay/ports) rather than restating them. The tests
drive two or more ``Regime`` instances as an in-process node pair over the
engine's own ``dispatch`` surface (the exact shapes the spec-only ports.json
listeners 4201/4202/4203 would carry), with all four clocks injected in seconds
so the whole DoD runs instantly.

The host has one peer, no Master and no second node: cross-node behaviour is
proven here in-process; the deployed legs (stanza application, live listeners,
ratification service, evidence repo) are ``DoD: confirm: user`` and stay in the
Master-hosted footprints.

Each test names the Step 8 DoD item it proves.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

VAULT_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = VAULT_ROOT.parent / "contracts"

NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
M1 = 60
H1 = 3600

# Faster-than-contract clocks so the tests run in seconds, never minutes.
FAST = {
    "invite_acceptance": 30,
    "idle_close": 30,
    "ratification": 30,
    "share_absolute": 120,
}


def _load_engine() -> Any:
    path = VAULT_ROOT / "services" / "sparknet-share-regime.py"
    spec = importlib.util.spec_from_file_location("sparknet_share_regime_t", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


engine = _load_engine()


class Clock:
    """A mutable wall clock the engines share; ``advance`` jumps it."""

    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    def iso(self) -> str:
        return self.now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _count(count: list[int]) -> Callable[[], int]:
    return lambda: count[0]


def _pair(clock: Clock, *, count=None, durations: dict[str, int] | None = None, name="kbc-peer_pair"):
    """Two-node peer_pair: the DoD 5 topology."""
    nodes = count if count is not None else [2]
    kwargs = {"node_count": _count(nodes), "durations": durations or FAST}
    a = engine.Regime("alice", now=clock, **kwargs, contracts_dir=CONTRACTS)
    b = engine.Regime("bob", now=clock, **kwargs, contracts_dir=CONTRACTS)
    return a, b, nodes


def _share_roundtrip(a, b, clock, kind="kb-share", temporary=True, toward="bob"):
    """Offer from alice to bob, accepted by bob, reopened on alice."""
    offer = a.offer(kind, toward=toward, temporary=temporary)
    b.dispatch(offer)
    accept = b.accept(offer["share_id"], kind)
    acts = a.dispatch(accept)
    return offer["share_id"], acts


def _kill_master(clock: Clock, node_count: list[int]) -> dict:
    """master_governed (DoD 6) needs a third node. The only role with a 2 h
    clock is the Master; the test enacts that by sending the arbitration
    message from a third, non-member Regime."""
    nodes = node_count if node_count is not None else [3]
    kwargs = {"node_count": _count(nodes), "durations": {**FAST, "idle_close": 300}}
    a = engine.Regime("alice", now=clock, **kwargs, contracts_dir=CONTRACTS)
    b = engine.Regime("bob", now=clock, **kwargs, contracts_dir=CONTRACTS)
    m = engine.Regime("master", now=clock, **kwargs, contracts_dir=CONTRACTS)
    return {"alice": a, "bob": b, "master": m, "nodes": nodes}


# --- DoD 1: invite open 1 h; acceptance requires choosing, may narrow ---------


def test_offer_opens_a_one_hour_invite_window() -> None:
    clock = Clock()
    a = engine.Regime("alice", now=clock, node_count=_count([1]), contracts_dir=CONTRACTS)
    offer = a.offer("kb-view", toward="bob")
    share = a.get(offer["share_id"])
    assert share["state"] == "invite"
    created = datetime.fromisoformat(share["created_at"].replace("Z", "+00:00"))
    accept_deadline = datetime.fromisoformat(share["accept_deadline"].replace("Z", "+00:00"))
    assert accept_deadline - created == timedelta(hours=1)


def test_acceptance_must_choose_a_kind_and_may_narrow_but_never_widen() -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    offer = a.offer("full", toward="bob")  # full's includes cover the four lesser
    b.dispatch(offer)
    wider = b.accept(offer["share_id"], "full")  # equal is allowed
    assert wider["chosen_kind"] == "full"
    a.dispatch(wider)
    assert a.get(offer["share_id"])["accepted_kind"] == "full"

    narrowed = a.offer("full", toward="bob")
    b.dispatch(narrowed)
    accepted = b.accept(narrowed["share_id"], "session-view")  # full includes session-view
    assert accepted["chosen_kind"] == "session-view"

    cant_widen = a.offer("session-view", toward="bob")
    b.dispatch(cant_widen)
    with pytest.raises(engine.ShareError) as exc:
        b.accept(cant_widen["share_id"], "full")  # wider than offered — refused
    assert exc.value.status == 400 and exc.value.reason == "cannot-widen"


def test_invite_expires_without_acceptance(durations=None) -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    offer = a.offer("kb-view", toward="bob")
    b.dispatch(offer)
    clock.advance(31)  # FAST invite window is 30 s
    acts = a.tick()
    assert a.get(offer["share_id"])["state"] == "expired"
    assert any(x["action"] == "offer-void" for x in acts)
    with pytest.raises(engine.ShareError) as exc:
        b.accept(offer["share_id"], "kb-view")
    assert exc.value.status == 410 and exc.value.reason == "offer-expired"


# --- DoD 2: the five share types ---------------------------------------------


def test_all_five_share_types_are_there_and_offerable() -> None:
    clock = Clock()
    a = engine.Regime("alice", now=clock, node_count=_count([1]), contracts_dir=CONTRACTS)
    kinds = ("kb-view", "kb-share", "session-view", "session-handoff", "full")
    assert set(a.share_types) == set(kinds)
    for kind in kinds:
        offer = a.offer(kind, toward="bob")
        assert offer["kind"] in kinds and a.get(offer["share_id"])["state"] == "invite"


def test_share_type_metadata_comes_from_the_contract() -> None:
    clock = Clock()
    a = engine.Regime("alice", now=clock, node_count=_count([1]), contracts_dir=CONTRACTS)
    html = json.loads((CONTRACTS / "capabilities.json").read_text())
    types = html["share_types"]["types"]
    for kind, meta in types.items():
        assert a.share_types[kind]["copies"] == meta["copies"]
        assert a.share_types[kind]["sensitive"] == meta["sensitive"]


# --- DoD 3: temporary vs permanent on idle; the 20 m idle close ---------------


def test_temporary_share_closes_after_idle(durations=None) -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-view", temporary=True)
    clock.advance(31)  # FAST idle window 30 s
    acts = a.tick()
    assert a.get(share_id)["state"] == "lapsed"
    assert any(x["action"] == "teardown-stanza" and x["reason"] == "idle-20m" for x in acts)


def test_permanent_share_survives_idle_but_the_absolute_cap_still_ends_it() -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-share", temporary=False)
    clock.advance(90)  # far past the 30 s idle window, still inside the 120 s cap
    assert not a.tick()
    assert a.get(share_id)["state"] == "open"
    clock.advance(60)  # past share_absolute
    acts = a.tick()
    assert a.get(share_id)["state"] == "closed"
    assert any(x["action"] == "teardown-stanza" and x["reason"] == "absolute-10h" for x in acts)


def test_activity_restarts_the_idle_window() -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="session-view", temporary=True)
    clock.advance(25)
    b.use(share_id)  # 5 s before the first idle deadline, activity resets it
    clock.advance(300)  # 180 s after reset, well past the original deadline
    assert b.get(share_id)["state"] == "open"


# --- DoD 4: all four clocks are clock-injected, so tests run in seconds -------


def test_contract_defaults_are_the_clock_norm() -> None:
    clock = Clock()
    a = engine.Regime("alice", now=clock, node_count=_count([1]), contracts_dir=CONTRACTS)
    assert a.durations == {
        "invite_acceptance": H1,
        "idle_close": 1200,
        "ratification": 2 * H1,
        "share_absolute": 10 * H1,
    }


def test_injected_durations_drive_the_lifecycle_in_seconds() -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-share")
    assert a.get(share_id)["state"] == "open"
    assert b.get(share_id)["state"] == "open"
    clock.advance(FAST["idle_close"] + 1)
    assert a.tick()


def test_idle_deadline_reflects_the_injected_clock() -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-view", temporary=True)
    share = a.get(share_id)
    opened = datetime.fromisoformat(share["event_log"][-1]["at"].replace("Z", "+00:00"))
    idle = datetime.fromisoformat(share["idle_deadline"].replace("Z", "+00:00"))
    assert idle - opened == timedelta(seconds=30)


# --- DoD 5: peer_pair — symmetric, equal privileges; additions+conflicts ------


def test_peer_pair_is_symmetric_and_never_arbitrates() -> None:
    clock = Clock()
    a, b, nodes = _pair(clock)
    share_id, acts = _share_roundtrip(a, b, clock, kind="kb-share")
    for node in (a, b):
        share = node.get(share_id)
        assert share["mode"] == "peer_pair"
        assert share["ratification_deadline"] is None, "peer_pair has NO ratification clock (contract peer_pair_note)"
        for endpoint in share["endpoints"].values():
            assert endpoint["trust"] == "flagged"  # nothing arbitrates in a pair
    assert any(x["action"] == "stanza-up" for x in acts)
    with pytest.raises(engine.ShareError) as exc:
        b.dispatch(engine._Message.ratify(share_id, True, "master", clock.iso()))
    assert exc.value.status == 409 and exc.value.reason == "no-arbiter"


def test_reconcile_is_additions_and_conflicts_only_and_recorded_both_ways() -> None:
    clock = Clock()
    incoming = {"kb/alpha.md": "sha-A", "kb/conflict.md": "sha-C"}
    bob_index = {"kb/alpha.md": "sha-A", "kb/conflict.md": "sha-B"}  # one identical, one divergent
    a = engine.Regime(
        "alice",
        now=clock,
        node_count=_count([2]),
        durations=FAST,
        contracts_dir=CONTRACTS,
    )
    b = engine.Regime(
        "bob",
        now=clock,
        node_count=_count([2]),
        durations=FAST,
        contracts_dir=CONTRACTS,
        vault_index=lambda: dict(bob_index),
    )
    offer = a.offer("kb-share", toward="bob")
    b.dispatch(offer)
    accepted = b.accept(offer["share_id"], "kb-share")
    a.dispatch(accepted)

    msg = b.reconcile(offer["share_id"], incoming)
    assert msg["additions"] == []
    assert msg["identical"] == ["kb/alpha.md"]
    assert msg["conflicts"] == ["kb/conflict.md"]
    b_record = b.get(offer["share_id"])["reconcile"]["received"]
    assert b_record["conflicts"] == ["kb/conflict.md"], "a divergence is a review item, never silently overwritten"

    a.dispatch(msg)  # sender records its own direction of the same evidence
    a_record = a.get(offer["share_id"])["reconcile"]["sent"]
    assert a_record["conflicts"] == ["kb/conflict.md"] and a_record["identical"] == ["kb/alpha.md"]


def test_reconcile_is_refused_for_non_copying_kinds() -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="session-view")  # copies=False
    with pytest.raises(engine.ShareError) as exc:
        b.reconcile(share_id, {"x.md": "sha"})
    assert exc.value.status == 409 and exc.value.reason == "not-kb-share"


# --- DoD 6: master_governed — downgrade to flagged, ratify to authenticated ---


def test_master_governed_share_requires_ratification_and_clears_to_authenticated() -> None:
    clock = Clock()
    nodes = _kill_master(clock, [3])
    a, b = nodes["alice"], nodes["bob"]
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-share")
    for node in (a, b):
        share = node.get(share_id)
        assert share["mode"] == "master_governed"
        assert share["ratification_deadline"] is not None
        for endpoint in share["endpoints"].values():
            assert endpoint["trust"] == "flagged", "arrival under a 3-node mesh is flagged until arbitration"
    ratify = nodes["master"].ratify(share_id, True, "master", clock.iso())
    a.dispatch(ratify)
    b.dispatch(ratify)
    for node in (a, b):
        share = node.get(share_id)
        for endpoint in share["endpoints"].values():
            assert endpoint["trust"] == "authenticated"
            assert endpoint["retained_flags"] == ["flagged"]


def test_a_third_node_joining_mid_share_re_evaluates_instead_of_grandfathering() -> None:
    clock = Clock()
    a, b, nodes = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-view")  # accepted as peer_pair
    assert a.get(share_id)["mode"] == "peer_pair"
    nodes[0] = 3  # a third node joins the registry
    acts = a.tick()
    assert a.get(share_id)["state"] == "re_evaluated"
    assert any(x["action"] == "re-evaluate" for x in acts)

    offer = a.offer("kb-view", toward="bob")  # re-offer under master_governed
    b.dispatch(offer)
    accepted = b.accept(offer["share_id"], "kb-view")
    a.dispatch(accepted)
    assert b.get(offer["share_id"])["mode"] == "master_governed"


# --- DoD 7: the time-boxed stanza; teardown; one-address authority invariant --


def test_stanza_is_time_boxed_and_keeps_the_one_address_authority_invariant() -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-view", temporary=True)
    stanza = a.render_stanza(share_id)
    assert stanza["interface"] == f"spk-share-{share_id}"
    assert stanza["wireguard"]["mtu"] == 1400
    assert stanza["wireguard"]["persistent_keepalive"] == 25
    assert stanza["wireguard"]["config_mode"] == "0600"
    assert stanza["grants_no_authority"] is True
    assert stanza["authority_plane_invariant"]["allowed_ips"] == "10.100.0.1/32"
    # kb-view rides 4201 share-intake; the wire names the service, not a number the engine invented
    assert stanza["transport"]["port"] == 4201
    assert stanza["transport"]["service"] == "share-intake"
    # time-boxed until the earlier of idle and absolute — never a permanent mapping
    timeout = datetime.fromisoformat(stanza["time_boxed_until"].replace("Z", "+00:00"))
    assert timeout - clock.now < timedelta(seconds=120)
    assert "nft" in stanza["nftables_exception"]


def test_teardown_fires_on_close_idle_and_absolute() -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-view", temporary=True)
    msg = a.close(share_id, reason="explicit")
    assert msg["msg_type"] == "share.close"  # the wire message; the host acts on outstanding_actions
    actions = a.get(share_id)["outstanding_actions"]
    assert any(x["action"] == "teardown-stanza" and x["reason"] == "explicit" for x in actions)
    with pytest.raises(engine.ShareError) as exc:
        a.close(share_id)
    assert exc.value.status == 409 and exc.value.reason == "already-closed"


def _peer_pair_roundtrip_with_idle_dose(clock):
    a, b, _ = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-view", temporary=True)
    return a, b, share_id


def test_peer_dispatch_of_close_tears_down_everywhere() -> None:
    clock = Clock()
    a, b, share_id = _peer_pair_roundtrip_with_idle_dose(clock)
    msg = a.close(share_id)
    b.dispatch(msg)
    assert b.get(share_id)["state"] == "closed"
    assert any(x["action"] == "teardown-stanza" for x in b.get(share_id)["outstanding_actions"])


# --- DoD 8: non-ratification severs the fabric data plane; ends at 10 h -------


def test_silence_at_the_ratification_deadline_is_a_refusal_and_isolates() -> None:
    clock = Clock()
    nodes = _kill_master(clock, [3])
    a, b = nodes["alice"], nodes["bob"]
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-share")  # wait 2 h for a Master answer
    clock.advance(FAST["ratification"] + 1)
    acts = b.tick()
    assert b.get(share_id)["state"] == "refused"
    isolates = [x for x in acts if x["action"] == "isolate-data-plane"]
    assert isolates and isolates[0]["authority_plane_allowed_ips"] == "10.100.0.1/32"
    assert any(x["action"] == "teardown-stanza" for x in acts), "the data-plane stanza is gone with it"
    # the pair keeps its authority channel: the refusal is severing fabric data, not the peer-to-peer identity


def test_a_late_master_answer_is_a_refusal_not_a_ratification() -> None:
    clock = Clock()
    nodes = _kill_master(clock, [3])
    a = nodes["alice"]
    share_id, _ = _share_roundtrip(nodes["alice"], nodes["bob"], clock, kind="kb-share")
    clock.advance(FAST["ratification"] + 1)  # the Master's 2 h window lapses silently
    a.dispatch(nodes["master"].ratify(share_id, True, "master", clock.iso()))  # its late "yes" ...
    assert a.get(share_id)["state"] == "refused", "... is a refusal (silence is an answer)"


def test_share_ends_at_the_absolute_deadline_even_ratified_and_permanent() -> None:
    clock = Clock()
    a, b, _ = _pair(clock)
    share_id, _ = _share_roundtrip(a, b, clock, kind="kb-share", temporary=False)
    clock.advance(FAST["share_absolute"] + 1)
    acts = a.tick()
    assert a.get(share_id)["state"] == "closed"
    assert any(x["action"] == "teardown-stanza" and x["reason"] == "absolute-10h" for x in acts)


# --- the honest single-node host: drafts allowed, acceptance refused ----------


def test_solo_node_can_draft_but_never_accept() -> None:
    clock = Clock()
    a = engine.Regime("alice", now=clock, node_count=_count([1]), contracts_dir=CONTRACTS)
    offer = a.offer("kb-view", toward="bob")
    assert a.get(offer["share_id"])["state"] == "invite"  # a draft is a draft until a peer accepts
    with pytest.raises(engine.ShareError) as exc:
        a.accept(offer["share_id"], "kb-view")
    assert exc.value.status == 409 and exc.value.reason == "solo"


def test_regime_refuses_to_run_without_the_contracts() -> None:
    clock = Clock()
    with pytest.raises(FileNotFoundError):
        engine.Regime("alice", now=clock, node_count=_count([1]), contracts_dir=Path("/nonexistent"))


# --- the manifest that declares this step's surface ---------------------------


def test_the_share_regime_manifest_matches_the_engine() -> None:
    manifest = json.loads((VAULT_ROOT / "manifests" / "share-regime.json").read_text())
    assert manifest["schema"] == "sparknet-share-regime/1"
    assert set(manifest["share_types"]) == {"kb-view", "kb-share", "session-view", "session-handoff", "full"}
    assert set(manifest["clocks"]) == {"invite_acceptance", "idle_close", "ratification", "share_absolute"}
    assert manifest["authority_boundary"]["grants_no_authority"] is True
    assert "10.100.0.1/32" in manifest["authority_boundary"]["authority_plane_invariant"]
    assert manifest["status"] == "wired, not deployed"


def test_the_manifest_and_engine_agree_on_the_contract_types() -> None:
    clock = Clock()
    regime = engine.Regime("alice", now=clock, node_count=_count([1]), contracts_dir=CONTRACTS)
    manifest = json.loads((VAULT_ROOT / "manifests" / "share-regime.json").read_text())
    assert set(regime.share_types) == set(manifest["share_types"])
    assert set(regime.durations) == set(manifest["clocks"])