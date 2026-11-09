#!/usr/bin/env python3
"""The peer's liveness probe and claim — presence evidence, never authority.

Contract: ``contracts/health-protocol.md``; envelope:
``contracts/telemetry-schema.json``. The state machine is the one shared
implementation, ``shared/sparknet-liveness.py``; this service only supplies the
three things it deliberately does not: a real address probe, a listener that
lets a sibling register activity, and a periodic ``kind=liveness`` report.

Why a dedicated probe port
~~~~~~~~~~~~~~~~~~~~~~~~~~
The monitor must never make the serving path do work. It therefore probes
``4211`` (``contracts/ports.json``), a contracted, overlay-only, non-serving
port whose only listener is this service: it accepts a TCP connection and
closes it without reading or writing a byte. A guard that is busy answering a
share therefore cannot look dead, and a guard bug cannot look alive.

Attribution is the overlay
~~~~~~~~~~~~~~~~~~~~~~~~~~
There is no signature here and deliberately no liveness signing primitive. A
claim is a telemetry report whose ``peer_fingerprint`` names this peer; the
Master derives the same fingerprint from the WireGuard-authenticated source
address and refuses a mismatch. Nothing in the fabric binds a peer fingerprint
to a node signing key (Step 68A), so signing a claim would be unverifiable
theatre. ``nonce`` plus ``expires_at`` carry replay/freshness instead.

What is claimed
~~~~~~~~~~~~~~~
One report per push carries the monitor's view of every registry-derived peer:
``observer`` (this node_id), ``probe_port`` and ``claims`` from
``Monitor.claim()``. An evicted peer stays in ``claims`` as dead — eviction
removes it from this monitor's probe set only; it never edits the registry and
never touches ``revoked``.

Silence on an ungranted node
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Probing is local data-plane belief and runs regardless, so siblings can still
find this node. Pushing needs the peer-held ``telemetry`` capability, exactly
as 70A's pusher; without it the service probes, binds and reports nothing.

Usage::

    sparknet-liveness-monitor.py               # probe + claim, continuously
    sparknet-liveness-monitor.py --once        # one round and one claim
    sparknet-liveness-monitor.py --dry-run     # print the claim, bind nothing
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import signal
import socket
import socketserver
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
try:
    from sparknet_loader import load_shared  # noqa: E402 - shared/ is added by path above
except ImportError:  # a role copy without shared/ can still answer --help

    def load_shared(filename, modname, **_kwargs):
        raise FileNotFoundError(f"cannot load {filename}: shared/sparknet_loader.py is not present")


SERVICE_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT_ROOT = SERVICE_DIR.parent

#: The contracted non-serving probe port (contracts/ports.json). Overriding it is
#: for a loopback test or a same-host collision; it never moves a fabric edge.
DEFAULT_PORT = 4211
DEFAULT_INTERVAL = 10
DEFAULT_PUSH_EVERY = 6
DEFAULT_TIMEOUT = 10

#: Ports that carry a serving or authority path. The probe listener must never
#: be pointed at one: the point of 4211 is that watching cannot become working.
SERVING_PORTS = frozenset({4200, 4201, 4202, 4203, 4210, 51820, 8088, 8090, 9090})


def _shared_dirs() -> list[Path]:
    dirs = []
    env_shared = os.environ.get("SPARKNET_SHARED_DIR", "").strip()
    if env_shared:
        dirs.append(Path(env_shared))
    dirs.extend([DEFAULT_VAULT_ROOT.parent / "shared", SERVICE_DIR])
    return dirs


def _load_shared(filename: str, module_name: str) -> Any:
    return load_shared(filename, module_name, dirs=_shared_dirs(), missing_ok=True)


def _load_authz() -> Any:
    return _load_shared("sparknet-authz.py", "sparknet_liveness_authz")


def _load_monitor() -> Any:
    return _load_shared("sparknet-liveness.py", "sparknet_liveness")


def _load_pusher() -> Any:
    return load_shared("sparknet-telemetry-push.py", "sparknet_liveness_pusher", dirs=[SERVICE_DIR], missing_ok=True)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _address(value: Any) -> str | None:
    """A bare IP from ``10.100.0.7/32``; anything else is not a probe target."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().split("/", 1)[0]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def _parse_stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def load_vault(vault_root: Path) -> tuple[dict[str, Any], dict[str, Any], Any, Any] | None:
    """Read the identity, the Master-signed registry and the Master's key."""
    authz = _load_authz()
    monitor_module = _load_monitor()
    if authz is None or monitor_module is None:
        return None
    identity = _read_json(vault_root / "manifests" / "node-identity.json")
    registry = _read_json(vault_root / "manifests" / "node-authorizations.json")
    key_path = vault_root / "manifests" / "keys" / "master-ed25519.pub.b64"
    if not isinstance(identity, dict) or not isinstance(registry, dict) or not key_path.is_file():
        return None
    try:
        public_key = authz.load_public_key(key_path)
    except Exception:
        return None
    return identity, registry, public_key, authz


