#!/usr/bin/env python3
"""Tiny Unix-socket TCP bridge for one OpenCode sandbox.

The sandbox has no published host port. OpenCode listens on loopback inside the
container; this bridge exposes it on a Unix socket in the mounted runtime state
directory, and the engine connects to that socket from the host. A socket is
simpler and narrower than a host port: only the engine that owns the state
directory can connect.
"""

from __future__ import annotations

import os
import socket
import sys
import threading


def pump(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(65536)
            if not chunk:
                break
            target.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client: socket.socket, host: str, port: int) -> None:
    try:
        upstream = socket.create_connection((host, port), timeout=30)
    except OSError:
        try:
            client.close()
        except OSError:
            pass
        return
    try:
        left = threading.Thread(target=pump, args=(client, upstream), daemon=True)
        left.start()
        pump(upstream, client)
        left.join(timeout=5)
    finally:
        for sock in (client, upstream):
            try:
                sock.close()
            except OSError:
                pass


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: unix-proxy.py <socket> <host> <port>", file=sys.stderr)
        return 2
    path, host, port_text = argv[1], argv[2], argv[3]
    port = int(port_text)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"cannot remove stale socket: {exc}", file=sys.stderr)
        return 1
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o600)
    server.listen(32)
    while True:
        client, _ = server.accept()
        threading.Thread(target=handle, args=(client, host, port), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
