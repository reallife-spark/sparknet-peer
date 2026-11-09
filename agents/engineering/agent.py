#!/usr/bin/env python3
"""Engineering sub-agent: consumes sparknet.task.engineering events."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
from sparknet_loader import load_shared  # noqa: E402 - shared/ is added by path above

AGENT = load_shared("sparknet-agent-bus.py", "sparknet_engineering_agent")


def _token() -> str:
    token = os.environ.get("SPARKNET_AGENT_TOKEN", "")
    if token:
        return token
    path = os.environ.get("SPARKNET_AGENT_TOKEN_FILE", "")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def handle(payload, event):
    prompt = str(payload.get("prompt") or payload.get("instruction") or "").strip()
    if not prompt:
        raise AGENT.AgentBusError("bad-task", "engineering task needs a prompt or instruction")
    content = AGENT.call_model(
        prompt=prompt,
        url=os.environ.get("SPARKNET_AGENT_MODEL_URL", "http://127.0.0.1:4204/v1"),
        model=os.environ.get("SPARKNET_AGENT_MODEL", "sparknet-chat"),
        token=_token(),
        user=os.environ.get("SPARKNET_AGENT_USER", "agent-engineering"),
        timeout=float(os.environ.get("SPARKNET_AGENT_MODEL_TIMEOUT", "120")),
        max_tokens=int(os.environ.get("SPARKNET_AGENT_MAX_TOKENS", "512")),
    )
    return {"agent": "engineering", "task_event_id": event["event_id"], "summary": content}


def _policy():
    return AGENT.AgentPolicy.from_descriptor(
        {
            "schema_version": "sparknet-agent/1",
            "agent_id": "engineering",
            "role": "engineering",
            "enabled": True,
            "topics": [os.environ.get("SPARKNET_AGENT_SUBJECT", "sparknet.task.engineering")],
            "model_scope": os.environ.get("SPARKNET_AGENT_MODEL", "sparknet-chat"),
            "max_runtime_seconds": int(os.environ.get("SPARKNET_AGENT_RUNTIME", "300")),
            "max_concurrency": int(os.environ.get("SPARKNET_AGENT_CONCURRENCY", "2")),
            "max_classification": os.environ.get("SPARKNET_AGENT_CLASSIFICATION", "internal"),
            "departments": [],
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sparknet-engineering-agent")
    parser.add_argument("--nats-url", default=os.environ.get("SPARKNET_NATS_URL", "nats://127.0.0.1:4222"))
    parser.add_argument("--state-dir", default=os.environ.get("SPARKNET_STATE_DIR", "/srv/sparknet/peer-node/.state"))
    parser.add_argument("--node-id", default=os.environ.get("SPARKNET_NODE_ID", "sparknet-node-0002"))
    parser.add_argument("--subject", default=os.environ.get("SPARKNET_AGENT_SUBJECT", "sparknet.task.engineering"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)
    policy = _policy()
    address = AGENT.BUS.BusAddress.parse(args.nats_url)
    bus = AGENT.BUS.NatsClient(address.host, address.port)
    runner = AGENT.AgentRunner(policy, handle, bus, node_id=args.node_id, state_dir=Path(args.state_dir))
    try:
        bus.connect()
        if args.once or not args.serve:
            print(json.dumps(runner.run_once(args.subject), indent=2, sort_keys=True))
            return 0
        for event in bus.subscribe(args.subject):
            try:
                print(json.dumps(runner.handle(event), sort_keys=True), flush=True)
            except AGENT.AgentBusError as exc:
                print(f"engineering-agent: {exc.code}: {exc}", file=sys.stderr, flush=True)
    except (AGENT.AgentBusError, AGENT.NatsError) as exc:
        print(f"engineering-agent: {exc}", file=sys.stderr)
        return 2
    finally:
        bus.close()


if __name__ == "__main__":
    raise SystemExit(main())
