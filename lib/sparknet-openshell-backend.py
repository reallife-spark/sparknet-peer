#!/usr/bin/env python3
"""OpenShell microVM backend: CLI lifecycle, forward, proxy and owner checks.

The backend owns the VM-specific state and receives the engine's manager for
vault, state, config and password helpers. It is loaded by path through
shared/sparknet_loader.py; it is a role library, not a service.
"""

from __future__ import annotations

import base64
import json
import os
import pwd
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class OpenShellForward:
    """One `openshell forward service` process for a VM sandbox.

    The forward carries Basic auth end-to-end, unlike a service URL with the
    default `strip` authorization mode. It binds a loopback port that the
    engine's HTTP client uses directly.
    """

    def __init__(self, argv: list[str], port: int, log_path: Path, error_cls: type[RuntimeError]) -> None:
        self.argv = argv
        self.port = port
        self.log_path = log_path
        self.error_cls = error_cls
        self.proc: subprocess.Popen[bytes] | None = None
        self._log: Any = None

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "ab")  # noqa: SIM115 - held for the process lifetime
        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self._close_log()
            raise self.error_cls(503, f"cannot start openshell forward: {exc}") from exc
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                detail = self._tail()
                self.stop()
                raise self.error_cls(503, f"openshell forward exited: {detail}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.2)
        self.stop()
        raise self.error_cls(503, f"openshell forward did not bind 127.0.0.1:{self.port}")

    def _tail(self) -> str:
        try:
            data = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return data[-800:].strip()

    def _close_log(self) -> None:
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._close_log()


class OpenShellBackend:
    """The VM driver path for one engine. The manager keeps the shared helpers."""

    def __init__(
        self,
        manager: Any,
        profile: Any,
        error_cls: type[RuntimeError],
        proxy_thread_cls: Any,
    ) -> None:
        self.m = manager
        self.profile = profile
        self.error_cls = error_cls
        self.proxy_thread_cls = proxy_thread_cls
        self.forwards: dict[str, OpenShellForward] = {}

    def _openshell_prefix(self) -> list[str]:
        """The CLI prefix, dropping to the owner when the engine runs as root."""
        if self.m.openshell_user and os.geteuid() == 0:
            try:
                home = pwd.getpwnam(self.m.openshell_user).pw_dir
            except KeyError as exc:
                raise self.error_cls(500, f"OpenShell user {self.m.openshell_user!r} does not exist") from exc
            return ["runuser", "-u", self.m.openshell_user, "--", "env", f"HOME={home}", self.m.openshell]
        return [self.m.openshell]

    def _openshell_owner_path(self) -> Path:
        if self.m.openshell_owner_file is not None:
            return self.m.openshell_owner_file
        if self.m.openshell_user and os.geteuid() == 0:
            try:
                home = Path(pwd.getpwnam(self.m.openshell_user).pw_dir)
            except KeyError as exc:
                raise self.error_cls(500, f"OpenShell user {self.m.openshell_user!r} does not exist") from exc
        else:
            home = Path.home()
        return home / ".config" / "openshell" / "owner.json"

    def _read_gateway_owner(self) -> dict[str, Any]:
        """Read the renderer's owner identity and require it to match this engine."""
        path = self._openshell_owner_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise self.error_cls(
                503,
                f"OpenShell owner manifest missing or invalid at {path}: {exc}; "
                "run scripts/sparknet-openshell-config.py",
            ) from exc
        if data.get("driver") != "vm":
            raise self.error_cls(503, f"OpenShell owner manifest {path} does not select the vm driver")
        try:
            uid, gid = int(data["uid"]), int(data["gid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise self.error_cls(503, f"OpenShell owner manifest {path} has no numeric uid/gid") from exc
        if (uid, gid) != (self.m.sandbox_uid, self.m.sandbox_gid):
            raise self.error_cls(
                503,
                f"OpenShell gateway owner {uid}:{gid} from {path} does not match the engine owner "
                f"{self.m.sandbox_uid}:{self.m.sandbox_gid}; re-render the gateway config",
            )
        return {"uid": uid, "gid": gid, "path": path}

    def _openshell_workload_identity(self, name: str) -> tuple[int, int] | None:
        try:
            out = self._run_openshell(["sandbox", "get", name, "-o", "json"], timeout=30)
            data = json.loads(out)
        except (self.error_cls, json.JSONDecodeError):
            return None
        for condition in data.get("conditions", []):
            if isinstance(condition, dict) and condition.get("type") == "WorkloadIdentity":
                match = re.search(r"(\d+):(\d+)", str(condition.get("message", "")))
                if match:
                    return int(match.group(1)), int(match.group(2))
        return None

    def _run_openshell(self, args: list[str], timeout: float = 120) -> str:
        argv = self._openshell_prefix() + list(args)
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise self.error_cls(500, f"OpenShell CLI not found: {self.m.openshell}") from exc
        except subprocess.TimeoutExpired as exc:
            raise self.error_cls(503, f"OpenShell {' '.join(args[:2])} timed out") from exc
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout).strip().splitlines()
            detail = lines[-1] if lines else "no output"
            raise self.error_cls(503, f"OpenShell {' '.join(args[:2])} failed: {detail}")
        return proc.stdout

    def _openshell_phase(self, name: str) -> str | None:
        try:
            out = self._run_openshell(["sandbox", "get", name, "-o", "json"], timeout=30)
        except self.error_cls:
            return None
        try:
            return str(json.loads(out).get("phase") or "")
        except json.JSONDecodeError:
            return None

    def _openshell_error(self, name: str) -> str:
        try:
            out = self._run_openshell(["sandbox", "get", name, "-o", "json"], timeout=30)
            data = json.loads(out)
        except (self.error_cls, json.JSONDecodeError):
            return ""
        messages = [
            str(item.get("message") or "")
            for item in data.get("conditions", [])
            if isinstance(item, dict) and item.get("status") == "False"
        ]
        return "; ".join(message for message in messages if message)

    def _wait_openshell_ready(self, name: str, timeout: float = 600) -> None:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            phase = self._openshell_phase(name)
            if phase == "Ready":
                return
            if phase == "Error":
                raise self.error_cls(503, f"OpenShell sandbox {name} failed: {self._openshell_error(name)}")
            last = phase or "unknown"
            time.sleep(2)
        raise self.error_cls(503, f"OpenShell sandbox {name} did not become Ready: {last}")

    def ensure_vm_proxy(self, user: str, row: dict[str, Any]) -> dict[str, Any]:
        """One host-loopback model proxy per VM sandbox.

        The VM supervisor reaches the host as 127.0.0.1, so the proxy binds
        loopback and accepts only loopback clients. The guest config uses the
        `host.openshell.internal` alias; the egress policy names the same port.
        """
        port = self.m.proxy_port + int(row["index"])
        key = f"vm:{user}"
        with self.m.lock:
            proxy = self.m.proxies.get(key)
            if proxy is None:
                try:
                    proxy = self.proxy_thread_cls("127.0.0.1", port, self.m.model_upstream, "127.0.0.1/32")
                except OSError as exc:
                    raise self.error_cls(503, f"cannot bind VM model proxy on 127.0.0.1:{port}: {exc}") from exc
                proxy.start()
                self.m.proxies[key] = proxy
        return {"port": port, "guest_url": f"http://host.openshell.internal:{port}/v1"}

    def _ensure_forward(self, user: str, name: str, row: dict[str, Any]) -> OpenShellForward:
        with self.m.lock:
            forward = self.forwards.get(user)
            if forward is not None and forward.proc is not None and forward.proc.poll() is None:
                return forward
            port = self.m.forward_base + int(row["index"])
            argv = self._openshell_prefix() + [
                "forward",
                "service",
                name,
                "--target-port",
                str(self.profile.OPENCODE_PORT),
                "--local",
                str(port),
            ]
            forward = OpenShellForward(
                argv,
                port,
                self.m.user_state(user) / "forward.log",
                self.error_cls,
            )
            forward.start()
            self.forwards[user] = forward
            return forward

    def _create_openshell(self, name: str, policy: Path, driver_json: str, command: str) -> None:
        self._run_openshell(
            [
                "sandbox",
                "create",
                "--name",
                name,
                "--from",
                self.m.vm_image,
                "--policy",
                str(policy),
                "--driver-config-json",
                driver_json,
                "--no-auto-providers",
                "--detach",
                "--",
                "sh",
                "-lc",
                command,
            ],
            timeout=900,
        )

    def _wait_health_url(self, url: str, password: str, timeout: float = 180) -> None:
        deadline = time.time() + timeout
        request_url = url.rstrip("/") + "/api/info"
        while time.time() < deadline:
            request = urllib.request.Request(request_url)
            if password:
                token = base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
                request.add_header("Authorization", "Basic " + token)
            try:
                with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback health probe
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(1)
        raise self.error_cls(503, "OpenShell sandbox did not become healthy")

    def _ensure_openshell(self, user: str) -> Any:
        self._read_gateway_owner()
        vault = self.m.vault_for(user)
        self.m._own_vault(user, vault)
        row = self.m.allocator.allocate(user)
        state = self.m.user_state(user)
        proxy = self.ensure_vm_proxy(user, row)
        vm_dir = state / "vm"
        vm_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(vm_dir, 0o700)
        self.m._config_for(user, proxy["guest_url"], directory=vm_dir)
        password = self.m.password_for(user)
        env_file = vm_dir / "opencode.env"
        env_file.write_text(
            "\n".join(
                [
                    f"OPENCODE_SERVER_PASSWORD={password}",
                    f"OPENCODE_CONFIG={self.profile.VM_CONFIG_TARGET}/opencode.json",
                    f"HOME={self.profile.WORKSPACE}",
                    f"XDG_CONFIG_HOME={self.profile.WORKSPACE}/.config",
                    f"XDG_DATA_HOME={self.profile.WORKSPACE}/.local/share",
                    f"XDG_CACHE_HOME={self.profile.WORKSPACE}/.cache",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.chmod(env_file, 0o600)
        skill_mounts = self.m.skill_mounts()
        policy = self.profile.write_vm_policy(
            vm_dir / "policy.yaml",
            read_only=self.profile.DEFAULT_READ_ONLY
            + (self.profile.VM_CONFIG_TARGET, *[target for _source, target in skill_mounts]),
            model_port=proxy["port"],
        )
        driver = self.profile.build_vm_driver_config(
            vault,
            vm_dir,
            vault_target=self.profile.VM_VAULT_TARGET,
            config_target=self.profile.VM_CONFIG_TARGET,
            extra_mounts=skill_mounts,
        )
        driver_json = json.dumps(driver, separators=(",", ":"))
        self.m._own_path(vm_dir, recursive=True)
        name = self.m.openshell_name(user)
        config_env = f"{self.profile.VM_CONFIG_TARGET}/opencode.env"
        serve = f"exec {self.profile.OPENCODE_BINARY} serve --hostname 127.0.0.1 --port {self.profile.OPENCODE_PORT}"
        command = f"set -a; . {config_env}; set +a; cd {self.profile.VM_VAULT_TARGET}; {serve}"
        phase = self._openshell_phase(name)
        if phase is None:
            self._create_openshell(name, policy, driver_json, command)
        elif phase == "Error":
            self._run_openshell(["sandbox", "delete", name], timeout=60)
            self._create_openshell(name, policy, driver_json, command)
        elif phase == "Stopped":
            self._run_openshell(["sandbox", "start", name], timeout=120)
        elif phase not in ("Ready", "Provisioning", "Stopping"):
            raise self.error_cls(503, f"OpenShell sandbox {name} is {phase}")
        self._wait_openshell_ready(name)
        actual = self._openshell_workload_identity(name)
        if actual != (self.m.sandbox_uid, self.m.sandbox_gid):
            try:
                self._run_openshell(["sandbox", "delete", name], timeout=60)
            except self.error_cls as exc:
                print(f"OpenShell identity mismatch cleanup failed for {name}: {exc}", file=sys.stderr)
            raise self.error_cls(
                503,
                f"OpenShell sandbox {name} resolved workload identity {actual}, expected "
                f"{self.m.sandbox_uid}:{self.m.sandbox_gid}; refusing the sandbox",
            )
        forward = self._ensure_forward(user, name, row)
        url = f"http://127.0.0.1:{forward.port}"
        self._wait_health_url(url, password)
        self.m.last_used[user] = time.time()
        return self.m._make_sandbox(
            user,
            name,
            vault,
            proxy["guest_url"],
            password,
            url,
            forward,
        )

    def ensure(self, user: str) -> Any:
        return self._ensure_openshell(user)

    def stop(self, user: str) -> None:
        forward = self.forwards.pop(user, None)
        if forward is not None:
            forward.stop()
        try:
            self._run_openshell(["sandbox", "stop", self.m.openshell_name(user)], timeout=60)
        except self.error_cls as exc:
            print(f"openshell stop failed for {user}: {exc}", file=sys.stderr)
        self.m.last_used.pop(user, None)

    def close(self) -> None:
        for forward in self.forwards.values():
            forward.stop()
        self.forwards.clear()
