#!/usr/bin/env python3
"""Operator client for the local share control socket (0600, Unix only)."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def _default_socket() -> Path:
    explicit = os.environ.get("SPARKNET_SHARE_CONTROL", "").strip()
    if explicit:
        return Path(explicit)
    base = Path(os.environ.get("XDG_STATE_HOME", "").strip() or Path.home() / ".local" / "state")
    return base / "sparknet" / "share-serve" / "control.sock"


def call(socket_path: Path, payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(30)
        client.connect(str(socket_path))
        client.sendall(json.dumps(payload, sort_keys=True).encode("utf-8") + b"\n")
        data = b""
        while b"\n" not in data and len(data) < 1_048_576:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
    try:
        return json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SystemExit(f"control socket returned invalid JSON: {exc}") from exc


def _files(args: argparse.Namespace) -> dict[str, str]:
    files: dict[str, str] = {}
    for path_text in args.file:
        path = Path(path_text)
        relative = args.as_path or path.name
        files[relative] = path.read_text(encoding="utf-8")
    if args.directory:
        root = Path(args.directory)
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(root).as_posix()
                files[relative] = path.read_text(encoding="utf-8")
                if len(files) >= 200:
                    break
    if not files:
        raise SystemExit("payload needs --file or --directory")
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=_default_socket())
    parser.add_argument("action", choices=("state", "offer", "accept", "use", "close", "tick", "payload"))
    parser.add_argument("--share-id")
    parser.add_argument("--kind")
    parser.add_argument("--toward")
    parser.add_argument("--scope", action="append", default=None)
    parser.add_argument("--temporary", dest="temporary", action="store_true", default=True)
    parser.add_argument("--no-temporary", dest="temporary", action="store_false")
    parser.add_argument("--reason", default="explicit")
    parser.add_argument("--send-to", help="remote listener URL")
    parser.add_argument("--file", action="append", default=[], help="text file to include")
    parser.add_argument("--as", dest="as_path", help="relative path for a single --file")
    parser.add_argument("--directory", help="stage every regular text file under this directory")
    args = parser.parse_args(argv)

    action = args.action
    if action == "state":
        request = {"action": "state"}
    elif action == "offer":
        request = {
            "action": "offer",
            "kind": args.kind,
            "toward": args.toward,
            "temporary": args.temporary,
            "scope": args.scope,
        }
    elif action == "accept":
        request = {"action": "accept", "share_id": args.share_id, "kind": args.kind}
    elif action == "use":
        request = {"action": "use", "share_id": args.share_id}
    elif action == "close":
        request = {"action": "close", "share_id": args.share_id, "reason": args.reason}
    elif action == "tick":
        request = {"action": "tick"}
    else:
        if args.send_to:
            files = _files(args)
            try:
                response = call(
                    args.socket,
                    {"action": "send_payload", "url": args.send_to, "share_id": args.share_id, "files": files},
                )
            except OSError as exc:
                print(json.dumps({"ok": False, "reason": "socket-error", "detail": str(exc)}), file=sys.stderr)
                return 2
            print(json.dumps(response, sort_keys=True, indent=2))
            return 0 if response.get("ok") else 2
        request = {"action": "payload", "share_id": args.share_id, "files": _files(args)}

    try:
        response = call(args.socket, request)
    except OSError as exc:
        print(json.dumps({"ok": False, "reason": "socket-error", "detail": str(exc)}), file=sys.stderr)
        return 2
    if not response.get("ok"):
        print(json.dumps(response, sort_keys=True, indent=2), file=sys.stderr)
        return 2
    if action == "payload":
        print(json.dumps(response, sort_keys=True, indent=2))
        return 0 if response.get("ok") else 2
    if args.send_to and action in ("offer", "accept", "close", "use"):
        message = response.get("message")
        if not isinstance(message, dict):
            print(json.dumps({"ok": False, "reason": "no-message"}), file=sys.stderr)
            return 2
        response = call(args.socket, {"action": "send", "url": args.send_to, "message": message})
    print(json.dumps(response, sort_keys=True, indent=2))
    return 0 if response.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
