#!/usr/bin/env python3
"""Sparknet OpenCode engine — the WebUI-facing agent runtime.

Open WebUI talks OpenAI-compatible HTTP to this service. The service maps the
signed-in WebUI user and chat to a per-user OpenCode sandbox, and the sandbox
mounts only that user's private vault. OpenCode is the execution engine; this
file is only the adapter and the sandbox supervisor. It holds no signing key,
never touches the ledger or registry, and cannot grant authority.

Why a sandbox per user
----------------------
The peer owns a private ``peer-<user_id>`` vault per user. Running one OpenCode
server over the role vault would let one user's model read another user's
pages. Each sandbox instead gets:

* exactly one bind mount, ``<vault>:/work``;
* a per-user Docker network (``--internal``) whose only reachable peer is the
  host model proxy on that network's gateway;
* a read-only root filesystem, dropped capabilities and no host state mounts.

The model proxy allow-lists the model API paths and forwards to the node's
local model endpoint (Ollama on the Spark pair, or the goSpark/vLLM gateway on
an AGX node). A build agent may run shell commands and tests inside the
sandbox; it has no internet and no host filesystem outside ``/work``.

The engine is deliberately not a network service beyond loopback. The
``/v1/chat/completions`` surface requires both the deploy-time bearer token and
the WebUI-forwarded user header. The chat header is a routing hint, not
authority: the vault path is derived here from the validated user id and the
user's provisioned directory.

Usage::

    sparknet-opencode-engine --role peer --bind 127.0.0.1:4204
    sparknet-opencode-engine --role master --single-vault /srv/sparknet/master-admin

The master form runs one sandbox over a knowledge slice; it never mounts the
master role vault (which contains keys, the ledger and the registry).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import http.client
import json
import os
import queue
import re
import secrets
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:  # Python 3.9+
    from collections.abc import Iterator
except ImportError:  # pragma: no cover - Python 3.8
    from typing import Iterator  # noqa: UP035

DEFAULT_BIND = "127.0.0.1:4204"
DEFAULT_PORT_BASE = 4300
DEFAULT_NETWORK_PREFIX = "sparknet-opencode"
DEFAULT_SUBNET_BASE = "172.30"
DEFAULT_PROXY_PORT = 11500
DEFAULT_SOCKET_ROOT = "/run/sparknet-opencode"
SANDBOX_UID = int(os.environ.get("SPARKNET_OPENCODE_SANDBOX_UID", "10001"))
SANDBOX_GID = int(os.environ.get("SPARKNET_OPENCODE_SANDBOX_GID", "10001"))
DEFAULT_PROXY_PATH_ALLOW = (
    "/v1/",
    "/api/chat",
    "/api/embed",
    "/api/embedding",
    "/api/tags",
    "/api/ps",
    "/api/show",
    "/health",
    "/global/health",
)
MODEL_AGENTS = {
    "sparknet-chat": "sparknet-chat",
    "sparknet-plan": "sparknet-plan",
    "sparknet-build": "sparknet-build",
}
MODEL_LABELS = {
    "sparknet-chat": "Sparknet Chat (your vault)",
    "sparknet-plan": "Sparknet Plan (your vault)",
    "sparknet-build": "Sparknet Build (your vault)",
}
USER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


class EngineError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = message


def _read_token(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value


def _valid_user(user: str) -> str:
    user = (user or "").strip()
    if not USER_RE.match(user):
        raise EngineError(400, "missing or invalid X-Sparknet-User header")
    return user


def _read_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json_file(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def _http_json(
    url: str,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    data = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in request_headers.items():
        req.add_header(key, value)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw.decode("utf-8")) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw.decode("utf-8"))
        except Exception:
            detail = raw.decode("utf-8", "replace")
        return exc.code, detail


def _http_json_url(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise EngineError(500, "model upstream must be an http URL")
    return parsed.hostname, int(parsed.port or 80)


class NetworkAllocator:
    """Persist one isolated Docker subnet per sandbox user."""

    def __init__(self, state_root: Path) -> None:
        self.path = state_root / "networks.json"
        self.lock = threading.Lock()
        self.data = _read_json_file(self.path, {})

    def allocate(self, user: str) -> dict[str, Any]:
        key = user
        with self.lock:
            row = self.data.get(key)
            if isinstance(row, dict) and row.get("index") and row.get("gateway"):
                return row
            used = {
                int(item.get("index"))
                for item in self.data.values()
                if isinstance(item, dict) and item.get("index") is not None
            }
            for index in range(1, 250):
                if index in used:
                    continue
                row = {
                    "user": user,
                    "index": index,
                    "subnet": f"{DEFAULT_SUBNET_BASE}.{index}.0/29",
                    "gateway": f"{DEFAULT_SUBNET_BASE}.{index}.1",
                    "created_at": _now(),
                }
                self.data[key] = row
                _write_json_file(self.path, self.data)
                return row
            raise EngineError(507, "no free sandbox subnet")


class ModelProxyHandler(BaseHTTPRequestHandler):
    """Allow-listed reverse proxy from one sandbox network to the model API."""

    protocol_version = "HTTP/1.1"
    server_version = "sparknet-model-proxy/1"
    timeout = 30
    max_body_bytes = 16 << 20
    upstream = "http://127.0.0.1:11434"
    allowed_subnet = ""
    allowed_paths = DEFAULT_PROXY_PATH_ALLOW

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - stdlib signature
        log(f"model-proxy {self.address_string()} {fmt % args}")

    def _client_allowed(self) -> bool:
        try:
            import ipaddress

            address = ipaddress.ip_address(self.client_address[0])
            return address in ipaddress.ip_network(self.allowed_subnet, strict=False)
        except Exception:
            return False

    def _path_allowed(self) -> bool:
        path = urllib.parse.urlsplit(self.path).path
        return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in self.allowed_paths)

    def _proxy(self) -> None:
        if not self._client_allowed():
            self.send_error(403, "sandbox network only")
            return
        if not self._path_allowed():
            self.send_error(403, "model API path only")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400, "invalid content length")
            return
        if length < 0 or length > self.max_body_bytes:
            self.send_error(413, "request body too large")
            return
        body = self.rfile.read(length) if length else None
        host, port = _http_json_url(self.upstream)
        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_HEADERS or key.lower() == "host":
                continue
            headers[key] = value
        connection = http.client.HTTPConnection(host, port, timeout=120)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in HOP_HEADERS:
                    continue
                # http.client has already de-chunked a chunked response, so
                # forward a Content-Length when present but never a chunked
                # Transfer-Encoding. A missing length is handled by closing.
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001 - a proxy failure is a clean 502
            log(f"model-proxy upstream error: {exc}")
            try:
                self.send_error(502, "model upstream unavailable")
            except Exception:
                pass
            return
        finally:
            connection.close()
            self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        self._proxy()

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        self._proxy()


class ProxyThread:
    def __init__(self, gateway: str, port: int, upstream: str, subnet: str) -> None:
        self.gateway = gateway
        handler = type(
            "BoundModelProxyHandler",
            (ModelProxyHandler,),
            {"upstream": upstream, "allowed_subnet": subnet},
        )
        self.server = ThreadingHTTPServer((gateway, port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name=f"model-proxy-{gateway}", daemon=True)

    def start(self) -> None:
        self.thread.start()
        log(f"model proxy listening on {self.server.server_address[0]}:{self.server.server_address[1]}")

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Sandbox:
    def __init__(
        self, user: str, container: str, socket_path: Path, vault: Path, proxy_url: str, password: str = ""
    ) -> None:
        self.user = user
        self.container = container
        self.socket_path = socket_path
        self.vault = vault
        self.proxy_url = proxy_url
        self.password = password
        self.base_url = f"unix://{socket_path}"


class SandboxManager:
    def __init__(self, args: argparse.Namespace) -> None:
        self.role = args.role
        self.knowledge_root = Path(args.knowledge_root).resolve()
        self.single_vault = Path(args.single_vault).resolve() if args.single_vault else None
        self.state_root = Path(args.state_root).resolve()
        self.image = args.image
        self.docker = args.docker
        self.port_base = int(args.port_base)
        self.proxy_port = int(args.proxy_port)
        self.model_upstream = args.model_upstream
        self.memory = args.sandbox_memory
        self.cpus = args.sandbox_cpus
        self.idle_seconds = int(args.idle_seconds)
        self.config_template = Path(args.config_template).resolve()
        self.model_id = args.model_id
        self.small_model_id = args.small_model
        self.allocator = NetworkAllocator(self.state_root)
        self.lock = threading.Lock()
        self._password_lock = threading.Lock()
        self.proxies: dict[str, ProxyThread] = {}
        self.last_used: dict[str, float] = {}
        self._owned_vaults: set[str] = set()
        self._owned_states: set[str] = set()
        self.state_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_root, 0o700)
        except OSError:
            pass

    # -- paths ---------------------------------------------------------------
    def vault_for(self, user: str) -> Path:
        if self.single_vault is not None:
            vault = self.single_vault
        else:
            vault = self.knowledge_root / (f"peer-{user}")
        try:
            resolved = vault.resolve()
        except OSError:
            raise EngineError(404, "user vault not provisioned")
        if self.single_vault is None:
            root = self.knowledge_root
            try:
                resolved.relative_to(root)
            except ValueError:
                raise EngineError(403, "user vault escapes the knowledge root")
            if resolved.name != f"peer-{user}":
                raise EngineError(403, "user vault does not match the signed-in user")
        if not resolved.is_dir():
            raise EngineError(404, "user vault not provisioned")
        return resolved

    def _own_path(self, path: Path, recursive: bool = False) -> None:
        if os.geteuid() != 0:
            return
        # Never follow a sandbox-created symlink: chowning /etc/shadow through a
        # symlink in the vault would hand the sandbox a host file it did not own.
        if path.is_symlink():
            return
        try:
            os.chown(path, SANDBOX_UID, SANDBOX_GID, follow_symlinks=False)
            if recursive and path.is_dir():
                for child in path.rglob("*"):
                    if child.is_symlink():
                        continue
                    try:
                        os.chown(child, SANDBOX_UID, SANDBOX_GID, follow_symlinks=False)
                    except OSError:
                        pass
        except OSError:
            pass

    def user_state(self, user: str) -> Path:
        path = self.state_root / "users" / user
        path.mkdir(parents=True, exist_ok=True)
        (path / "data").mkdir(exist_ok=True)
        (path / "cache").mkdir(exist_ok=True)
        try:
            os.chmod(path, 0o700)
            os.chmod(path / "data", 0o700)
            os.chmod(path / "cache", 0o700)
        except OSError:
            pass
        if user not in self._owned_states:
            self._own_path(path, recursive=True)
            self._owned_states.add(user)
        return path

    def socket_for(self, user: str) -> tuple[Path, Path]:
        root = Path(os.environ.get("SPARKNET_OPENCODE_SOCKET_ROOT", DEFAULT_SOCKET_ROOT))
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        digest = hashlib.sha256(user.encode("utf-8")).hexdigest()[:16]
        directory = root / digest
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        self._own_path(directory)
        return directory, directory / "opencode.sock"

    def _own_vault(self, user: str, vault: Path) -> None:
        if user in self._owned_vaults:
            return
        self._own_path(vault, recursive=True)
        self._owned_vaults.add(user)

    def container_name(self, user: str) -> str:
        return f"{DEFAULT_NETWORK_PREFIX}-{user}"

    def password_for(self, user: str) -> str:
        """A per-sandbox server password, kept in the 0700 engine state dir.

        OpenCode 2 requires HTTP basic auth on its loopback API. The password is
        per user, never logged, and never the engine token.
        """
        state = self.user_state(user)
        path = state / "opencode-password"
        with self._password_lock:
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if len(value) < 32:
                value = secrets.token_urlsafe(32)
                path.write_text(value + "\n", encoding="utf-8")
                os.chmod(path, 0o600)
            return value

    # -- docker --------------------------------------------------------------
    def _run(self, args: list[str], timeout: float = 60.0) -> tuple[int, str, str]:
        try:
            completed = subprocess.run(
                [self.docker, *args],
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise EngineError(503, "docker CLI is not available")
        except subprocess.TimeoutExpired:
            raise EngineError(503, "docker command timed out")
        return (
            completed.returncode,
            completed.stdout.decode("utf-8", "replace").strip(),
            completed.stderr.decode("utf-8", "replace").strip(),
        )

    def _network_name(self, user: str) -> str:
        return f"{DEFAULT_NETWORK_PREFIX}-net-{user}"

    def ensure_network(self, user: str) -> dict[str, Any]:
        row = self.allocator.allocate(user)
        name = self._network_name(user)
        rc, _out, _err = self._run(["network", "inspect", name])
        if rc == 0:
            return row
        rc, _out, err = self._run(
            [
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                "--subnet",
                row["subnet"],
                "--gateway",
                row["gateway"],
                name,
            ]
        )
        if rc != 0:
            raise EngineError(503, f"cannot create sandbox network: {err}")
        return row

    def ensure_proxy(self, user: str, row: dict[str, Any]) -> str:
        gateway = str(row["gateway"])
        with self.lock:
            proxy = self.proxies.get(gateway)
            if proxy is None:
                try:
                    proxy = ProxyThread(gateway, self.proxy_port, self.model_upstream, str(row["subnet"]))
                except OSError as exc:
                    raise EngineError(503, f"cannot bind model proxy on {gateway}: {exc}")
                proxy.start()
                self.proxies[gateway] = proxy
        return f"http://{gateway}:{self.proxy_port}/v1"

    def _config_for(self, user: str, proxy_url: str) -> Path:
        state = self.user_state(user)
        path = state / "opencode.json"
        try:
            template = self.config_template.read_text(encoding="utf-8")
        except OSError as exc:
            raise EngineError(500, f"opencode config template missing: {exc}")
        content = (
            template.replace("${MODEL_PROXY_URL}", proxy_url)
            .replace("${MODEL_ID}", self.model_id)
            .replace("${SMALL_MODEL_ID}", self.small_model_id)
        )
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        stamp = state / "opencode.json.sha256"
        old = stamp.read_text(encoding="utf-8").strip() if stamp.exists() else ""
        if old != digest:
            path.write_text(content, encoding="utf-8")
            os.chmod(path, 0o644)
            self._own_path(path)
            stamp.write_text(digest, encoding="utf-8")
            os.chmod(stamp, 0o600)
        return path

    def _running(self, name: str) -> bool:
        rc, out, _err = self._run(["inspect", "--type", "container", "-f", "{{.State.Running}}", name])
        return rc == 0 and out.lower() == "true"

    def _wait_health(self, socket_path: Path, password: str, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        last = ""
        client = OpenCodeClient(f"unix://{socket_path}", password)
        while time.time() < deadline:
            if socket_path.exists() and client.health():
                return
            last = "socket/health not ready"
            time.sleep(1.0)
        raise EngineError(503, f"sandbox did not become healthy: {last}")

    def ensure(self, user: str) -> Sandbox:
        vault = self.vault_for(user)
        self._own_vault(user, vault)
        row = self.ensure_network(user)
        proxy_url = self.ensure_proxy(user, row)
        name = self.container_name(user)
        state = self.user_state(user)
        socket_dir, socket_path = self.socket_for(user)
        password = self.password_for(user)
        password_env = state / "opencode.env"
        password_env.write_text(f"OPENCODE_SERVER_PASSWORD={password}\n", encoding="utf-8")
        os.chmod(password_env, 0o600)
        rc, running_image, _err = self._run(["inspect", "--type=container", "-f", "{{.Config.Image}}", name])
        if rc == 0 and running_image.strip() != self.image:
            self._run(["rm", "-f", name], timeout=60)
        if self._running(name):
            self.last_used[user] = time.time()
            return Sandbox(user, name, socket_path, vault, proxy_url, password)
        rc, _out, _err = self._run(["inspect", "--type", "container", name])
        if rc == 0:
            rc, _out, err = self._run(["start", name], timeout=60)
            if rc != 0:
                raise EngineError(503, f"cannot start sandbox: {err}")
        else:
            config = self._config_for(user, proxy_url)
            args = [
                "run",
                "-d",
                "--name",
                name,
                "--label",
                f"sparknet.opencode.user={user}",
                "--network",
                self._network_name(user),
                "--read-only",
                "--user",
                f"{SANDBOX_UID}:{SANDBOX_GID}",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=256m",
                "--tmpfs",
                "/home/opencode/.local:rw,noexec,nosuid,size=64m,mode=1777",
                "--tmpfs",
                "/home/opencode/.cache:rw,noexec,nosuid,size=64m,mode=1777",
                "--tmpfs",
                "/home/opencode/.config:rw,noexec,nosuid,size=16m,mode=1777",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "512",
                "--memory",
                self.memory,
                "--cpus",
                self.cpus,
                "--mount",
                f"type=bind,src={vault},dst=/work",
                "--mount",
                "type=bind,src=%s,dst=/home/opencode/.local/share/opencode" % (state / "data"),
                "--mount",
                "type=bind,src=%s,dst=/home/opencode/.cache/opencode" % (state / "cache"),
                "--mount",
                f"type=bind,src={socket_dir},dst=/run/sparknet-runtime",
                "--mount",
                f"type=bind,src={config},dst=/etc/opencode/opencode.json,readonly",
                "--env",
                "OPENCODE_CONFIG=/etc/opencode/opencode.json",
                "--env-file",
                str(password_env),
                "--env",
                "HOME=/home/opencode",
                "--env",
                "XDG_CONFIG_HOME=/home/opencode/.config",
                "--env",
                "XDG_DATA_HOME=/home/opencode/.local/share",
                "--env",
                "XDG_CACHE_HOME=/home/opencode/.cache",
                "--entrypoint",
                "sh",
                self.image,
                "-c",
                "python3 /opt/sparknet/unix-proxy.py /run/sparknet-runtime/opencode.sock 127.0.0.1 4200 & "
                "exec opencode serve --hostname 127.0.0.1 --port 4200",
            ]
            rc, _out, err = self._run(args, timeout=120)
            if rc != 0:
                raise EngineError(503, f"cannot create sandbox: {err}")
        try:
            self._wait_health(socket_path, password)
        except EngineError:
            _rc, logs, _err = self._run(["logs", "--tail", "40", name])
            raise EngineError(503, f"sandbox health failed: {logs}")
        self.last_used[user] = time.time()
        return Sandbox(user, name, socket_path, vault, proxy_url, password)

    def touch(self, user: str) -> None:
        self.last_used[user] = time.time()

    def stop(self, user: str) -> None:
        name = self.container_name(user)
        self._run(["stop", name], timeout=60)
        self.last_used.pop(user, None)

    def prune_idle(self) -> int:
        if self.idle_seconds <= 0:
            return 0
        now = time.time()
        stopped = 0
        for user, used in list(self.last_used.items()):
            if now - used < self.idle_seconds:
                continue
            try:
                self.stop(user)
                stopped += 1
            except Exception as exc:  # noqa: BLE001 - a prune failure is logged, never fatal
                log(f"idle stop failed for {user}: {exc}")
        return stopped


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection over a Unix domain socket."""

    def __init__(self, socket_path: str, timeout: float = 600.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class OpenCodeClient:
    """The OpenCode 2 HTTP API over the sandbox's loopback Unix socket.

    v2 moved the API under ``/api`` and requires HTTP basic auth. The password
    is per sandbox, generated and stored in the 0700 engine state directory.
    """

    def __init__(self, base_url: str, password: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.socket_path = ""
        if self.base_url.startswith("unix://"):
            self.socket_path = self.base_url[len("unix://") :]

    def _connection(self, timeout: float = 600.0) -> http.client.HTTPConnection:
        if self.socket_path:
            return UnixHTTPConnection(self.socket_path, timeout=timeout)
        parsed = urllib.parse.urlsplit(self.base_url)
        return http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)

    def _authorization(self) -> str:
        if not self.password:
            return ""
        token = base64.b64encode(f"opencode:{self.password}".encode()).decode("ascii")
        return "Basic " + token

    def request(self, method: str, path: str, body: Any = None, timeout: float = 600.0) -> tuple[int, Any]:
        connection = self._connection(timeout=timeout)
        data = None
        headers = {"Accept": "application/json", "Connection": "close"}
        authorization = self._authorization()
        if authorization:
            headers["Authorization"] = authorization
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except json.JSONDecodeError:
                payload = raw.decode("utf-8", "replace")
            return response.status, payload
        finally:
            connection.close()

    def health(self) -> bool:
        try:
            status, body = self.request("GET", "/api/info", timeout=3)
            return status == 200 and bool(isinstance(body, dict) and body.get("version"))
        except Exception:
            return False

    def create_session(
        self, *, title: str = "", agent: str = "", model_id: str = "", model_provider: str = "local"
    ) -> str:
        body: dict[str, Any] = {"title": title or "Sparknet WebUI"}
        if agent:
            body["agent"] = agent
        if model_id:
            body["model"] = {"providerID": model_provider, "id": model_id}
        status, payload = self.request("POST", "/api/session", body=body, timeout=30)
        data = payload.get("data") if isinstance(payload, dict) else None
        if status != 200 or not isinstance(data, dict) or not data.get("id"):
            raise EngineError(502, f"OpenCode could not create a session: {payload}")
        return str(data["id"])

    def prompt(self, session_id: str, text: str, *, delivery: str = "queue") -> dict[str, Any]:
        body = {"text": text, "delivery": delivery}
        status, result = self.request("POST", f"/api/session/{session_id}/prompt", body=body, timeout=900)
        if status not in (200, 204):
            raise EngineError(502, f"OpenCode prompt failed: {result}")
        return result if isinstance(result, dict) else {}

    def messages(self, session_id: str, timeout: float = 30.0) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"order": "desc", "type": "assistant", "limit": "20"})
        status, payload = self.request("GET", f"/api/session/{session_id}/message?{query}", timeout=timeout)
        data = payload.get("data") if isinstance(payload, dict) else None
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def answer_permission(self, session_id: str, permission_id: str, response: str = "reject") -> None:
        path = f"/api/session/{session_id}/permission/{permission_id}/reply"
        try:
            self.request("POST", path, body={"decision": response}, timeout=20)
        except Exception as exc:  # noqa: BLE001 - an unresolved card is reported in the answer
            log(f"permission {permission_id} could not be answered: {exc}")

    def abort(self, session_id: str) -> None:
        try:
            self.request("POST", f"/api/session/{session_id}/interrupt", timeout=10)
        except Exception:
            pass

    def event_lines(self, stop: threading.Event | None = None, timeout: float = 900.0) -> Iterator[str]:
        # A bounded read timeout lets the reader observe `stop` after the turn
        # ends without breaking the HTTP/1.1 response object on every heartbeat.
        # The server heartbeats well inside this window, so an active turn keeps
        # its stream alive; an idle reader exits within one heartbeat.
        connection = self._connection(timeout=60.0)
        headers = {"Accept": "text/event-stream"}
        authorization = self._authorization()
        if authorization:
            headers["Authorization"] = authorization
        connection.request("GET", "/api/event", headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise EngineError(502, f"OpenCode event stream refused: HTTP {response.status}")
        try:
            while stop is None or not stop.is_set():
                try:
                    line = response.readline()
                except (TimeoutError, OSError):
                    continue
                if not line:
                    break
                yield line.decode("utf-8", "replace").rstrip("\r\n")
        finally:
            connection.close()


class Engine:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.sandbox = SandboxManager(args)
        self.token = _read_token(Path(args.token_file)) if args.token_file else ""
        if not self.token and args.token:
            self.token = args.token
        if not self.token:
            log("WARNING: no engine token configured; /v1 requests will be refused")
        self.model_id = args.model_id
        self.sessions_path = self.sandbox.state_root / "sessions.json"
        self.sessions = _read_json_file(self.sessions_path, {})
        self.sessions_lock = threading.Lock()
        self.prune_stop = threading.Event()
        self.prune_thread = threading.Thread(target=self._prune_loop, name="sandbox-prune", daemon=True)
        self.prune_thread.start()

    def _prune_loop(self) -> None:
        while not self.prune_stop.wait(60):
            try:
                self.sandbox.prune_idle()
            except Exception as exc:  # noqa: BLE001
                log(f"prune loop: {exc}")

    def close(self) -> None:
        self.prune_stop.set()

    def _session_key(self, user: str, chat_id: str, agent: str) -> str:
        return "{}:{}:{}".format(user, chat_id or "api", agent)

    def _session_for(self, user: str, chat_id: str, title: str, agent: str, model_id: str) -> str:
        key = self._session_key(user, chat_id, agent)
        with self.sessions_lock:
            row = self.sessions.get(key)
            if isinstance(row, dict) and row.get("session_id"):
                return str(row["session_id"])
        sandbox = self.sandbox.ensure(user)
        client = OpenCodeClient(sandbox.base_url, sandbox.password)
        session_id = client.create_session(title=title, agent=agent, model_id=model_id)
        with self.sessions_lock:
            self.sessions[key] = {
                "session_id": session_id,
                "created_at": _now(),
                "user": user,
                "chat_id": chat_id,
                "agent": agent,
            }
            _write_json_file(self.sessions_path, self.sessions)
        return session_id

    def models(self) -> list[dict[str, Any]]:
        return [
            {
                "id": model,
                "object": "model",
                "owned_by": "sparknet",
                "created": 0,
                "name": label,
                "agent": MODEL_AGENTS[model],
            }
            for model, label in MODEL_LABELS.items()
        ]

    @staticmethod
    def _last_user_text(messages: Any) -> str:
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(str(part.get("text") or ""))
                return "\n".join(parts).strip()
        return ""

    @staticmethod
    def _system_text(messages: Any) -> str:
        if not isinstance(messages, list):
            return ""
        parts = []
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "system":
                content = message.get("content")
                if isinstance(content, str):
                    parts.append(content)
        return "\n\n".join(parts)

    def _agent(self, model: str) -> str:
        if model not in MODEL_AGENTS:
            raise EngineError(400, f"unknown Sparknet model {model!r}")
        return MODEL_AGENTS[model]

    def chat(self, body: dict[str, Any], user: str, chat_id: str) -> tuple[dict[str, Any], bool]:
        model = str(body.get("model") or "")
        agent = self._agent(model)
        text = self._last_user_text(body.get("messages"))
        if not text:
            raise EngineError(400, "messages must contain a user text message")
        system = self._system_text(body.get("messages"))
        if system:
            text = f"{system}\n\nUser request:\n{text}"
        sandbox = self.sandbox.ensure(user)
        self.sandbox.touch(user)
        client = OpenCodeClient(sandbox.base_url, sandbox.password)
        if not client.health():
            raise EngineError(503, "OpenCode sandbox is not healthy")
        session_id = self._session_for(user, chat_id, "Sparknet %s" % (chat_id or user), agent, self.model_id)
        content = self._collect_turn(client, session_id, text)
        completion = {
            "id": f"chatcmpl-{hashlib.sha256((session_id + text).encode()).hexdigest()[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        return completion, False

    @staticmethod
    def _render_tool(part: dict[str, Any]) -> str:
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = str(state.get("status") or "")
        if status not in ("completed", "error"):
            return ""
        name = html.escape(str(part.get("tool") or part.get("name") or "tool"))
        call_id = html.escape(str(part.get("callID") or part.get("id") or "tool"))
        arguments = html.escape(json.dumps(state.get("input") or {}, ensure_ascii=False))
        output = state.get("output") or state.get("error") or ""
        if not output and isinstance(state.get("content"), list):
            texts = []
            for item in state["content"]:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    texts.append(str(item.get("text") or ""))
                elif item.get("type") == "file":
                    texts.append(str(item.get("path") or item.get("name") or ""))
            output = "\n".join(texts)
        return (
            f'<details type="tool_calls" done="true" id="{call_id}" name="{name}" arguments="{arguments}">\n'
            f"<summary>Tool Executed</summary>\n{html.escape(str(output))}\n</details>\n"
        )

    def _render_result(self, result: dict[str, Any], session_id: str) -> str:
        pieces = []
        parts = result.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    pieces.append(str(part.get("text") or ""))
                elif part.get("type") == "tool":
                    block = self._render_tool(part)
                    if block:
                        pieces.append(block)
        return "".join(pieces).strip()

    def _render_messages(self, messages: list[dict[str, Any]]) -> str:
        for message in messages:
            if not isinstance(message, dict) or message.get("type") != "assistant":
                continue
            pieces = []
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        pieces.append(str(part.get("text") or ""))
                    elif part.get("type") == "tool":
                        block = self._render_tool(part)
                        if block:
                            pieces.append(block)
            return "".join(pieces).strip()
        return ""

    def _render_tool_event(self, data: dict[str, Any]) -> str:
        status = "error" if data.get("error") else "completed"
        state = {"status": status, "input": data.get("input") or {}}
        if status == "error":
            state["error"] = str(data.get("error"))
        elif isinstance(data.get("content"), list):
            state["content"] = data["content"]
        part = {
            "type": "tool",
            "tool": data.get("name") or "tool",
            "callID": data.get("id") or "tool",
            "state": state,
        }
        return self._render_tool(part)

    def _event_reader(self, client: OpenCodeClient, stop: threading.Event, events: queue.Queue) -> None:
        try:
            for line in client.event_lines(stop):
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                try:
                    events.put_nowait(payload)
                except queue.Full:
                    try:
                        events.put_nowait({"type": "engine.error", "error": "event queue overflow"})
                    except queue.Full:
                        pass
        except Exception as exc:  # noqa: BLE001
            try:
                events.put_nowait({"type": "engine.error", "error": str(exc)})
            except queue.Full:
                pass
        finally:
            try:
                events.put_nowait(None)
            except queue.Full:
                pass

    def _consume_events(self, client: OpenCodeClient, session_id: str, events: queue.Queue, emit: Any) -> None:
        seen: dict[tuple[str, int], str] = {}
        tool_meta: dict[str, dict[str, Any]] = {}
        deadline = time.time() + 900.0
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                client.abort(session_id)
                raise EngineError(504, "OpenCode turn timed out")
            try:
                event = events.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                continue
            if event is None:
                break
            etype = event.get("type")
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if etype == "engine.error":
                raise EngineError(502, "OpenCode event stream failed: {}".format(event.get("error")))
            if etype == "permission.asked":
                if data.get("sessionID") != session_id:
                    continue
                permission_id = str(data.get("id") or "")
                if permission_id:
                    client.answer_permission(session_id, permission_id, "reject")
                    emit(
                        "\n\n[Sparknet: a sandbox permission request was refused; "
                        "the action was outside the agent's allowed scope.]\n\n"
                    )
                continue
            event_session = data.get("sessionID")
            if event_session not in (None, session_id):
                continue
            if etype == "session.text.delta":
                delta = data.get("delta")
                if isinstance(delta, str) and delta:
                    emit(delta)
                    key = (str(data.get("assistantMessageID") or ""), int(data.get("ordinal") or 0))
                    seen[key] = seen.get(key, "") + delta
            elif etype == "session.text.ended":
                full = data.get("text")
                if isinstance(full, str) and full:
                    key = (str(data.get("assistantMessageID") or ""), int(data.get("ordinal") or 0))
                    previous = seen.get(key, "")
                    addition = full[len(previous) :] if full.startswith(previous) else full
                    if addition:
                        emit(addition)
                    seen[key] = full
            elif etype == "session.tool.called":
                tool_id = str(data.get("id") or "")
                if tool_id:
                    tool_meta[tool_id] = {"name": data.get("name"), "input": data.get("input")}
            elif etype in ("session.tool.success", "session.tool.failed"):
                tool_id = str(data.get("id") or "")
                meta = tool_meta.get(tool_id, {})
                merged = dict(data)
                merged.setdefault("name", meta.get("name"))
                merged.setdefault("input", meta.get("input"))
                block = self._render_tool_event(merged)
                if block:
                    emit(block)
            elif etype == "session.execution.failed":
                emit(f"\n\n[Sparknet: the sandbox turn failed: {html.escape(str(data.get('error')))}\n\n")
                break
            elif etype in ("session.execution.succeeded", "session.execution.interrupted"):
                break

    def _collect_turn(self, client: OpenCodeClient, session_id: str, text: str) -> str:
        stop = threading.Event()
        events: queue.Queue = queue.Queue(maxsize=10000)
        thread = threading.Thread(
            target=self._event_reader, args=(client, stop, events), name="opencode-events", daemon=True
        )
        thread.start()
        pieces: list[str] = []
        try:
            client.prompt(session_id, text)
            self._consume_events(client, session_id, events, pieces.append)
        finally:
            stop.set()
            thread.join(timeout=3)
        rendered = self._render_messages(client.messages(session_id))
        return rendered or "".join(pieces).strip()


class StreamingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sparknet-opencode-engine/1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log(f"{self.address_string()} {fmt % args}")

    @property
    def engine(self) -> Engine:
        return self.server.engine  # type: ignore[attr-defined]

    def _json(self, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": {"message": message, "type": "sparknet_engine_error"}})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except Exception:
            raise EngineError(400, "request body is not JSON")
        if not isinstance(value, dict):
            raise EngineError(400, "request body must be an object")
        return value

    def _auth(self) -> None:
        expected = self.engine.token
        if not expected:
            raise EngineError(503, "engine token is not configured")
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            raise EngineError(401, "missing bearer token")
        if not _constant_time_equal(header[7:].strip(), expected):
            raise EngineError(403, "bearer token refused")

    def _user(self) -> str:
        return _valid_user(self.headers.get("X-Sparknet-User") or "")

    def _chat_id(self) -> str:
        return str(self.headers.get("X-Sparknet-Chat-Id") or "").strip()[:128]

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/healthz":
                self._json(
                    200,
                    {
                        "ok": True,
                        "role": self.engine.args.role,
                        "model": self.engine.model_id,
                        "state": str(self.engine.sandbox.state_root),
                    },
                )
                return
            if path == "/v1/models":
                self._json(200, {"object": "list", "data": self.engine.models()})
                return
            self._error(404, "unknown path")
        except EngineError as exc:
            self._error(exc.status, exc.message)
        except Exception as exc:  # noqa: BLE001
            log(f"GET {path} failed: {exc}")
            self._error(500, "engine failure")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/v1/chat/completions":
                self._auth()
                body = self._body()
                user = self._user()
                chat_id = self._chat_id()
                model = str(body.get("model") or "")
                stream = bool(body.get("stream"))
                if stream:
                    self._stream_chat(body, user, chat_id, model)
                else:
                    completion, _ = self.engine.chat(body, user, chat_id)
                    self._json(200, completion)
                return
            self._error(404, "unknown path")
        except EngineError as exc:
            self._error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # noqa: BLE001
            log(f"POST {path} failed: {exc}")
            self._error(500, "engine failure")

    def _stream_chat(self, body: dict[str, Any], user: str, chat_id: str, model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        completion_id = f"chatcmpl-{hashlib.sha256((user + chat_id + str(time.time())).encode()).hexdigest()[:24]}"
        created = int(time.time())

        def chunk(content: str, finish: str | None = None) -> dict[str, Any]:
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": ({"content": content} if content else {}),
                        "finish_reason": finish,
                    }
                ],
            }

        def emit(content: str) -> None:
            if not content:
                return
            self.wfile.write(("data: " + json.dumps(chunk(content), ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        try:
            # The engine's chat() streams through a generator-like callback. The
            # implementation below inlines that contract so a client disconnect
            # can abort the OpenCode turn.
            self._run_stream(body, user, chat_id, model, emit)
            self.wfile.write(("data: " + json.dumps(chunk("", "stop"), ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except EngineError as exc:
            try:
                self.wfile.write(
                    ("data: " + json.dumps({"error": {"message": exc.message}}, ensure_ascii=False) + "\n\n").encode(
                        "utf-8"
                    )
                )
                self.wfile.flush()
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            log(f"stream failed: {exc}")
            try:
                self.wfile.write(
                    (
                        "data: "
                        + json.dumps({"error": {"message": "engine stream failure"}}, ensure_ascii=False)
                        + "\n\n"
                    ).encode("utf-8")
                )
                self.wfile.flush()
            except Exception:
                pass

    def _run_stream(
        self,
        body: dict[str, Any],
        user: str,
        chat_id: str,
        model: str,
        emit: Any,
    ) -> None:
        agent = self.engine._agent(model)
        text = self.engine._last_user_text(body.get("messages"))
        if not text:
            raise EngineError(400, "messages must contain a user text message")
        system = self.engine._system_text(body.get("messages"))
        if system:
            text = f"{system}\n\nUser request:\n{text}"
        sandbox = self.engine.sandbox.ensure(user)
        self.engine.sandbox.touch(user)
        client = OpenCodeClient(sandbox.base_url, sandbox.password)
        if not client.health():
            raise EngineError(503, "OpenCode sandbox is not healthy")
        session_id = self.engine._session_for(
            user, chat_id, "Sparknet %s" % (chat_id or user), agent, self.engine.model_id
        )
        stop = threading.Event()
        events: queue.Queue = queue.Queue(maxsize=10000)
        thread = threading.Thread(
            target=self.engine._event_reader, args=(client, stop, events), name="opencode-events", daemon=True
        )
        thread.start()
        try:
            client.prompt(session_id, text)
            self.engine._consume_events(client, session_id, events, emit)
        except (BrokenPipeError, ConnectionResetError):
            # A closed chat must stop the turn; otherwise tool calls keep
            # mutating the vault after the user is gone.
            try:
                client.abort(session_id)
            except Exception:
                pass
            raise
        finally:
            stop.set()
            thread.join(timeout=3)


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sparknet-opencode-engine", description="Sparknet OpenCode engine for Open WebUI")
    p.add_argument("--bind", default=os.environ.get("SPARKNET_OPENCODE_ENGINE_BIND", DEFAULT_BIND))
    p.add_argument("--role", choices=("peer", "master"), default=os.environ.get("SPARKNET_ROLE", "peer"))
    p.add_argument("--knowledge-root", default=os.environ.get("SPARKNET_KNOWLEDGE_ROOT", "/srv/sparknet/knowledge"))
    p.add_argument("--single-vault", default=os.environ.get("SPARKNET_OPENCODE_SINGLE_VAULT", ""))
    p.add_argument(
        "--state-root", default=os.environ.get("SPARKNET_OPENCODE_ENGINE_STATE", "/srv/sparknet/opencode-engine")
    )
    p.add_argument("--token-file", default=os.environ.get("SPARKNET_OPENCODE_ENGINE_TOKEN_FILE", ""))
    p.add_argument("--token", default=os.environ.get("SPARKNET_OPENCODE_ENGINE_TOKEN", ""))
    p.add_argument("--image", default=os.environ.get("SPARKNET_OPENCODE_IMAGE", "sparknet-opencode-runtime:2.2.5"))
    default_docker = "/usr/bin/docker" if os.geteuid() == 0 else os.environ.get("SPARKNET_DOCKER", "docker")
    p.add_argument("--docker", default=default_docker)
    p.add_argument(
        "--port-base", type=int, default=int(os.environ.get("SPARKNET_OPENCODE_PORT_BASE", DEFAULT_PORT_BASE))
    )
    p.add_argument(
        "--proxy-port", type=int, default=int(os.environ.get("SPARKNET_OPENCODE_PROXY_PORT", DEFAULT_PROXY_PORT))
    )
    p.add_argument("--model-upstream", default=os.environ.get("SPARKNET_MODEL_UPSTREAM", "http://127.0.0.1:11434"))
    p.add_argument("--model-id", default=os.environ.get("SPARKNET_OPENCODE_MODEL", "qwen3.6-35b-262k-q8:latest"))
    p.add_argument("--small-model", default=os.environ.get("SPARKNET_OPENCODE_SMALL_MODEL", "gemma4-4b-131k:latest"))
    p.add_argument(
        "--config-template",
        default=os.environ.get(
            "SPARKNET_OPENCODE_CONFIG_TEMPLATE",
            str(Path(__file__).resolve().parent.parent / "configs" / "opencode-engine" / "opencode.json.template"),
        ),
    )
    p.add_argument("--sandbox-memory", default=os.environ.get("SPARKNET_OPENCODE_SANDBOX_MEMORY", "2g"))
    p.add_argument("--sandbox-cpus", default=os.environ.get("SPARKNET_OPENCODE_SANDBOX_CPUS", "2"))
    p.add_argument("--idle-seconds", type=int, default=int(os.environ.get("SPARKNET_OPENCODE_IDLE_SECONDS", "1800")))
    p.add_argument("--no-prune", action="store_true", help="disable the idle sandbox cleanup loop")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    host, _, port_text = args.bind.rpartition(":")
    if not host:
        host = "127.0.0.1"
    port = int(port_text)
    if args.single_vault and args.role != "master":
        raise SystemExit("--single-vault is only valid with --role master")
    engine = Engine(args)
    if args.no_prune:
        engine.prune_stop.set()
    server = ThreadingHTTPServer((host, port), StreamingHandler)
    server.engine = engine  # type: ignore[attr-defined]

    def _stop(_signum: int, _frame: Any) -> None:
        log("shutting down")
        engine.close()
        # A handler may be blocked on an OpenCode turn. Shut the listener down
        # from a daemon thread so SIGTERM does not wait for that turn; the
        # process is a supervisor, not the owner of the user's session.
        threading.Thread(target=server.shutdown, name="engine-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    server.daemon_threads = True
    log(f"Sparknet OpenCode engine on {host}:{port} (role={args.role}, model={args.model_id})")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
