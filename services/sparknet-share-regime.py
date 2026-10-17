#!/usr/bin/env python3
"""Sparknet share regime — the clocked, contract-driven sharing conversation
(peer PLAN Step 8).

Implements the cross-node share lifecycle of ``contracts/share-protocol.md``,
driven by the *machine-readable* contracts rather than restating them: share
types and the four clocks from ``contracts/capabilities.json``, governance
modes and the data-plane stanza from ``contracts/overlay.json``, and the share
transport ports from ``contracts/ports.json``. A share type or clock that the
contract files rename or remove changes this engine's behaviour without a code
change — and an engine that cannot find the contracts refuses to run rather
than guessing.

The engine is a pure, in-process, message-dispatched state machine. Two or
more ``Regime`` instances model distinct nodes; tests drive them as an
in-process pair by passing ``dispatch`` messages between the two, which is
exactly the shape the wire messages would have over the (spec-only, port 4201 /
4202 / 4203) listeners. It opens no sockets, loads no keys, and never fakes
applying WireGuard or nftables — those remain ``DoD: confirm: user`` target-node
legs. The engine's job is to be *right about the rules*; the host's job is to
obey the actions it returns (``teardown-stanza``, ``isolate-data-plane``,
``re-evaluate``).

The same ports also carry the **proposal exchange** (``contracts/proposal-schema.json``
``transport``): a sibling message family (``proposal.submit`` / ``proposal.review``
/ ``proposal.decide``) that is *not* a share type. The engine recognises the
family — ``accepts_message`` and ``proposal_route`` — and reads the family and
its ports from the proposal contract, so a missing contract refuses construction
rather than letting the route be guessed.

A share is not an authority transfer (share-protocol.md "What a share is").
Authority comes from the Master's Ed25519 key and the signed grant envelope,
never from being talked to; this module holds no key material at all.
"""

from __future__ import annotations

import json
import os

try:  # Python 3.9+
    from collections.abc import Callable
except ImportError:  # pragma: no cover - Python 3.8
    from typing import Callable  # noqa: UP035

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SHARE_SCHEMA = "sparknet-share/1"


class ShareError(Exception):
    """A regime rule refusal. ``status`` mirrors the bridge's HTTP vocabulary
    so the same rule reads the same on both sides of a wire."""

    def __init__(self, status: int, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.status = status
        self.reason = reason
        self.detail = detail


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    dt = dt if dt is not None else _utcnow()
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_duration(value: str) -> int:
    """``1h`` / ``20m`` / ``45s`` → seconds, the units capabilities.json's
    clocks use."""
    text = value.strip().lower()
    if text.endswith("h"):
        return int(text[:-1]) * 3600
    if text.endswith("m"):
        return int(text[:-1]) * 60
    if text.endswith("s"):
        return int(text[:-1])
    raise ValueError(f"cannot parse duration {value!r}")


def _contracts_dir() -> Path:
    env = os.environ.get("SPARKNET_CONTRACTS_DIR", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent / "contracts",
        here.parent.parent / "contracts",
    ]
    for candidate in candidates:
        if (candidate / "capabilities.json").is_file():
            return candidate
    raise FileNotFoundError(
        "cannot locate contracts/ (capabilities.json, overlay.json, ports.json); "
        "the share regime refuses to reimplement the contract from prose"
    )


def _load(name: str, contracts: Path) -> dict:
    path = contracts / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"contract file missing: {path}")
    return json.loads(path.read_text())


