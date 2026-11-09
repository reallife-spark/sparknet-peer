#!/usr/bin/env python3
"""The 4201/4202 share listeners — a signed wire around the one regime engine.

The share lifecycle is already implemented once in
``peer-node/services/sparknet-share-regime.py``. This service adds only the
thing that engine deliberately does not have: a socket and durable state. Every
message is a signed request from the sibling peer; the listener verifies the
envelope with ``shared/sparknet-http.py``, hands the message to ``Regime.dispatch``
and writes the resulting share records atomically before answering.

It does not hold an authority key, open a grant or decide a trust level. A share
still ends at the four clocks, and a Master-governed share still needs a signed
ratification on 4203. The listener carries the conversation the engine already
defines; it does not add a second state machine.

State lives at ``$XDG_STATE_HOME/sparknet/share-serve/shares.json`` (0600) by
default. A corrupt state file refuses startup: losing share deadlines silently
would leave a data-plane stanza up past its expiry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import sys
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
try:
    from sparknet_loader import load_by_path  # noqa: E402 - shared/ is added by path above
except ImportError:  # a role copy without shared/ can still answer --help

    def load_by_path(name, path, **_kwargs):
        raise FileNotFoundError(f"cannot load {path}: shared/sparknet_loader.py is not present")


HERE = Path(__file__).resolve().parent
DEFAULT_VAULT = HERE.parent
DEFAULT_PORTS = (4201, 4202)


class ShareServeError(Exception):
    """A refusal carrying the HTTP status and stable reason a router returns."""

    def __init__(self, status: int, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.detail = detail


def _load_by_path(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    if not path.is_file():
        raise ShareServeError(500, "missing-implementation", str(path))
    return load_by_path(name, path)


def _shared_dir() -> Path:
    explicit = os.environ.get("SPARKNET_SHARED_DIR", "").strip()
    if explicit:
        return Path(explicit)
    for candidate in (HERE.parent / "shared", HERE.parent.parent / "shared"):
        if (candidate / "sparknet-http.py").is_file():
            return candidate
    return HERE.parent.parent / "shared"


def _load_http() -> Any:
    return _load_by_path("sparknet_http_share_serve", _shared_dir() / "sparknet-http.py")


def _load_regime() -> Any:
    return _load_by_path("sparknet_share_regime_serve", HERE / "sparknet-share-regime.py")


def _default_control_path(state_path: Path) -> Path:
    return state_path.with_name("control.sock")


def _default_state_path() -> Path:
    explicit = os.environ.get("SPARKNET_SHARE_STATE", "").strip()
    if explicit:
        return Path(explicit)
    base = Path(os.environ.get("XDG_STATE_HOME", "").strip() or Path.home() / ".local" / "state")
    return base / "sparknet" / "share-serve" / "shares.json"


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".shares.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        with_path = Path(temporary)
        if with_path.exists():
            with_path.unlink()


class ShareService:
    """Durable owner of one node's share records.

    The regime is the rule engine. This class supplies the registry-backed mesh
    facts, persists every mutation and exposes the exact message shapes the
    listeners carry. Two instances in a test are two nodes; the wire is the only
    difference from the in-process regime tests.
    """

    def __init__(
        self,
        vault_root: Path | str = DEFAULT_VAULT,
        *,
        state_path: Path | str | None = None,
        registry_path: Path | str | None = None,
        contracts_dir: Path | str | None = None,
        node_id: str | None = None,
        now: Callable[[], datetime] | None = None,
        durations: dict[str, int] | None = None,
        master_public_key: Any | None = None,
        master_public_key_path: Path | str | None = None,
        self_key: Any | None = None,
    ):
        self.vault_root = Path(vault_root)
        self._regime_mod = _load_regime()
        self._http = _load_http()
        self._authz = self._http._load_authz()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.registry_path = Path(registry_path or (self.vault_root / "manifests" / "node-authorizations.json"))
        self.state_path = Path(state_path or _default_state_path())
        self._lock = threading.RLock()
        if master_public_key is None and master_public_key_path is not None:
            master_public_key = self._authz.load_public_key(Path(master_public_key_path))
        self._master_public_key = master_public_key
        self._self_key = self_key

        if node_id is None:
            identity = self._read_json(self.vault_root / "manifests" / "node-identity.json", required=False)
            node_id = str((identity or {}).get("node_id") or "")
        if not node_id:
            raise ShareServeError(500, "no-identity", "node-identity.json names no node_id")
        self.node_id = node_id

        self.regime = self._regime_mod.Regime(
            node_id,
            now=self._now,
            node_count=self._mesh_count,
            department_of=self._department_of,
            holds_capability=self._holds_capability,
            vault_index=self._vault_index,
            contracts_dir=Path(contracts_dir) if contracts_dir else None,
            durations=durations,
        )
        self._load()

    @staticmethod
    def _read_json(path: Path, *, required: bool = True) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if not required:
                return None
            raise ShareServeError(500, "state-missing", str(path))
        except (OSError, ValueError) as exc:
            raise ShareServeError(500, "state-corrupt", f"{path}: {exc}")

    def _registry(self) -> dict[str, Any]:
        try:
            document = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return document if isinstance(document, dict) else {}

    def _entry(self, node_id: str) -> dict[str, Any] | None:
        for entry in self._registry().get("authorizations") or []:
            if not isinstance(entry, dict):
                continue
            identity = entry.get("identity") or {}
            if str(identity.get("node_id") or "") == node_id:
                return entry
        return None

    def _vault_index(self) -> dict[str, str]:
        """Hashes of existing vault text pages, used for share conflict detection."""
        index: dict[str, str] = {}
        for path in self.vault_root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.vault_root)
            if any(
                part in {".state", ".git", "__pycache__", "keys", "blockchain", "manifests"} for part in relative.parts
            ):
                continue
            if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
                continue
            try:
                index[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
        return index

    def _mesh_count(self) -> int:
        peers = {
            str((entry.get("identity") or {}).get("node_id") or "")
            for entry in self._registry().get("authorizations") or []
            if isinstance(entry, dict) and not entry.get("revoked")
        }
        peers.discard("")
        peers.discard(self.node_id)
        return 1 + len(peers)

    def _department_of(self, node_id: str) -> str | None:
        entry = self._entry(node_id)
        department = (entry or {}).get("department")
        return str(department) if department else None

    def _holds_capability(self, node_id: str, capability: str) -> bool:
        entry = self._entry(node_id)
        return bool(entry and capability in (entry.get("capabilities") or []))

    def _load(self) -> None:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise ShareServeError(500, "state-corrupt", f"{self.state_path}: {exc}")
        if not isinstance(state, dict) or state.get("schema") != "sparknet-share-serve/1":
            raise ShareServeError(500, "state-corrupt", f"{self.state_path} is not share-serve state")
        for record in state.get("shares") or []:
            self.regime.restore(record)

    def _save(self) -> None:
        _write_atomic(
            self.state_path,
            {
                "schema": "sparknet-share-serve/1",
                "node_id": self.node_id,
                "saved_at": self._now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "shares": sorted(self.regime.shares.values(), key=lambda record: str(record.get("share_id"))),
            },
        )

    def send(self, url: str, message: dict[str, Any]) -> dict[str, Any]:
        """Sign and deliver one share message to a sibling; local control only."""
        if self._self_key is None:
            raise ShareServeError(503, "no-self-key", "outbound delivery needs --self-key")
        status, body = self._http.post_json(
            url.rstrip("/"), "POST", "/v1/share/message", {"message": message}, self._self_key
        )
        if status != 200 or not body.get("ok"):
            raise ShareServeError(
                status or 502, str(body.get("reason") or "transport-error"), str(body.get("detail") or "")
            )
        return body

    def send_payload(self, url: str, share_id: str, files: dict[str, str]) -> dict[str, Any]:
        if self._self_key is None:
            raise ShareServeError(503, "no-self-key", "outbound delivery needs --self-key")
        status, body = self._http.post_json(
            url.rstrip("/"),
            "POST",
            "/v1/share/payload",
            {"share_id": share_id, "files": files},
            self._self_key,
        )
        if status != 200 or not body.get("ok"):
            raise ShareServeError(
                status or 502, str(body.get("reason") or "transport-error"), str(body.get("detail") or "")
            )
        return body

    def offers(self, node_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(record) for record in self.regime.shares.values()]
        if node_id:
            rows = [record for record in rows if record.get("offered_by") == node_id or record.get("toward") == node_id]
        return rows

    def get(self, share_id: str) -> dict[str, Any]:
        with self._lock:
            return dict(self.regime.get(share_id))

    def offer(
        self,
        kind: str,
        *,
        toward: str | None = None,
        temporary: bool = True,
        scope: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            try:
                message = self.regime.offer(kind, toward=toward, temporary=temporary, scope=scope)
            except self._regime_mod.ShareError as exc:
                raise ShareServeError(exc.status, exc.reason, str(exc)) from exc
            self._save()
            return message

    def accept(self, share_id: str, chosen_kind: str) -> dict[str, Any]:
        with self._lock:
            try:
                message = self.regime.accept(share_id, chosen_kind)
            except self._regime_mod.ShareError as exc:
                raise ShareServeError(exc.status, exc.reason, str(exc)) from exc
            self._save()
            return message

    def use(self, share_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                message = self.regime.use(share_id)
            except self._regime_mod.ShareError as exc:
                raise ShareServeError(exc.status, exc.reason, str(exc)) from exc
            self._save()
            return message

    def close(self, share_id: str, reason: str = "explicit") -> dict[str, Any]:
        with self._lock:
            try:
                message = self.regime.close(share_id, reason)
            except self._regime_mod.ShareError as exc:
                raise ShareServeError(exc.status, exc.reason, str(exc)) from exc
            self._save()
            return message

    def tick(self) -> list[dict[str, Any]]:
        with self._lock:
            actions = self.regime.tick()
            if actions:
                self._save()
            return actions

    def _verify_ratification(self, message: dict[str, Any]) -> None:
        """A Master-governed share is only lifted by the Master's signed answer.

        The regime's in-process tests send the ``share.ratify`` shape directly;
        on a wire that shape is not authority. The peer verifies the pinned
        Master signature over the closed ratification envelope, and checks the
        envelope names this share and the same decision. Only then does the
        regime's ``_recv_ratify`` run.
        """
        if self._master_public_key is None:
            raise ShareServeError(503, "no-verifier", "no pinned Master public key for share ratification")
        signed = message.get("ratification")
        if not isinstance(signed, dict):
            raise ShareServeError(403, "unsigned-ratification", "share.ratify carries no signed ratification")
        if not self._authz.verify_share_ratification(signed, self._master_public_key):
            raise ShareServeError(
                403, "bad-signature", "share ratification does not verify against the pinned Master key"
            )
        expected = signed.get("decision") == "ratified"
        if signed.get("share_id") != message.get("share_id") or expected != bool(message.get("decision")):
            raise ShareServeError(400, "ratification-mismatch", "signed ratification does not match this message")

    def deliver(self, share_id: str, files: dict[str, str], actor: str | None = None) -> dict[str, Any]:
        """Place a signed payload in the receiver's review inbox.

        A share grants a bounded read/copy, not a write into a user's vault.
        Additions are staged under ``01_Inbox/share-<id>/`` for the existing
        inbox review path; conflicts are reported and nothing is overwritten.
        Only an open ``kb-share``/``session-handoff``/``full`` share may arrive.
        """
        if not re.fullmatch(r"share-[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(share_id)):
            raise ShareServeError(400, "bad-share-id", "share_id is not a safe identifier")
        if not isinstance(files, dict) or not files or len(files) > 200:
            raise ShareServeError(400, "malformed", "files must be a non-empty mapping of at most 200 entries")
        with self._lock:
            try:
                share = self.regime.get(share_id)
            except self._regime_mod.ShareError as exc:
                raise ShareServeError(exc.status, exc.reason, str(exc)) from exc
            if actor and actor not in {share.get("offered_by"), share.get("toward")}:
                raise ShareServeError(403, "not-party", "the caller is not a party to this share")
            if share.get("state") != "open":
                raise ShareServeError(409, "not-open", f"share {share_id} is {share.get('state')}")
            kind = share.get("accepted_kind")
            if kind not in ("kb-share", "session-handoff", "full"):
                raise ShareServeError(409, "not-deliverable", f"{kind} conveys no copy")
            hashes: dict[str, str] = {}
            for relative, content in files.items():
                if not isinstance(relative, str) or not isinstance(content, str):
                    raise ShareServeError(400, "malformed", "payload paths and contents must be strings")
                path = PurePosixPath(relative)
                if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
                    raise ShareServeError(400, "unsafe-path", relative)
                hashes[path.as_posix()] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if kind in ("kb-share", "full"):
                try:
                    summary = self.regime.reconcile(share_id, hashes)
                except self._regime_mod.ShareError as exc:
                    raise ShareServeError(exc.status, exc.reason, str(exc)) from exc
                additions = set(summary["additions"])
                conflicts = list(summary["conflicts"])
            else:
                additions = set(hashes)
                conflicts = []
            inbox_root = (self.vault_root / "01_Inbox").resolve()
            inbox = (inbox_root / f"share-{share_id}").resolve()
            try:
                inbox.relative_to(inbox_root)
            except ValueError as exc:
                raise ShareServeError(400, "unsafe-path", "share inbox escapes 01_Inbox") from exc
            for relative in sorted(additions):
                destination = (inbox / PurePosixPath(relative)).resolve()
                try:
                    destination.relative_to(inbox)
                except ValueError as exc:
                    raise ShareServeError(400, "unsafe-path", "share file escapes its inbox") from exc
                destination.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".share.", dir=destination.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.write(files[relative])
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, destination)
                finally:
                    leftover = Path(temporary)
                    if leftover.exists():
                        leftover.unlink()
            self._save()
            return {
                "ok": True,
                "share_id": share_id,
                "additions": sorted(additions),
                "conflicts": conflicts,
                "inbox": str(inbox),
            }

    def dispatch(self, message: Any) -> list[dict[str, Any]]:
        """Ingest one remote share message and persist before answering."""
        if not isinstance(message, dict) or not isinstance(message.get("msg_type"), str):
            raise ShareServeError(400, "malformed", "message must be an object with msg_type")
        if message["msg_type"] == "share.ratify":
            self._verify_ratification(message)
        with self._lock:
            try:
                actions = self.regime.dispatch(message)
            except self._regime_mod.ShareError as exc:
                raise ShareServeError(exc.status, exc.reason, str(exc)) from exc
            self._save()
            return actions


def _message_actor(message: dict[str, Any]) -> str | None:
    """The node a wire message says it came from, when the shape carries one."""
    if message.get("msg_type") == "share.offer":
        return message.get("offered_by")
    if message.get("msg_type") == "share.accept":
        return message.get("accepted_by")
    return message.get("by")


class ShareControl:
    """A 0600 Unix socket for the node operator; not a network surface.

    The listener has no local HTTP face by design. Control actions (offer,
    accept, close, send) run in the service process so the persisted state and
    the in-memory regime cannot diverge. The socket is reachable only by the
    node's own uid, and ``send`` uses the node key held by the service.
    """

    def __init__(self, service: ShareService, path: Path):
        self.service = service
        self.path = Path(path)

    def _handle(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        try:
            if action == "state":
                return {"ok": True, "node_id": self.service.node_id, "shares": self.service.offers()}
            if action == "offer":
                message = self.service.offer(
                    str(request.get("kind") or ""),
                    toward=request.get("toward"),
                    temporary=bool(request.get("temporary", True)),
                    scope=request.get("scope"),
                )
                return {"ok": True, "message": message, "share": self.service.get(message["share_id"])}
            if action == "accept":
                message = self.service.accept(str(request.get("share_id") or ""), str(request.get("kind") or ""))
                return {"ok": True, "message": message, "share": self.service.get(message["share_id"])}
            if action == "use":
                message = self.service.use(str(request.get("share_id") or ""))
                return {"ok": True, "message": message}
            if action == "close":
                message = self.service.close(
                    str(request.get("share_id") or ""), str(request.get("reason") or "explicit")
                )
                return {"ok": True, "message": message}
            if action == "tick":
                return {"ok": True, "actions": self.service.tick()}
            if action == "deliver":
                return self.service.deliver(str(request.get("share_id") or ""), request.get("files") or {})
            if action == "send":
                return {
                    "ok": True,
                    "response": self.service.send(str(request.get("url") or ""), request.get("message")),
                }
            if action == "send_payload":
                return {
                    "ok": True,
                    "response": self.service.send_payload(
                        str(request.get("url") or ""),
                        str(request.get("share_id") or ""),
                        request.get("files") or {},
                    ),
                }
            return {"ok": False, "reason": "unknown-action"}
        except ShareServeError as exc:
            return {"ok": False, "reason": exc.reason, "detail": exc.detail, "status": exc.status}
        except self.service._regime_mod.ShareError as exc:  # pragma: no cover - service wraps these
            return {"ok": False, "reason": exc.reason, "detail": str(exc), "status": exc.status}

    def serve_forever(self, stopping: threading.Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            self.path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.path))
        os.chmod(self.path, 0o600)
        listener.listen(16)
        listener.settimeout(0.2)
        try:
            while not stopping.is_set():
                try:
                    connection, _ = listener.accept()
                except OSError as exc:
                    if isinstance(exc, (TimeoutError, socket.timeout)):
                        continue
                    raise
                with connection:
                    connection.settimeout(5)
                    data = b""
                    while b"\n" not in data and len(data) < 1_048_576:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    try:
                        request = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
                        response = self._handle(request)
                    except (ValueError, UnicodeDecodeError) as exc:
                        response = {"ok": False, "reason": "malformed", "detail": str(exc)}
                    connection.sendall(json.dumps(response, sort_keys=True).encode("utf-8") + b"\n")
        finally:
            listener.close()
            if self.path.exists():
                self.path.unlink()


def router_for(service: ShareService):
    """The listener's route table: one signed remote message route and a view."""

    def router(method: str, path: str, body: dict[str, Any], identity: dict[str, Any]):
        if method == "GET" and path in ("/v1/share", "/v1/share/state"):
            caller = str(identity.get("verified_key_id") or "")
            return 200, {"ok": True, "node_id": service.node_id, "shares": service.offers(caller)}
        if method == "POST" and path == "/v1/share/payload":
            share_id = body.get("share_id")
            if not isinstance(share_id, str) or not share_id:
                return 400, {"ok": False, "reason": "malformed", "detail": "share_id required"}
            try:
                delivered = service.deliver(share_id, body.get("files"), str(identity.get("verified_key_id") or ""))
            except ShareServeError as exc:
                return exc.status, {"ok": False, "reason": exc.reason, "detail": exc.detail}
            return 200, delivered
        if method == "POST" and path == "/v1/share/message":
            message = body.get("message")
            if not isinstance(message, dict):
                return 400, {"ok": False, "reason": "malformed", "detail": "message object required"}
            actor = _message_actor(message)
            verified = str(identity.get("verified_key_id") or "")
            if actor and verified and actor != verified:
                return 403, {
                    "ok": False,
                    "reason": "key-mismatch",
                    "detail": "the message names a different node than the key that signed it",
                }
            try:
                actions = service.dispatch(message)
            except ShareServeError as exc:
                return exc.status, {"ok": False, "reason": exc.reason, "detail": exc.detail}
            return 200, {"ok": True, "actions": actions}
        return 404, {"ok": False, "reason": "not-found"}

    return router


