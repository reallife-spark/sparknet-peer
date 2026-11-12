"""Identity, policy and mount profile for the peer OpenShell VM sandbox.

The VM is the only OpenShell driver this module profiles. The engine's legacy
Docker supervisor keeps its own numeric selectors; mixing the two here is what
let the contract say `docker-rootful` while the code ran `vm`.

The VM guest uses the symbolic ``sandbox`` account reconciled by the driver to
``sandbox_uid``/``sandbox_gid``. Those must equal the node owner; the engine
reads the renderer's ``owner.json`` and checks the resolved ``WorkloadIdentity``
before accepting a sandbox.

This module is stdlib-only and is loaded by path through
``shared/sparknet_loader.py``. It is a role library, not a service.
"""

from __future__ import annotations

import json
import os
import pwd
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

#: The pre-OpenShell identity the engine's Docker backend still uses.
LEGACY_UID = 10001
LEGACY_GID = 10001

#: The microVM workspace and mount targets.
WORKSPACE = "/sandbox"
VM_VAULT_TARGET = "/work"
VM_CONFIG_TARGET = "/sandbox/.sparknet"

#: OpenShell VM mount targets that must never be replaced.
VM_RESERVED_TARGETS = (
    "/",
    "/sandbox",
    "/tmp",
    "/proc",
    "/dev",
    "/srv",
    "/opt/openshell",
    "/etc/openshell",
    "/run/openshell",
)

#: The OpenShell model-proxy base and the OpenCode guest port.
DEFAULT_MODEL_PROXY_PORT = 11501
OPENCODE_PORT = 4200
OPENCODE_BINARY = "/usr/local/bin/opencode"
MODEL_HOST = "host.openshell.internal"

DEFAULT_READ_ONLY = ("/usr", "/lib", "/lib64", "/etc", "/opt/sparknet")

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "sandbox-schema.json"


class ProfileError(RuntimeError):
    """Raised when an identity, policy or mount would violate the contract."""


def _load_uid_bounds() -> tuple[int, int]:
    """Read the one UID range from the contract instead of hardcoding it."""
    try:
        data = json.loads(_CONTRACT.read_text(encoding="utf-8"))
        return int(data["identity"]["uid_min"]), int(data["identity"]["uid_max"])
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ProfileError(f"cannot load sandbox UID bounds from {_CONTRACT}: {exc}") from exc


UID_MIN, UID_MAX = _load_uid_bounds()


def _parse_id(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProfileError(f"{name} must be an integer or numeric string")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"{name} must be an integer or numeric string") from exc
    if parsed < UID_MIN or parsed > UID_MAX:
        raise ProfileError(f"{name} must be in {UID_MIN}..{UID_MAX}; OpenShell refuses 0")
    return parsed


def resolve_owner_identity(
    *,
    uid: object | None = None,
    gid: object | None = None,
    user: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, int]:
    """Return the numeric owner identity the VM must reconcile to.

    Explicit arguments win. Otherwise ``SPARKNET_OWNER_UID``/``GID`` are used,
    then ``SPARKNET_OWNER_USER`` is looked up in the local passwd database. A
    partial pair is refused: taking the UID from one source and the GID from
    another can silently run the workload under the wrong primary group.
    """
    source = os.environ if env is None else env
    if uid is not None or gid is not None:
        if uid is None or gid is None:
            raise ProfileError("owner identity needs both uid and gid")
        return _parse_id(uid, name="uid"), _parse_id(gid, name="gid")

    env_uid = source.get("SPARKNET_OWNER_UID")
    env_gid = source.get("SPARKNET_OWNER_GID")
    if bool(env_uid) != bool(env_gid):
        raise ProfileError("SPARKNET_OWNER_UID and SPARKNET_OWNER_GID must be set together")
    if env_uid and env_gid:
        return _parse_id(env_uid, name="SPARKNET_OWNER_UID"), _parse_id(env_gid, name="SPARKNET_OWNER_GID")

    name = user or source.get("SPARKNET_OWNER_USER")
    if not name:
        raise ProfileError("no owner identity: set SPARKNET_OWNER_UID/GID or SPARKNET_OWNER_USER")
    try:
        record = pwd.getpwnam(name)
    except KeyError as exc:
        raise ProfileError(f"owner user {name!r} does not exist on this host") from exc
    return _parse_id(record.pw_uid, name="owner uid"), _parse_id(record.pw_gid, name="owner gid")


