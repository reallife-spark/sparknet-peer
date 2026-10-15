#!/usr/bin/env python3
"""Sparknet proposal exchange — carry the proposal round trip over the share ports.

`contracts/proposal-schema.json` ``transport`` is the source of truth: the
proposal exchange rides the existing share ports 4201 (`share-intake`) / 4202
(`session-transfer`) as a message family (``proposal.submit`` / ``proposal.review``
/ ``proposal.decide``), not as a new port and not as a share type. This service
delivers a signed proposal and returns a signed review/decision, verifying every
bundle with the shared ladder — `shared/sparknet-proposal.py`
`verify_proposal_chain` — before it signs anything.

It is a pure, in-process endpoint, exactly like the share regime it rides: it
opens no socket and the wire message it produces is the shape a listener would
carry. Two ``Exchange`` instances model two nodes; the test drives a full round
trip between them. The live cross-node leg over the overlay is
``DoD: confirm: user`` (the development host has no wireguard interface).

The service adds no cryptography of its own: it loads
`shared/sparknet-proposal.py` by path (which in turn loads the one provenance,
authz and merkle implementations) and the sibling
`peer-node/services/sparknet-share-regime.py` for the contract-driven route.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_HERE = Path(__file__).resolve().parent

PROPOSAL_MESSAGE_TYPES = ("proposal.submit", "proposal.review", "proposal.decide")


class ExchangeError(Exception):
    """A refusal with a message the caller can act on."""


def _load_by_path(name: str, path: Path) -> Any:
    """Load a sibling/shared module by path — never by import.

    The fabric rule: one implementation, loaded by path. The proposal module and
    the share regime are both loaded this way so neither can drift into an
    installed copy.
    """
    if name in sys.modules:
        return sys.modules[name]
    if not path.is_file():
        raise ExchangeError(f"cannot find {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ExchangeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _shared_dir() -> Path:
    """Locate shared/, honouring an env override, then repo/bundle layout.

    The bundle reproduces the repository layout (``<root>/peer-node/`` +
    ``<root>/shared/``), so ``services/../../shared`` resolves in both a clone and
    an extracted node.
    """
    env = os.environ.get("SPARKNET_SHARED_DIR", "").strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for candidate in (here.parent.parent.parent / "shared", here.parent.parent / "shared"):
        if (candidate / "sparknet-proposal.py").is_file():
            return candidate
    return here.parent.parent.parent / "shared"


def _load_proposal() -> Any:
    return _load_by_path("sparknet_proposal_exchange_impl", _shared_dir() / "sparknet-proposal.py")


def _load_regime() -> Any:
    return _load_by_path("sparknet_proposal_exchange_regime", DEFAULT_HERE / "sparknet-share-regime.py")


class Exchange:
    """One node's proposal-exchange endpoint.

    ``submit`` builds and appends a proposal on this vault; ``review``/``decide``
    each verify a received bundle with the shared ladder and append only this
    node's own record. ``receive`` is the verify-only step both use.
    """

    def __init__(self, vault: Path, *, contracts_dir: Path | None = None):
        self.vault = Path(vault)
        self._proposal = _load_proposal()
        self._regime_module = _load_regime()
        provenance = self._proposal._load_provenance()
        self.node_id = provenance.node_id(self.vault)
        if contracts_dir is not None:
            self.regime = self._regime_module.Regime(self.node_id, contracts_dir=Path(contracts_dir))
        else:
            self.regime = self._regime_module.Regime(self.node_id)

    # -- routing -----------------------------------------------------------------

    def _message(self, msg_type: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "msg_type": msg_type,
            "port": self.regime.proposal_route(msg_type),
            "records": records,
        }

    def verify(self, message: dict[str, Any]) -> list[str]:
        """The shared ladder over a received message's records ([] means good)."""
        records = message.get("records")
        return self._proposal.verify_proposal_chain(records)

    def receive(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        """Verify a received bundle; return its records or refuse.

        This endpoint speaks only the proposal family: a ``share.*`` message is a
        different family on the same port and is not this service's to accept,
        even though the regime routes it.
        """
        msg_type = message.get("msg_type")
        if msg_type not in self.regime.proposal_message_types:
            raise ExchangeError(f"{msg_type!r} is not a proposal exchange message")
        problems = self.verify(message)
        if problems:
            raise ExchangeError("refusing a bundle that does not verify: " + "; ".join(problems))
        return [dict(record) for record in message["records"]]

    # -- the round trip ----------------------------------------------------------

    def submit(self, draft: dict[str, Any] | None = None, **build: Any) -> dict[str, Any]:
        """Submit a proposal (a draft, or built from keywords) and emit the message."""
        if draft is None:
            draft = self._proposal.build_proposal(self.vault, **build)
        record = self._proposal.submit_proposal(self.vault, draft)
        return self._message("proposal.submit", [record])

    def review(
        self,
        message: dict[str, Any],
        *,
        verdict: str,
        reason: str,
        agent: str,
        model: str,
        conversation: Any = (),
        clock: Any = None,
    ) -> dict[str, Any]:
        """Verify a received proposal/review bundle and return it with this review."""
        records = self.receive(message)
        review = self._proposal.review_bundle(
            self.vault,
            records,
            verdict=verdict,
            reason=reason,
            agent=agent,
            model=model,
            conversation=conversation,
            clock=clock,
        )
        return self._message("proposal.review", records + [review])

    def decide(
        self,
        message: dict[str, Any],
        *,
        decision: str,
        reason: str,
        agent: str,
        model: str,
        conversation: Any = (),
        clock: Any = None,
    ) -> dict[str, Any]:
        """Verify a received proposal/review bundle and return it with this decision."""
        records = self.receive(message)
        decision_record = self._proposal.decide_bundle(
            self.vault,
            records,
            decision=decision,
            reason=reason,
            agent=agent,
            model=model,
            conversation=conversation,
            clock=clock,
        )
        return self._message("proposal.decide", records + [decision_record])


# --- CLI ----------------------------------------------------------------------


def _resolve_vault(args: argparse.Namespace) -> Path:
    if args.vault:
        return Path(args.vault).resolve()
    env = os.environ.get("VAULT_PATH")
    if env:
        return Path(env).resolve()
    raise ExchangeError("cannot find a vault; pass --vault or set VAULT_PATH")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sparknet-proposal-exchange.py",
        description=(
            "Carry a proposal round trip over the share ports "
            "(contracts/proposal-schema.json transport). Verifies with the shared "
            "ladder before signing; opens no socket."
        ),
    )
    parser.add_argument("--vault", help="the vault root (default: VAULT_PATH)")
    sub = parser.add_subparsers(dest="command")

    verify = sub.add_parser("verify", help="verify a received message/bundle JSON")
    verify.add_argument("message", help="a JSON file with {records: [...]} or a bare record list")

    sub.add_parser("ports", help="print the proposal route ports from the contract")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    try:
        if args.command == "verify":
            payload = json.loads(Path(args.message).read_text())
            if isinstance(payload, dict):
                exchange = Exchange(_resolve_vault(args))
                problems = exchange.verify(payload)
            else:
                problems = _load_proposal().verify_proposal_chain(payload)
            if problems:
                for problem in problems:
                    sys.stdout.write(problem + "\n")
                return 1
            sys.stdout.write("proposal bundle ok\n")
            return 0

        if args.command == "ports":
            exchange = Exchange(_resolve_vault(args))
            for msg_type in exchange.regime.proposal_message_types:
                sys.stdout.write(f"{msg_type} {exchange.regime.proposal_route(msg_type)}\n")
            return 0
    except (ExchangeError, ValueError) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