def bind_servers(
    service: ShareService,
    *,
    bind: str,
    ports: tuple[int, ...],
    public_key: Any | None = None,
    public_keys: dict[str, Any] | None = None,
    http: Any | None = None,
) -> list[Any]:
    """Bind every share port before serving; return the live servers.

    ``public_key`` is the pair case (one pinned counterpart). ``public_keys``
    is the governed case: the counterpart(s) plus the Master key that signs a
    ratification, keyed by node id so the router can bind the message actor to
    the key that actually signed it.
    """
    transport = http or _load_http()
    router = router_for(service)
    listeners = []
    try:
        for port in ports:
            route = transport.Route(
                "/",
                lambda request: router(request.method, request.path, request.json or {}, request.identity),
                auth="signed",
                prefix=True,
            )
            listeners.append(
                transport.listen(
                    [route],
                    name="share-serve",
                    bind=bind,
                    port=int(port),
                    public_keys=public_keys or None,
                    public_key=None if public_keys else public_key,
                )
            )
    except Exception:
        for listener in listeners:
            listener.stop()
        raise
    return listeners


def _serve_forever(listeners: list[Any], *, stopping: threading.Event | None = None) -> None:
    for listener in listeners:
        listener.start()
    stopping = stopping or threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    try:
        while not stopping.wait(1.0):
            pass
    finally:
        for listener in listeners:
            listener.stop()