def legacy_identity() -> tuple[int, int]:
    """The pre-OpenShell synthetic identity, used by the Docker backend."""
    return LEGACY_UID, LEGACY_GID


def _validate_path(path: str, *, read_write: bool) -> str:
    if not isinstance(path, str) or not path:
        raise ProfileError("policy path must be a non-empty string")
    if not path.startswith("/"):
        raise ProfileError(f"policy path must be absolute: {path!r}")
    if ".." in Path(path).parts:
        raise ProfileError(f"policy path must not contain '..': {path!r}")
    if os.path.normpath(path) != path:
        raise ProfileError(f"policy path must be normalised: {path!r}")
    if read_write and path == "/":
        raise ProfileError("read_write must not contain '/'")
    if len(path.encode("utf-8")) > 4096:
        raise ProfileError("policy path exceeds 4096 bytes")
    return path


def _yaml_scalar(value: object) -> str:
    """Quote a value as a JSON string, which is valid YAML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value), ensure_ascii=True)


def build_vm_policy(
    *,
    read_only: Iterable[str] = DEFAULT_READ_ONLY + (VM_CONFIG_TARGET,),
    read_write: Iterable[str] = (VM_VAULT_TARGET, "/tmp"),
    model_host: str = MODEL_HOST,
    model_port: int = DEFAULT_MODEL_PROXY_PORT,
    binary: str = OPENCODE_BINARY,
) -> str:
    """Render the microVM policy YAML with the symbolic sandbox account."""
    ro = [_validate_path(p, read_write=False) for p in read_only]
    rw = [_validate_path(p, read_write=True) for p in read_write]
    if len(ro) + len(rw) > 256:
        raise ProfileError("policy lists more than 256 paths")
    if not isinstance(model_port, int) or not 1 <= model_port <= 65535:
        raise ProfileError("model_port must be a TCP port")
    if not isinstance(model_host, str) or not model_host:
        raise ProfileError("model_host must be a non-empty string")
    if not isinstance(binary, str) or not binary.startswith("/"):
        raise ProfileError("binary must be an absolute path")
    lines = [
        "version: 1",
        "process:",
        f"  run_as_user: {_yaml_scalar('sandbox')}",
        f"  run_as_group: {_yaml_scalar('sandbox')}",
        "filesystem_policy:",
        "  include_workdir: true",
        "  read_only:",
        *[f"    - {_yaml_scalar(p)}" for p in ro],
        "  read_write:",
        *[f"    - {_yaml_scalar(p)}" for p in rw],
        "landlock:",
        "  compatibility: hard_requirement",
        "network_policies:",
        "  model_proxy:",
        "    endpoints:",
        f"      - host: {_yaml_scalar(model_host)}",
        f"        port: {model_port}",
        "        protocol: rest",
        "        enforcement: enforce",
        "        access: full",
        "    binaries:",
        f"      - path: {_yaml_scalar(binary)}",
        "",
    ]
    return "\n".join(lines)


def validate_vm_mount_target(target: str, *, workspace: str = WORKSPACE) -> str:
    """Refuse a microVM bind target that hits a reserved path or nests mounts."""
    if not isinstance(target, str) or not target.startswith("/"):
        raise ProfileError("VM mount target must be absolute")
    normal = os.path.normpath(target)
    if normal != target:
        raise ProfileError(f"VM mount target must be normalised: {target!r}")
    if normal in VM_RESERVED_TARGETS:
        raise ProfileError(f"VM mount target is reserved: {target!r}")
    for reserved in VM_RESERVED_TARGETS:
        # Subdirectories of the workspace are the documented VM mount shape;
        # the workspace root itself is not.
        if reserved in ("/", workspace):
            continue
        if normal.startswith(reserved + "/"):
            raise ProfileError(f"VM mount target is under a reserved path: {target!r}")
    if workspace.startswith(normal + "/"):
        raise ProfileError(f"VM mount target must not contain the workspace root: {target!r}")
    return normal


def build_vm_driver_config(
    vault: Path | str,
    config_dir: Path | str,
    *,
    vault_target: str = VM_VAULT_TARGET,
    config_target: str = VM_CONFIG_TARGET,
    workspace: str = WORKSPACE,
    extra_mounts: Iterable[tuple[Path | str, str]] = (),
) -> dict[str, dict[str, list[dict[str, object]]]]:
    """Return the VM driver-config JSON for the vault and config directory.

    MicroVM bind sources must be host directories, not single files. The engine
    writes ``opencode.json`` and ``opencode.env`` into ``config_dir``; that
    directory is mounted read-only at ``/sandbox/.sparknet``.
    """
    vault_path = Path(vault)
    config_path = Path(config_dir)
    for name, path in (("vault", vault_path), ("config", config_path)):
        if not path.is_absolute():
            raise ProfileError(f"VM {name} path must be absolute: {path!r}")
        if not path.is_dir():
            raise ProfileError(f"VM {name} path is not a directory: {path!r}")
    vault_mount = validate_vm_mount_target(vault_target, workspace=workspace)
    config_mount = validate_vm_mount_target(config_target, workspace=workspace)
    for left, right in ((vault_mount, config_mount), (config_mount, vault_mount)):
        if left == right or left.startswith(right + "/"):
            raise ProfileError("VM mounts must not be nested or identical")
    mounts: list[dict[str, object]] = [
        {"type": "bind", "source": str(vault_path), "target": vault_mount, "read_only": False},
        {"type": "bind", "source": str(config_path), "target": config_mount, "read_only": True},
    ]
    for source, target in extra_mounts:
        source_path = Path(source)
        if not source_path.is_absolute() or not source_path.is_dir():
            raise ProfileError(f"VM extra mount source is not an absolute directory: {source!r}")
        normal = validate_vm_mount_target(target, workspace=workspace)
        if any(normal == str(row["target"]) or normal.startswith(str(row["target"]) + "/") for row in mounts):
            raise ProfileError(f"VM extra mount nests an existing mount: {normal!r}")
        mounts.append({"type": "bind", "source": str(source_path), "target": normal, "read_only": True})
    return {"vm": {"mounts": mounts}}


def write_vm_policy(
    path: Path | str,
    *,
    read_only: Iterable[str] = DEFAULT_READ_ONLY + (VM_CONFIG_TARGET,),
    read_write: Iterable[str] = (VM_VAULT_TARGET, "/tmp"),
    model_port: int = DEFAULT_MODEL_PROXY_PORT,
) -> Path:
    """Write the VM policy 0600 and return its path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        build_vm_policy(read_only=read_only, read_write=read_write, model_port=model_port),
        encoding="utf-8",
    )
    os.chmod(target, 0o600)
    return target