class _Message:
    """Wire-message builders. The engine serializes its own messages so the
    in-process pair and a future wire both carry the same shapes."""

    @staticmethod
    def offer(share: dict) -> dict:
        return {
            "msg_type": "share.offer",
            "share_id": share["share_id"],
            "kind": share["kind"],
            "offered_by": share["offered_by"],
            "toward": share["toward"],
            "temporary": share["temporary"],
            "scope": share["scope"],
            "created_at": share["created_at"],
            "accept_deadline": share["accept_deadline"],
        }

    @staticmethod
    def accept(share_id: str, chosen_kind: str, accepted_by: str, at: str) -> dict:
        return {
            "msg_type": "share.accept",
            "share_id": share_id,
            "chosen_kind": chosen_kind,
            "accepted_by": accepted_by,
            "at": at,
        }

    @staticmethod
    def ratify(share_id: str, decision: bool, by: str, at: str) -> dict:
        return {
            "msg_type": "share.ratify",
            "share_id": share_id,
            "decision": bool(decision),
            "by": by,
            "at": at,
        }

    @staticmethod
    def use(share_id: str, at: str) -> dict:
        return {"msg_type": "share.use", "share_id": share_id, "at": at}

    @staticmethod
    def reconcile(
        share_id: str,
        direction: str,
        additions: list[str],
        conflicts: list[str],
        identical: list[str],
    ) -> dict:
        return {
            "msg_type": "share.reconcile",
            "share_id": share_id,
            "direction": direction,
            "additions": additions,
            "conflicts": conflicts,
            "identical": identical,
        }

    @staticmethod
    def close(share_id: str, reason: str, by: str, at: str) -> dict:
        return {
            "msg_type": "share.close",
            "share_id": share_id,
            "reason": reason,
            "by": by,
            "at": at,
        }