def peer_roster(
    identity: dict[str, Any],
    registry: dict[str, Any],
    public_key: Any,
    authz: Any,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Registry-derived peers, mapped to their probe address.

    Every entry is signature-checked, so an edited registry cannot add a probe
    target; revoked, expired, non-peer and self entries are skipped. Liveness is
    not a trust decision — a revoked node can still answer a probe — but a
    revoked node is no longer a sibling worth watching, and the roster follows
    the signed registry rather than inventing targets.
    """
    moment = now or datetime.now(timezone.utc)
    self_fingerprint = authz.fingerprint(identity)
    roster: dict[str, str] = {}
    entries = registry.get("authorizations")
    if not isinstance(entries, list):
        return roster
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("fingerprint") == self_fingerprint:
            continue
        peer_identity = entry.get("identity")
        if not isinstance(peer_identity, dict) or peer_identity.get("role") != "peer":
            continue
        if entry.get("revoked"):
            continue
        expires = _parse_stamp(entry.get("expires_at"))
        if expires is not None and expires <= moment:
            continue
        try:
            if not authz.verify_signature(
                public_key, str(entry.get("signature_b64", "")), authz.authorization_message(entry)
            ):
                continue
        except Exception:
            continue
        address = _address(entry.get("overlay_ipv4") or peer_identity.get("overlay_ipv4"))
        if address is None:
            continue
        name = str(peer_identity.get("node_id") or entry.get("fingerprint") or address)
        roster[name] = address
    return roster


def probe(address: str, port: int, timeout: float) -> bool:
    """Reach the peer's non-serving port. Success is connect+close, not a read."""
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


class _ProbeHandler(socketserver.BaseRequestHandler):
    """Accept, close, and tell the monitor who touched us. Never read a byte."""

    def handle(self) -> None:
        callback = getattr(self.server, "on_connection", None)
        with contextlib.suppress(OSError):
            self.request.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self.request.close()
        if callback is not None:
            callback(self.client_address[0])


class ProbeListener(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, bind: str, port: int, on_connection: Any) -> None:
        self.on_connection = on_connection
        super().__init__((bind, port), _ProbeHandler)


class LivenessService:
    def __init__(
        self,
        vault_root: Path,
        *,
        bind: str,
        port: int,
        endpoint: str | None,
        interval: int = DEFAULT_INTERVAL,
        push_every: int = DEFAULT_PUSH_EVERY,
        timeout: int = DEFAULT_TIMEOUT,
        dry_run: bool = False,
    ) -> None:
        material = load_vault(vault_root)
        if material is None:
            raise RuntimeError("no readable identity/registry/master key")
        self.identity, self.registry, self.public_key, self.authz = material
        self.monitor_module = _load_monitor()
        self.pusher = _load_pusher()
        self.vault_root = vault_root
        self.bind = bind
        self.port = port
        self.endpoint = endpoint
        self.interval = interval
        self.push_every = push_every
        self.timeout = timeout
        self.dry_run = dry_run
        self._stop = threading.Event()
        self._push_lock = threading.Lock()
        self._rounds = 0
        self._evicted: set[str] = set()
        self._addresses: dict[str, str] = {}
        self._known: list[str] = []
        self._registry_stamp: tuple[int, int] | None = None
        self._can_push = False
        self._push_refusal = ""

        self._monitor = self.monitor_module.ContinuousMonitor(
            {},
            resolve=lambda name: self._addresses.get(name),
            probe=lambda address: probe(address, self.port, self.timeout),
            probe_interval=self.interval,
            on_round=self._after_round,
        )
        self._listener: ProbeListener | None = None

        capabilities, _, refusal = self.pusher.granted_capabilities(vault_root)
        if refusal:
            self._push_refusal = refusal
        elif "telemetry" not in capabilities:
            self._push_refusal = "telemetry capability not granted"
        else:
            self._can_push = True
        self._push_enabled = self._can_push and not dry_run
        if self._push_enabled:
            resolved = self.pusher.resolve_endpoint(vault_root, endpoint)
            if not resolved.startswith(("http://", "https://")):
                raise RuntimeError(f"refusing non-HTTP endpoint: {resolved}")
            self._endpoint_url = resolved
        else:
            self._endpoint_url = ""

    # --- roster ---------------------------------------------------------------

    def _stamp(self) -> tuple[int, int] | None:
        try:
            stat = (self.vault_root / "manifests" / "node-authorizations.json").stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def refresh_roster(self, *, force: bool = False) -> None:
        stamp = self._stamp()
        if not force and stamp == self._registry_stamp:
            return
        material = load_vault(self.vault_root)
        if material is None:
            print("sparknet-liveness-monitor: registry unreadable; keeping the last roster", file=sys.stderr)
            return
        identity, registry, public_key, authz = material
        roster = peer_roster(identity, registry, public_key, authz)
        for name in list(self._monitor.peers):
            if name not in roster:
                del self._monitor.peers[name]
        for name in roster:
            if name not in self._monitor.peers:
                self._monitor.peers[name] = self.monitor_module.Peer(name)
        self._addresses = dict(roster)
        self._known = sorted(roster)
        # A registry publication is the re-admission event: an evicted peer is
        # local to this roster, and a fresh signed roster is a new observation.
        self._evicted.clear()
        self._registry_stamp = stamp

    def record_activity(self, source: str) -> None:
        address = _address(source)
        if address is None:
            return
        for name, peer_address in self._addresses.items():
            if peer_address == address:
                self._monitor.activity(name)

    # --- cycles ---------------------------------------------------------------

    def _evict(self) -> None:
        for name in self._monitor.eviction_candidates():
            if self._monitor.evict(name):
                self._evicted.add(name)
                print(
                    f"sparknet-liveness-monitor: evicted {name} from the local probe view (registry untouched)",
                    file=sys.stderr,
                )

    def build_report(self) -> dict[str, Any] | None:
        if not self._known:
            return None
        payload = {
            "observer": self.identity.get("node_id"),
            "probe_port": self.port,
            "claims": [self._monitor.claim(name) for name in self._known],
        }
        if self._evicted:
            payload["evicted"] = sorted(self._evicted)
        return self.authz.build_telemetry_report(
            kind="liveness",
            peer_fingerprint=self.authz.fingerprint(self.identity),
            payload=payload,
        )

    def push(self) -> None:
        report = self.build_report()
        if report is None:
            print("sparknet-liveness-monitor: no registry peers to claim", file=sys.stderr)
            return
        if self.dry_run:
            print(json.dumps(report, indent=2, sort_keys=True))
            return
        if not self._can_push:
            print(f"sparknet-liveness-monitor: not pushing: {self._push_refusal}", file=sys.stderr)
            return
        with self._push_lock:
            ok, detail = self.pusher.post_report(self._endpoint_url, report, self.timeout)
        if not ok:
            print(
                f"sparknet-liveness-monitor: liveness to {self._endpoint_url} failed: {detail}",
                file=sys.stderr,
            )

    def _after_round(self) -> None:
        self._rounds += 1
        self._evict()
        if self._rounds == 1 or self._rounds % self.push_every == 0:
            self.push()

    def once(self) -> None:
        self.refresh_roster(force=True)
        if not self.dry_run:
            self._start_listener()
        self._monitor.probe_once()
        self._after_round()
        self.close()

    def _start_listener(self) -> None:
        try:
            self._listener = ProbeListener(self.bind, self.port, self.record_activity)
        except OSError as exc:
            raise RuntimeError(f"cannot bind probe listener {self.bind}:{self.port}: {exc}") from exc
        thread = threading.Thread(target=self._listener.serve_forever, daemon=True)
        thread.start()

    def run(self) -> None:
        self.refresh_roster(force=True)
        self._start_listener()
        self._monitor.start()
        print(
            f"sparknet-liveness-monitor: probing {len(self._known)} peer(s) on "
            f"{self.bind}:{self.port}; push={'on' if self._can_push else 'off'}",
            file=sys.stderr,
        )
        try:
            self._stop.wait()
        finally:
            self.close()

    def close(self) -> None:
        self._stop.set()
        self._monitor.stop()
        if self._listener is not None:
            self._listener.shutdown()
            self._listener.server_close()
            self._listener = None

    def stop(self) -> None:
        self._stop.set()


def _bind_address(identity: dict[str, Any], override: str | None) -> str | None:
    if override:
        return override
    env_bind = os.environ.get("SPARKNET_LIVENESS_BIND", "").strip()
    if env_bind:
        return env_bind
    return _address(identity.get("overlay_ipv4"))


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    if port in SERVING_PORTS:
        raise argparse.ArgumentTypeError(f"{port} is a serving/authority port; the probe must use a non-serving port")
    return port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Peer liveness probe and claim (no authority)")
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
        help="peer vault root (default: the directory above this service)",
    )
    parser.add_argument("--bind", help="probe listener address (default: this peer's overlay address)")
    parser.add_argument(
        "--port",
        type=_port,
        default=DEFAULT_PORT,
        help=f"non-serving probe port (default: {DEFAULT_PORT})",
    )
    parser.add_argument("--endpoint", help="override the telemetry POST URL")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="probe interval in seconds")
    parser.add_argument(
        "--push-every",
        type=int,
        default=DEFAULT_PUSH_EVERY,
        help="push a liveness claim every N probe rounds",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="connect/HTTP timeout in seconds")
    parser.add_argument("--once", action="store_true", help="run one probe round, push one claim, exit")
    parser.add_argument("--dry-run", action="store_true", help="print the claim, bind nothing, send nothing")
    args = parser.parse_args(argv)

    if args.interval <= 0 or args.push_every <= 0:
        parser.error("--interval and --push-every must be positive")

    material = load_vault(args.vault_root)
    if material is None:
        print("sparknet-liveness-monitor: not watching: no readable identity/registry/master key", file=sys.stderr)
        return 0
    identity = material[0]
    bind = _bind_address(identity, args.bind)
    if bind is None and not args.dry_run:
        print("sparknet-liveness-monitor: not watching: no overlay address", file=sys.stderr)
        return 0

    try:
        service = LivenessService(
            args.vault_root,
            bind=bind or "127.0.0.1",
            port=args.port,
            endpoint=args.endpoint,
            interval=args.interval,
            push_every=args.push_every,
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(f"sparknet-liveness-monitor: {exc}", file=sys.stderr)
        return 1

    if args.once or args.dry_run:
        try:
            service.once()
        except RuntimeError as exc:
            print(f"sparknet-liveness-monitor: {exc}", file=sys.stderr)
            return 1
        return 0

    def _stop(_signum: int, _frame: Any) -> None:
        service.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        service.run()
    except RuntimeError as exc:
        print(f"sparknet-liveness-monitor: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