def chown_no_follow(path: Path | str, uid: int, gid: int, *, recursive: bool = False) -> None:
    """Chown a path and, optionally, its children without following symlinks.

    Fail closed: collect every failure and raise one ``ProfileError``. A
    sandbox-created symlink is never followed; a chown of a host file through a
    vault symlink would hand the sandbox a file it does not own.
    """
    root = Path(path)
    target_uid = _parse_id(uid, name="uid")
    target_gid = _parse_id(gid, name="gid")
    failures: list[str] = []

    def one(item: Path) -> None:
        if item.is_symlink() or not item.exists():
            return
        try:
            os.chown(item, target_uid, target_gid, follow_symlinks=False)
        except OSError as exc:
            failures.append(f"{item}: {exc}")

    one(root)
    if recursive and root.is_dir() and not root.is_symlink():
        for current, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(current)
            for name in dirnames + filenames:
                one(base / name)
    if failures:
        shown = "; ".join(failures[:5])
        raise ProfileError(f"chown failed for {len(failures)} path(s): {shown}")


def migrate_ownership(
    paths: Iterable[Path | str],
    uid: int,
    gid: int,
    marker: Path | str,
) -> bool:
    """Re-own the listed trees once, guarded by a marker file.

    The marker is written only after every chown succeeded. A partial migration
    is reported and retried on the next start; it is never recorded as done.
    """
    target_uid = _parse_id(uid, name="uid")
    target_gid = _parse_id(gid, name="gid")
    marker_path = Path(marker)
    if marker_path.exists():
        return False
    if os.geteuid() != 0 and (target_uid, target_gid) != (os.geteuid(), os.getegid()):
        raise ProfileError("only root can migrate a vault to a different owner")

    for raw in paths:
        chown_no_follow(raw, target_uid, target_gid, recursive=True)

    marker_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "uid": target_uid,
        "gid": target_gid,
        "migrated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    temporary = marker_path.with_name(marker_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, marker_path)
    return True