class Regime:
    """One node's view of every share it participates in.

    Each engine holds its own store; nothing in this module reaches into a
    second engine's store. Two engines talk by moving ``dispatch`` messages,
    mirroring the spec-only transport (ports.json 4201 share-intake + 4202
    session-transfer for peer messages, 4203 share-ratification for the
    Master's decision).
    """

    def __init__(
        self,
        node_id: str,
        *,
        now: Callable[[], datetime] | None = None,
        node_count: Callable[[], int] | None = None,
        contracts_dir: Path | None = None,
        durations: dict[str, int] | None = None,
        vault_index: Callable[[], dict[str, str]] | None = None,
    ):
        if not node_id:
            raise ValueError("node_id is required")
        contracts = contracts_dir or _contracts_dir()
        caps = _load("capabilities", contracts)
        overlay = _load("overlay", contracts)
        ports = _load("ports", contracts)
        # The proposal exchange rides these ports; loading the contract here means
        # a missing proposal-schema.json refuses construction instead of letting
        # the route be invented (contracts/proposal-schema.json `transport`).
        proposal_schema = _load("proposal-schema", contracts)
        try:
            self.share_types: dict[str, dict] = caps["share_types"]["types"]
            clocks: dict[str, dict] = caps["share_clocks"]["clocks"]
        except (KeyError, TypeError) as exc:
            raise ShareError(500, "contract-malformed", str(exc))
        try:
            transport = proposal_schema["transport"]
            self.proposal_message_types: tuple[str, ...] = tuple(transport["message_types"])
            self.proposal_ports: tuple[int, ...] = tuple(int(p) for p in transport["ports"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ShareError(500, "contract-malformed", f"proposal-schema transport: {exc}")
        if not self.proposal_message_types or not self.proposal_ports:
            raise ShareError(500, "contract-malformed", "proposal transport names no route")
        self.durations = durations or {name: _parse_duration(info["duration"]) for name, info in clocks.items()}
        self.governance_modes: dict[str, dict] = overlay["governance_mode"]["modes"]
        self.master_anchor: str = overlay["master_anchor_ipv4"]
        self.allowed_ips_authority: str = f"{overlay['master_anchor_ipv4']}/32"
        self.allowed_ips_rule: str = overlay["peer_allowed_ips_rule"]
        self._mtu: int = overlay.get("mtu", 1400)
        self._keepalive: int = overlay.get("persistent_keepalive", 25)
        self._config_mode: str = overlay.get("config_mode", "0600")
        self.port_services: dict[str, dict] = {
            str(port): meta
            for port, meta in (ports.get("ports") or {}).items()
            if isinstance(meta, dict) and int(port) in (4201, 4202, 4203)
        }

        self.node_id = node_id
        self._now = now or _utcnow
        self._node_count = node_count if node_count is not None else (lambda: 1)
        self._vault_index = vault_index or (lambda: {})
        self.shares: dict[str, dict] = {}
        self._seq = 0

    # -- model helpers ----------------------------------------------------------

    def _iso(self) -> str:
        return _iso(self._now())

    def _seconds(self, clock: str) -> int:
        return int(self.durations[clock])

    def _deadline(self, clock: str) -> str:
        return _iso(self._now() + timedelta(seconds=self._seconds(clock)))

    def mode_for(self) -> str:
        """Governance is computed from the registry node count, never config."""
        count = int(self._node_count())
        if count <= 1:
            return "solo"
        if count == 2:
            return "peer_pair"
        return "master_governed"

    def _port_for_kind(self, kind: str) -> int:
        if kind in ("kb-view", "kb-share"):
            return 4201
        if kind in ("session-view", "session-handoff", "full"):
            return 4202
        raise ShareError(400, "unknown-kind", kind)

    def accepts_message(self, msg_type: str) -> bool:
        """Whether this node's share stanza knows the message family.

        Two families share the ports: ``share.*`` (the proposal is unrelated to
        the share state machine) and the proposal exchange ``proposal.*`` read
        from ``contracts/proposal-schema.json``. A message in neither family is
        not something this engine routes.
        """
        if isinstance(msg_type, str) and msg_type.startswith("share."):
            return True
        return msg_type in self.proposal_message_types

    def proposal_route(self, msg_type: str) -> int:
        """The port a proposal-exchange message travels on (proposal-schema.json).

        A proposal is not a share *type*, so it is not routed by ``kind``; it has
        its own message family and the ports the contract names for it. An
        unknown ``msg_type`` is refused rather than defaulted to a port.
        """
        if msg_type not in self.proposal_message_types:
            raise ShareError(400, "unknown-message", f"{msg_type!r} is not a proposal exchange message")
        return self.proposal_ports[0]

    def _narrowing(self, offered: str, chosen: str) -> bool:
        """A receiver may always accept a narrower type, never a wider one
        (capabilities.json ``share_types.narrowing_only``). Only ``full``
        carries an explicit ``includes`` set, so the partial order is: equal,
        or chosen is an explicitly-included lesser type of the offer."""
        if offered == chosen:
            return True
        offered_meta = self.share_types.get(offered)
        return bool(offered_meta and "includes" in offered_meta and chosen in offered_meta["includes"])

    def _get(self, share_id: str) -> dict:
        share = self.shares.get(share_id)
        if share is None:
            raise ShareError(404, "unknown-share", f"no share {share_id!r}")
        return share

    def restore(self, record: dict) -> None:
        """Re-adopt a persisted share record (session files survive a bridge
        restart; the engine's in-memory store does not)."""
        if not isinstance(record, dict) or not record.get("share_id"):
            raise ShareError(500, "corrupt-share", "a persisted share record carries no share_id")
        self.shares[record["share_id"]] = record
        try:
            self._seq = max(self._seq, int(record["share_id"].rsplit("-", 1)[-1]))
        except (ValueError, AttributeError):
            pass

    def _append_event(self, share: dict, event: dict) -> None:
        share.setdefault("event_log", []).append(event)

    def _transition(self, share: dict, state: str, at: str, note: str | None = None) -> None:
        share["state"] = state
        if state == "closed":
            share["closed_at"] = at
        self._append_event(share, {"event": f"→{state}", "at": at, "note": note})

    def _apply_open(
        self,
        share: dict,
        chosen_kind: str,
        accepted_by: str,
        at: str,
    ) -> None:
        now = _parse_iso(at)
        mode = self.mode_for()
        share["mode"] = mode
        share["accepted_kind"] = chosen_kind
        share["state"] = "open"
        share["last_use"] = at
        if share["temporary"]:
            share["idle_deadline"] = _iso(now + timedelta(seconds=self._seconds("idle_close")))
        else:
            share["idle_deadline"] = None
        share["ratification_deadline"] = (
            _iso(now + timedelta(seconds=self._seconds("ratification"))) if mode == "master_governed" else None
        )
        share["absolute_deadline"] = _iso(now + timedelta(seconds=self._seconds("share_absolute")))
        # ACCEPT — both nodes are flagged; only arbitration clears it, leaving
        # the flag retained (capabilities.json trust_levels.retained_after_ratification).
        share["endpoints"] = {
            share["offered_by"]: {"trust": "flagged", "retained_flags": []},
            accepted_by: {"trust": "flagged", "retained_flags": []},
        }
        self._append_event(
            share,
            {"event": "accept", "kind": chosen_kind, "accepted_by": accepted_by, "by": accepted_by, "at": at},
        )

    # -- offer side -------------------------------------------------------------

    def offer(
        self,
        kind: str,
        *,
        toward: str | None = None,
        temporary: bool = True,
        scope: list[str] | None = None,
        offered_by: str | None = None,
    ) -> dict:
        """Draft an offer (state ``invite``, a 1 h accept window). On a solo
        node (no second node in the registry) the draft need not name a
        counterpart yet — there is nobody to accept while the node is alone."""
        if kind not in self.share_types:
            raise ShareError(400, "unknown-kind", f"{kind} is not a share type in capabilities.json")
        if self.mode_for() != "solo" and not toward:
            raise ShareError(400, "malformed", "a share must name its counterpart")
        self._seq += 1
        share_id = f"share-{self._seq}"
        now = self._iso()
        share: dict[str, Any] = {
            "schema": SHARE_SCHEMA,
            "share_id": share_id,
            "kind": kind,
            "accepted_kind": None,
            "offered_by": offered_by or self.node_id,
            "toward": toward,
            "temporary": bool(temporary),
            "scope": scope if scope is not None else ["."],
            "mode": self.mode_for(),
            "mode_at_offer": self.mode_for(),
            "state": "invite",
            "created_at": now,
            "accept_deadline": self._deadline("invite_acceptance"),
            "absolute_deadline": None,
            "idle_deadline": None,
            "ratification_deadline": None,
            "last_use": None,
            "endpoints": {},
            "reconcile": {},
            "event_log": [
                {
                    "event": "offer",
                    "by": offered_by or self.node_id,
                    "kind": kind,
                    "temporary": bool(temporary),
                    "at": now,
                }
            ],
            "outstanding_actions": [],
        }
        self.shares[share_id] = share
        return _Message.offer(share)

    # -- receiving side ---------------------------------------------------------

    def _mirror_offer(self, message: dict) -> list[dict]:
        kind = message.get("kind")
        if kind not in self.share_types:
            raise ShareError(400, "unknown-kind", f"{kind} is not a share type in capabilities.json")
        if self.shares.get(message["share_id"]):
            raise ShareError(409, "already-known", f"share {message['share_id']} already reached this node")
        share: dict[str, Any] = {
            "schema": SHARE_SCHEMA,
            "share_id": message["share_id"],
            "kind": message["kind"],
            "accepted_kind": None,
            "offered_by": message["offered_by"],
            "toward": message["toward"],
            "temporary": bool(message.get("temporary", True)),
            "scope": message.get("scope") or ["."],
            "mode": self.mode_for(),
            "mode_at_offer": self.mode_for(),
            "state": "invite",
            "created_at": message["created_at"],
            "accept_deadline": message["accept_deadline"],
            "absolute_deadline": None,
            "idle_deadline": None,
            "ratification_deadline": None,
            "last_use": None,
            "endpoints": {},
            "reconcile": {},
            "event_log": [
                {"event": "offer-received", "kind": message["kind"], "by": message["offered_by"], "at": self._iso()}
            ],
            "outstanding_actions": [],
        }
        self.shares[message["share_id"]] = share
        return []

    def accept(self, share_id: str, chosen_kind: str) -> dict:
        """The recipient chooses a type (narrowing allowed) and accepts. Returns
        the ``share.accept`` message the sender must ``dispatch``."""
        share = self._get(share_id)
        if share["state"] != "invite":
            raise ShareError(409, "not-invite", f"share {share_id} is {share['state']}")
        if _parse_iso(self._iso()) > _parse_iso(share["accept_deadline"]):
            self._transition(share, "expired", self._iso(), "accept-deadline")
            raise ShareError(410, "offer-expired", "the 1 h invite window closed; the offer never happened")
        if not self._narrowing(share["kind"], chosen_kind):
            raise ShareError(
                400,
                "cannot-widen",
                f"the receiver may only narrow {share['kind']}; {chosen_kind} is not a narrower type",
            )
        mode = self.mode_for()
        if mode == "solo":
            raise ShareError(409, "solo", "a single-node mesh has no cross-node share")
        if mode != share["mode_at_offer"]:
            self._re_evaluate(share, mode)
            raise ShareError(
                409,
                "re-evaluated",
                f"mesh mode changed to {mode} before acceptance; re-offer under the new mode",
            )
        self._apply_open(share, chosen_kind, accepted_by=self.node_id, at=self._iso())
        return _Message.accept(share_id, chosen_kind, self.node_id, self._iso())

    def _recv_accept(self, message: dict) -> list[dict]:
        share = self._get(message["share_id"])
        if share["offered_by"] != self.node_id:
            raise ShareError(403, "not-offerer", f"share {message['share_id']} was not offered by this node")
        if share["state"] != "invite":
            raise ShareError(409, "not-invite", f"share {message['share_id']} is {share['state']}")
        if not self._narrowing(share["kind"], message["chosen_kind"]):
            raise ShareError(
                400,
                "cannot-widen",
                f"the receiver may only narrow {share['kind']}; {message['chosen_kind']} is not narrower",
            )
        now = _parse_iso(message["at"])
        if now > _parse_iso(share["accept_deadline"]):
            self._transition(share, "expired", message["at"], "accept-deadline")
            raise ShareError(410, "offer-expired", "the accept message arrived after the 1 h window")
        mode = self.mode_for()
        if mode == "solo":
            raise ShareError(409, "solo", "a single-node mesh has no cross-node share")
        if mode != share["mode_at_offer"]:
            self._re_evaluate(share, mode)
            raise ShareError(409, "re-evaluated", "mesh mode changed; the offer must be re-made")
        self._apply_open(share, message["chosen_kind"], accepted_by=message["accepted_by"], at=message["at"])
        return [
            self._action(
                "stanza-up",
                share,
                transport=self._port_for_kind(share["accepted_kind"]),
                until=share["absolute_deadline"],
            )
        ]

    def use(self, share_id: str) -> dict:
        """The recipient pings activity; a temporary share's idle window restarts."""
        share = self._get(share_id)
        if share["state"] != "open":
            raise ShareError(409, "not-open", f"share {share_id} is {share['state']}")
        at = self._iso()
        share["last_use"] = at
        if share["temporary"]:
            share["idle_deadline"] = _iso(_parse_iso(at) + timedelta(seconds=self._seconds("idle_close")))
        self._append_event(share, {"event": "use", "at": at, "by": self.node_id})
        return _Message.use(share_id, at)

    def reconcile(
        self,
        share_id: str,
        incoming: dict[str, str],
    ) -> dict:
        """kb-share arrival: merge by additions and conflicts, never a silent
        overwrite (share-protocol.md "Reconciliation"). Both directions are
        recorded — the returned message makes the sender's mirror do so."""
        share = self._get(share_id)
        kind = share.get("accepted_kind")
        if kind not in ("kb-share", "full"):
            raise ShareError(409, "not-kb-share", f"{kind} conveys no copy into the receiver's vault")
        if share["state"] != "open":
            raise ShareError(409, "not-open", f"share {share_id} is {share['state']}")
        existing = self._vault_index()
        additions: list[str] = []
        conflicts: list[str] = []
        identical: list[str] = []
        for path, sha in incoming.items():
            if path not in existing:
                additions.append(path)
            elif existing[path] == sha:
                identical.append(path)
            else:
                conflicts.append(path)  # held for a human; never overwritten
        summary = {"additions": additions, "conflicts": conflicts, "identical": identical, "at": self._iso()}
        share.setdefault("reconcile", {})
        share["reconcile"]["received"] = summary
        self._append_event(
            share,
            {"event": "reconcile-received", "additions": len(additions), "conflicts": len(conflicts), **summary},
        )
        return _Message.reconcile(share_id, "receive", additions, conflicts, identical)

    def _recv_reconcile(self, message: dict) -> list[dict]:
        share = self._get(message["share_id"])
        summary = {
            "additions": message.get("additions", []),
            "conflicts": message.get("conflicts", []),
            "identical": message.get("identical", []),
            "at": self._iso(),
        }
        share.setdefault("reconcile", {})
        share["reconcile"]["sent"] = summary
        self._append_event(
            share,
            {"event": "reconcile-sent", "direction": message.get("direction"), **summary},
        )
        return []

    def close(self, share_id: str, reason: str = "explicit") -> dict:
        share = self._get(share_id)
        if share["state"] == "closed":
            raise ShareError(409, "already-closed", f"share {share_id} is already closed")
        at = self._iso()
        self._transition(share, "closed", at, reason)
        self._action("teardown-stanza", share, reason=reason)
        return _Message.close(share_id, reason, self.node_id, at)

    def _recv_close(self, message: dict) -> list[dict]:
        share = self._get(message["share_id"])
        if share["state"] != "closed":
            self._transition(share, "closed", message["at"], message.get("reason") or "peer-closed")
            self._action("teardown-stanza", share, reason=message.get("reason") or "peer-closed")
        return []

    # -- arbitration (master_governed: port 4203) -------------------------------

    def ratify(self, share_id: str, decision: bool, by: str, at: str | None = None) -> dict:
        """Build the arbiter's ratification message. The 2 h clock is the
        Master's to decide within: a message that arrives after the window is
        treated as a refusal by the receiving peers (silence is an answer)."""
        returned = _Message.ratify(share_id, decision, by, at or self._iso())
        return returned

    def _recv_ratify(self, message: dict) -> list[dict]:
        share_id = message["share_id"]
        share = self._get(share_id)
        at = message.get("at") or self._iso()
        if share["state"] != "open":
            raise ShareError(409, "not-open", f"share {share_id} is {share['state']}")
        decision = bool(message.get("decision"))
        ratification_deadline = share.get("ratification_deadline")
        if ratification_deadline and _parse_iso(at) > _parse_iso(ratification_deadline):
            decision = False  # silence is an answer; a late answer is a refusal
        if decision:
            ratio = share.get("mode") == "master_governed"
            if not ratio:
                raise ShareError(409, "no-arbiter", "peer_pair has no ratification step and none is required")
            for endpoint in share["endpoints"].values():
                endpoint["trust"] = "authenticated"
                endpoint.setdefault("retained_flags", [])
                if "flagged" not in endpoint["retained_flags"]:
                    endpoint["retained_flags"].append("flagged")
            self._append_event(share, {"event": "ratify", "by": message.get("by"), "at": at})
            return []
        self._transition(share, "refused", at, "non-ratification")
        self._action(
            "isolate-data-plane",
            share,
            reason="non-ratification",
            note="fabric data plane severed; the authority-and-evidence channel to the Master stays open",
            authority_plane_allowed_ips=self.allowed_ips_authority,
        )
        self._action("teardown-stanza", share, reason="refused")
        return []

    # -- the four clocks --------------------------------------------------------

    def tick(self) -> list[dict]:
        """Evaluate every deadline against the injected clock. Returns the
        actions a host must perform (stanza teardown, data-plane isolation,
        share re-evaluation); the unreachable-Master rule is enforced here as
        refusal, so a share can expire but never hang (share-protocol.md)."""
        actions: list[dict] = []
        now = self._iso()
        for share_id, share in list(self.shares.items()):
            state = share["state"]
            if state == "invite" and _parse_iso(now) > _parse_iso(share["accept_deadline"]):
                self._transition(share, "expired", now, "accept-deadline")
                actions.append(self._action("offer-void", share, reason="invite-window"))
                continue
            current_mode = self.mode_for()
            if state in ("open", "invite") and current_mode != share.get("mode_at_offer"):
                self._re_evaluate(share, current_mode)
                actions.append(
                    self._action(
                        "re-evaluate",
                        share,
                        reason=f"mode-{share.get('mode_at_offer')}→{current_mode}",
                    )
                )
                continue
            if state != "open":
                continue
            if share.get("absolute_deadline") and _parse_iso(now) >= _parse_iso(share["absolute_deadline"]):
                self._transition(share, "closed", now, "absolute-10h")
                actions.append(self._action("teardown-stanza", share, reason="absolute-10h"))
                continue
            if (
                share["temporary"]
                and share.get("idle_deadline")
                and _parse_iso(now) >= _parse_iso(share["idle_deadline"])
            ):
                self._transition(share, "lapsed", now, "idle-20m")
                actions.append(self._action("teardown-stanza", share, reason="idle-20m"))
                continue
            if share.get("ratification_deadline") and _parse_iso(now) > _parse_iso(share["ratification_deadline"]):
                # 2 h silence is a refusal; the unreachable Master expires the
                # share and severs the fabric data plane, peer-to-peer excluded.
                self._transition(share, "refused", now, "ratification-silence")
                actions.append(
                    self._action(
                        "isolate-data-plane",
                        share,
                        reason="ratification-silence",
                        authority_plane_allowed_ips=self.allowed_ips_authority,
                    )
                )
                actions.append(self._action("teardown-stanza", share, reason="refused"))
                continue
        return actions

    def _re_evaluate(self, share: dict, current_mode: str) -> None:
        if share["state"] == "invite":
            self._transition(share, "re_evaluated", self._iso(), f"mode-{share.get('mode_at_offer')}→{current_mode}")
        else:
            self._transition(share, "re_evaluated", self._iso(), f"mode-{current_mode}")

    def _action(self, action: str, share: dict, **extra: Any) -> dict:
        recorded = {"action": action, "share_id": share["share_id"], **extra}
        share.setdefault("outstanding_actions", []).append(recorded)
        return recorded

    def dispatch(self, message: dict) -> list[dict]:
        """Ingest one wire message from a sibling engine (or the arbiter)."""
        mtype = message.get("msg_type")
        handler = {
            "share.offer": self._mirror_offer,
            "share.accept": self._recv_accept,
            "share.ratify": self._recv_ratify,
            "share.use": self._recv_use,
            "share.reconcile": self._recv_reconcile,
            "share.close": self._recv_close,
        }.get(mtype)
        if handler is None:
            raise ShareError(400, "unknown-message", f"no handler for {mtype!r}")
        return handler(message)

    def _recv_use(self, message: dict) -> list[dict]:
        share_id = message["share_id"]
        share = self._get(share_id)
        if share["state"] != "open":
            raise ShareError(409, "not-open", f"share {share_id} is {share['state']}")
        share["last_use"] = message.get("at") or self._iso()
        if share["temporary"]:
            at = _parse_iso(share["last_use"])
            share["idle_deadline"] = _iso(at + timedelta(seconds=self._seconds("idle_close")))
        self._append_event(share, {"event": "use", "by": message.get("by"), "at": share["last_use"]})
        return []

    def get(self, share_id: str) -> dict:
        return self._get(share_id)

    def render_stanza(self, share_id: str) -> dict:
        """The time-boxed data-plane stanza plus its nftables exception,
        rendered (never applied). The authority plane's one-address
        ``AllowedIPs`` invariant is returned alongside, unchanged in every
        governance mode (overlay.json)."""
        share = self._get(share_id)
        if share["state"] != "open":
            raise ShareError(409, "not-open", f"share {share_id} is {share['state']}")
        expiry = share["absolute_deadline"]
        if share["temporary"] and share.get("idle_deadline"):
            expiry = min(_parse_iso(expiry), _parse_iso(share["idle_deadline"])).isoformat().replace("+00:00", "Z")
        port = self._port_for_kind(share["accepted_kind"])
        port_service = self.port_services.get(str(port), {})
        interface = f"spk-share-{share_id}"
        nftables = "nft add rule inet sparknet forward iifname {interface} ip daddr {toward}".format(
            interface=interface, toward=share["toward"]
        )
        return {
            "share_id": share_id,
            "mode": share["mode"],
            "interface": interface,
            "time_boxed_until": expiry,
            "transport": {
                "port": port,
                "service": port_service.get("service", ""),
                "overlay_only": bool(port_service.get("overlay_only", False)),
            },
            "trust_basis": (
                "a signed, unexpired, unrevoked share grant naming the "
                "counterpart's fingerprint — never the fact that a packet "
                "arrived from an overlay address"
            ),
            "grants_no_authority": True,
            "wireguard": {"mtu": self._mtu, "persistent_keepalive": self._keepalive, "config_mode": self._config_mode},
            "nftables_exception": nftables,
            "authority_plane_invariant": {
                "allowed_ips": self.allowed_ips_authority,
                "rule": self.allowed_ips_rule,
            },
        }
