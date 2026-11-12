"""The peer sandbox profile and the OpenShell VM identity contract.

The VM guest uses the symbolic `sandbox` account reconciled to the owner UID/GID.
These tests pin the policy/mount shape, the contract-sourced UID range, the
fail-closed migration, the owner renderer and the engine's owner checks.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from sparknet_loader import load_by_path

VAULT_ROOT = Path(__file__).resolve().parent.parent
profile = load_by_path("sparknet-sandbox-profile-test", VAULT_ROOT / "lib" / "sparknet-sandbox-profile.py")
ENGINE = load_by_path("engine_profile_test", VAULT_ROOT / "services" / "sparknet-opencode-engine.py")
BACKEND = load_by_path(
    "openshell_backend_test",
    VAULT_ROOT / "lib" / "sparknet-openshell-backend.py",
)
SandboxManager = ENGINE.SandboxManager


def test_resolve_explicit_identity() -> None:
    assert profile.resolve_owner_identity(uid="1000", gid="1000") == (1000, 1000)


def test_resolve_env_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARKNET_OWNER_UID", "1500")
    monkeypatch.setenv("SPARKNET_OWNER_GID", "1600")
    monkeypatch.delenv("SPARKNET_OWNER_USER", raising=False)
    assert profile.resolve_owner_identity() == (1500, 1600)


def test_resolve_user_identity() -> None:
    me = pwd.getpwuid(os.getuid())
    assert profile.resolve_owner_identity(user=me.pw_name) == (me.pw_uid, me.pw_gid)


def test_resolve_refuses_partial_pair() -> None:
    with pytest.raises(profile.ProfileError, match="both uid and gid"):
        profile.resolve_owner_identity(uid=1000)


def test_resolve_refuses_root_and_missing_identity() -> None:
    with pytest.raises(profile.ProfileError, match="refuses 0"):
        profile.resolve_owner_identity(uid=0, gid=0)
    with pytest.raises(profile.ProfileError, match="no owner identity"):
        profile.resolve_owner_identity(env={})


def test_uid_bounds_come_from_the_contract() -> None:
    contract = json.loads((VAULT_ROOT.parent / "contracts" / "sandbox-schema.json").read_text(encoding="utf-8"))
    assert (profile.UID_MIN, profile.UID_MAX) == (
        contract["identity"]["uid_min"],
        contract["identity"]["uid_max"],
    )


def test_build_vm_policy_uses_the_guest_sandbox_identity() -> None:
    policy = profile.build_vm_policy(model_port=11501)
    assert 'run_as_user: "sandbox"' in policy
    assert 'run_as_group: "sandbox"' in policy
    assert f'- "{profile.VM_VAULT_TARGET}"' in policy
    assert f'- "{profile.VM_CONFIG_TARGET}"' in policy
    assert "port: 11501" in policy
    assert 'path: "/usr/local/bin/opencode"' in policy


def test_build_vm_policy_rejects_root_read_write_and_traversal() -> None:
    with pytest.raises(profile.ProfileError, match="read_write must not contain"):
        profile.build_vm_policy(read_write=["/"])
    with pytest.raises(profile.ProfileError, match="absolute"):
        profile.build_vm_policy(read_only=["usr"])
    with pytest.raises(profile.ProfileError, match="\\.\\."):
        profile.build_vm_policy(read_only=["/usr/../etc"])


def test_vm_driver_config_mounts_vault_and_config(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = tmp_path / "config"
    vault.mkdir()
    config.mkdir()
    config_json = profile.build_vm_driver_config(vault, config)
    mounts = config_json["vm"]["mounts"]
    assert len(mounts) == 2
    assert mounts[0] == {
        "type": "bind",
        "source": str(vault),
        "target": profile.VM_VAULT_TARGET,
        "read_only": False,
    }
    assert mounts[1] == {
        "type": "bind",
        "source": str(config),
        "target": profile.VM_CONFIG_TARGET,
        "read_only": True,
    }


def test_vm_mount_target_allows_workspace_subpaths() -> None:
    assert profile.validate_vm_mount_target("/sandbox/.sparknet") == "/sandbox/.sparknet"


def test_vm_mount_target_refuses_reserved_paths() -> None:
    for target in ("/", "/sandbox", "/tmp", "/proc", "/dev", "/srv", "/opt/openshell", "/etc/openshell"):
        with pytest.raises(profile.ProfileError):
            profile.validate_vm_mount_target(target)


def test_vm_driver_config_refuses_nested_mounts(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    config = tmp_path / "config"
    vault.mkdir()
    config.mkdir()
    with pytest.raises(profile.ProfileError, match="nested"):
        profile.build_vm_driver_config(vault, config, config_target="/work/config")


def test_write_vm_policy_is_0600(tmp_path: Path) -> None:
    target = profile.write_vm_policy(tmp_path / "policy.yaml", model_port=11601)
    text = target.read_text()
    assert 'run_as_user: "sandbox"' in text
    assert "port: 11601" in text
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_migration_chowns_without_following_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "peer-user"
    vault.mkdir()
    (vault / "page.md").write_text("hello", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("not yours", encoding="utf-8")
    (vault / "escape").symlink_to(outside, target_is_directory=True)

    calls: list[tuple[str, bool]] = []
    real_chown = os.chown

    def recording_chown(path: object, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
        calls.append((str(path), follow_symlinks))
        real_chown(path, uid, gid, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(profile.os, "chown", recording_chown)
    marker = tmp_path / "state" / "identity.json"
    assert profile.migrate_ownership([vault], os.getuid(), os.getegid(), marker) is True
    assert marker.exists()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert all(follow is False for _path, follow in calls), "a chown followed a symlink"
    assert str(vault / "escape") not in {path for path, _ in calls}


def test_migration_failure_does_not_write_the_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "peer-user"
    vault.mkdir()
    (vault / "page.md").write_text("hello", encoding="utf-8")

    def deny(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(profile.os, "chown", deny)
    marker = tmp_path / "state" / "identity.json"
    with pytest.raises(profile.ProfileError, match="chown failed"):
        profile.migrate_ownership([vault], os.getuid(), os.getegid(), marker)
    assert not marker.exists(), "a partial chown must not be recorded as complete"


def test_migration_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault = tmp_path / "peer-user"
    vault.mkdir()
    marker = tmp_path / "state" / "identity.json"
    assert profile.migrate_ownership([vault], os.getuid(), os.getegid(), marker) is True

    calls: list[str] = []
    monkeypatch.setattr(profile.os, "chown", lambda path, *a, **k: calls.append(str(path)))
    assert profile.migrate_ownership([vault], os.getuid(), os.getegid(), marker) is False
    assert calls == []


def test_gateway_template_has_no_committed_owner_home() -> None:
    template = (VAULT_ROOT / "configs" / "openshell" / "gateway-vm.toml.in").read_text(encoding="utf-8")
    assert "/home/raxxar" not in template
    assert "@OWNER_HOME@" in template and "@OWNER_UID@" in template and "@OWNER_GID@" in template


def test_renderer_binds_owner_and_digest_pins(tmp_path: Path) -> None:
    out = tmp_path / "openshell"
    proc = subprocess.run(
        [
            sys.executable,
            str(VAULT_ROOT / "scripts" / "sparknet-openshell-config.py"),
            "--uid",
            "1500",
            "--gid",
            "1500",
            "--home",
            "/home/testowner",
            "--out-dir",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    gateway = (out / "gateway.toml").read_text(encoding="utf-8")
    assert "/home/testowner" in gateway
    assert "sandbox_uid = 1500" in gateway
    assert "@sha256:" in gateway
    owner = json.loads((out / "owner.json").read_text(encoding="utf-8"))
    assert (owner["uid"], owner["gid"]) == (1500, 1500)
    env = (out / "gateway.env").read_text(encoding="utf-8")
    assert "sandbox@sha256:" in env and "supervisor@sha256:" in env


def test_engine_parser_has_both_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPARKNET_OPENCODE_SANDBOX_BACKEND", raising=False)
    parser = ENGINE._parser()
    assert parser.parse_args([]).sandbox_backend == "docker"
    assert parser.parse_args(["--sandbox-backend", "openshell"]).sandbox_backend == "openshell"


def test_engine_refuses_openshell_without_an_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SPARKNET_OWNER_UID", "SPARKNET_OWNER_GID", "SPARKNET_OWNER_USER"):
        monkeypatch.delenv(name, raising=False)
    dummy = type("Dummy", (), {"sandbox_backend": "openshell"})()
    with pytest.raises(SystemExit, match="requires SPARKNET_OWNER"):
        SandboxManager._resolve_sandbox_identity(dummy)


def test_engine_resolves_the_owner_for_openshell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARKNET_OWNER_UID", "1500")
    monkeypatch.setenv("SPARKNET_OWNER_GID", "1600")
    monkeypatch.delenv("SPARKNET_OWNER_USER", raising=False)
    dummy = type("Dummy", (), {"sandbox_backend": "openshell"})()
    assert SandboxManager._resolve_sandbox_identity(dummy) == (1500, 1600)


def test_engine_keeps_the_legacy_identity_for_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SPARKNET_OWNER_UID", "SPARKNET_OWNER_GID", "SPARKNET_OWNER_USER"):
        monkeypatch.delenv(name, raising=False)
    dummy = type("Dummy", (), {"sandbox_backend": "docker"})()
    assert SandboxManager._resolve_sandbox_identity(dummy) == (profile.LEGACY_UID, profile.LEGACY_GID)


def test_engine_openshell_name_is_short_and_stable() -> None:
    user = "30ad6830-534d-493b-a702-aa0c6d0ee877"
    name = SandboxManager.openshell_name(type("Dummy", (), {})(), user)
    assert name.startswith("spk-")
    assert len(name) <= 19
    assert name == SandboxManager.openshell_name(type("Dummy", (), {})(), user)


def test_engine_owner_manifest_must_match_the_resolved_owner(tmp_path: Path) -> None:
    owner = tmp_path / "owner.json"
    owner.write_text(json.dumps({"driver": "vm", "uid": 1500, "gid": 1500}), encoding="utf-8")
    matching_manager = type("Dummy", (), {"openshell_owner_file": owner, "sandbox_uid": 1500, "sandbox_gid": 1500})()
    backend = BACKEND.OpenShellBackend(matching_manager, profile, ENGINE.EngineError, None)
    assert backend._read_gateway_owner()["uid"] == 1500
    mismatched_manager = type("Dummy", (), {"openshell_owner_file": owner, "sandbox_uid": 1000, "sandbox_gid": 1000})()
    backend = BACKEND.OpenShellBackend(mismatched_manager, profile, ENGINE.EngineError, None)
    with pytest.raises(ENGINE.EngineError, match="does not match the engine owner"):
        backend._read_gateway_owner()


def test_engine_reads_the_workload_identity_condition() -> None:
    payload = {
        "conditions": [
            {"type": "WorkloadIdentity", "message": "Resolved workload UID:GID is 1000:1000"},
        ]
    }

    backend = BACKEND.OpenShellBackend(type("Dummy", (), {})(), profile, ENGINE.EngineError, None)
    backend._run_openshell = lambda *_args, **_kwargs: json.dumps(payload)
    assert backend._openshell_workload_identity("sandbox") == (1000, 1000)


def test_openshell_forward_constructor_takes_the_error_class(tmp_path: Path) -> None:
    forward = BACKEND.OpenShellForward(
        ["openshell", "forward", "service", "sandbox"],
        14301,
        tmp_path / "forward.log",
        ENGINE.EngineError,
    )
    assert forward.port == 14301


def test_engine_openshell_lifecycle_with_fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARKNET_OWNER_UID", "1000")
    monkeypatch.setenv("SPARKNET_OWNER_GID", "1000")
    monkeypatch.delenv("SPARKNET_OWNER_USER", raising=False)
    vaults = tmp_path / "vaults"
    (vaults / "peer-testuser").mkdir(parents=True)
    owner = tmp_path / "owner.json"
    owner.write_text(json.dumps({"driver": "vm", "uid": 1000, "gid": 1000}), encoding="utf-8")
    args = ENGINE._parser().parse_args(
        [
            "--sandbox-backend",
            "openshell",
            "--knowledge-root",
            str(vaults),
            "--state-root",
            str(tmp_path / "state"),
            "--openshell-owner-file",
            str(owner),
            "--model-id",
            "mock-model",
            "--small-model",
            "mock-model",
            "--vm-image",
            "sparknet-opencode-runtime-vm:2.2.5",
            "--no-prune",
        ]
    )
    manager = ENGINE.SandboxManager(args)
    backend = manager.openshell_backend

    class FakeForward:
        def __init__(self) -> None:
            self.port = 14301
            self.proc = None
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    forward = FakeForward()
    created = {"value": False}
    calls: list[list[str]] = []

    def fake_run(argv: list[str], timeout: float = 120) -> str:
        del timeout
        calls.append(list(argv))
        if argv[:2] == ["sandbox", "get"]:
            if not created["value"]:
                raise ENGINE.EngineError(404, "not found")
            return json.dumps(
                {
                    "phase": "Ready",
                    "conditions": [{"type": "WorkloadIdentity", "message": "Resolved workload UID:GID is 1000:1000"}],
                }
            )
        if argv[:2] == ["sandbox", "create"]:
            created["value"] = True
            return ""
        if argv[:2] in (["sandbox", "start"], ["sandbox", "stop"], ["sandbox", "delete"]):
            return ""
        raise AssertionError(argv)

    monkeypatch.setattr(backend, "_run_openshell", fake_run)
    monkeypatch.setattr(
        backend,
        "ensure_vm_proxy",
        lambda _user, _row: {"port": 11601, "guest_url": "http://host.openshell.internal:11601/v1"},
    )
    monkeypatch.setattr(backend, "_ensure_forward", lambda _user, _name, _row: forward)
    monkeypatch.setattr(backend, "_wait_health_url", lambda *_args, **_kwargs: None)

    sandbox = manager.ensure("testuser")
    assert sandbox.base_url == "http://127.0.0.1:14301"
    create = next(call for call in calls if call[:2] == ["sandbox", "create"])
    policy = Path(create[create.index("--policy") + 1]).read_text(encoding="utf-8")
    driver = json.loads(create[create.index("--driver-config-json") + 1])
    assert 'run_as_user: "sandbox"' in policy
    assert "port: 11601" in policy
    mounts = {row["target"]: row for row in driver["vm"]["mounts"]}
    assert set(mounts) == {
        "/work",
        "/sandbox/.sparknet",
        "/opt/sparknet/skill-catalog/base",
        "/opt/sparknet/skill-catalog/role",
        "/opt/sparknet/shared",
    }
    assert all(mounts[target]["read_only"] is True for target in mounts if target != "/work")
    backend.forwards["testuser"] = forward
    manager.stop("testuser")
    assert forward.stopped is True
    manager.close()


def test_engine_refuses_a_vm_owner_mismatch_before_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPARKNET_OWNER_UID", "1000")
    monkeypatch.setenv("SPARKNET_OWNER_GID", "1000")
    monkeypatch.delenv("SPARKNET_OWNER_USER", raising=False)
    (tmp_path / "vaults" / "peer-testuser").mkdir(parents=True)
    owner = tmp_path / "owner.json"
    owner.write_text(json.dumps({"driver": "vm", "uid": 1500, "gid": 1500}), encoding="utf-8")
    args = ENGINE._parser().parse_args(
        [
            "--sandbox-backend",
            "openshell",
            "--knowledge-root",
            str(tmp_path / "vaults"),
            "--state-root",
            str(tmp_path / "state"),
            "--openshell-owner-file",
            str(owner),
            "--no-prune",
        ]
    )
    manager = ENGINE.SandboxManager(args)
    calls: list[list[str]] = []
    manager.openshell_backend._run_openshell = lambda argv, timeout=120: calls.append(list(argv)) or ""
    with pytest.raises(ENGINE.EngineError, match="does not match the engine owner"):
        manager.ensure("testuser")
    assert calls == [], "the owner mismatch must fail before any sandbox create"


def test_engine_parser_accepts_openshell_vm_options(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPARKNET_OPENCODE_SANDBOX_BACKEND", raising=False)
    args = ENGINE._parser().parse_args(
        [
            "--sandbox-backend",
            "openshell",
            "--vm-image",
            "sparknet-opencode-runtime-vm:2.2.5",
            "--forward-base",
            "14300",
            "--proxy-port",
            "11600",
            "--openshell-owner-file",
            "/tmp/owner.json",
        ]
    )
    assert args.sandbox_backend == "openshell"
    assert args.vm_image == "sparknet-opencode-runtime-vm:2.2.5"
    assert args.forward_base == 14300
    assert args.proxy_port == 11600
    assert args.openshell_owner_file == "/tmp/owner.json"