def _parse_ports(values: list[str]) -> tuple[int, ...]:
    ports: list[int] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            port = int(item)
            if not 1 <= port <= 65535:
                raise ValueError(f"invalid port {port}")
            ports.append(port)
    return tuple(dict.fromkeys(ports)) or DEFAULT_PORTS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Signed 4201/4202 share listener")
    parser.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT)
    parser.add_argument("--state", type=Path, default=None, help="share records JSON (0600)")
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument("--contracts", type=Path, default=None)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", action="append", default=[], help="port(s); repeat or comma-separate")
    parser.add_argument(
        "--trusted-key",
        action="append",
        default=[],
        metavar="NODE_ID=PATH",
        help="a caller key and its node id; repeat for the counterpart and Master",
    )
    parser.add_argument(
        "--master-public-key",
        type=Path,
        default=None,
        help="pinned Master Ed25519 key for 3+ node ratification (required for master_governed)",
    )
    parser.add_argument("--master-key-id", default="master", help="node id used for --master-public-key")
    parser.add_argument("--self-key", type=Path, help="this node's Ed25519 key for outbound delivery")
    parser.add_argument("--self-key-id", default=None, help="this node's id (defaults to node_id)")
    parser.add_argument("--control", type=Path, help="operator Unix socket (default: beside --state)")
    args = parser.parse_args(argv)

    try:
        ports = _parse_ports(args.port)
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    try:
        http = _load_http()
        authz = http._load_authz()
        keyring: dict[str, Any] = {}
        for item in args.trusted_key:
            key_id, separator, path = item.partition("=")
            if not separator or not key_id or not path:
                raise ShareServeError(400, "bad-key", f"expected NODE_ID=PATH, got {item!r}")
            key_path = Path(path)
            if not key_path.is_file():
                raise ShareServeError(500, "no-verifier", f"missing key {key_path}")
            keyring[key_id] = authz.load_public_key(key_path)
        if args.master_public_key is not None:
            if not args.master_public_key.is_file():
                raise ShareServeError(500, "no-verifier", f"missing Master key {args.master_public_key}")
            keyring[args.master_key_id] = authz.load_public_key(args.master_public_key)
        if not keyring:
            raise ShareServeError(500, "no-verifier", "at least one --trusted-key is required")
        self_key = authz.load_private_key(args.self_key) if args.self_key else None
        service = ShareService(
            args.vault_root,
            state_path=args.state,
            registry_path=args.registry,
            contracts_dir=args.contracts,
            master_public_key_path=args.master_public_key,
            self_key=self_key,
        )
        if args.self_key and args.self_key_id:
            keyring[args.self_key_id] = self_key.public_key()
        servers = bind_servers(service, bind=args.bind, ports=ports, public_keys=keyring, http=http)
        control = ShareControl(service, args.control or _default_control_path(service.state_path))
        stopping = threading.Event()
        control_thread = threading.Thread(target=control.serve_forever, args=(stopping,), daemon=True)
        control_thread.start()
    except (OSError, ValueError, ShareServeError) as exc:
        print(f"share-serve: {exc}", file=sys.stderr)
        return 1
    print(
        f"share-serve {service.node_id}: {','.join(str(p) for p in ports)} "
        f"(state {service.state_path}, control {control.path})",
        flush=True,
    )
    _serve_forever(servers, stopping=stopping)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
