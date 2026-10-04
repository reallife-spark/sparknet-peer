"""The env contract: paths resolve from the script's own location.

This is the guarantee that makes the vault relocatable, so it is tested from
several working directories and against an explicit override.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
ENV_SCRIPT = VAULT_ROOT / "scripts" / "sparknet-env.sh"


def _source_env(cwd: Path, env: dict[str, str] | None = None, script: Path | None = None) -> dict[str, str]:
    """Source sparknet-env.sh in a subshell and return the exported variables.

    `script` defaults to the real vault's copy; pass the relocated one when
    testing a copy, otherwise the relocation proof is meaningless.
    """
    target = script or ENV_SCRIPT
    proc = subprocess.run(
        ["bash", "-c", f'set -a; source "{target}"; env'],
        cwd=str(cwd),
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        check=True,
    )
    out = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def test_resolves_from_vault_root(vault_root: Path) -> None:
    env = _source_env(vault_root)
    assert Path(env["SPARKNET_VAULT_ROOT"]) == vault_root.resolve()
    assert Path(env["VAULT_PATH"]) == vault_root.resolve()


def test_resolves_from_unrelated_cwd(tmp_path: Path, vault_root: Path) -> None:
    """The whole point: cwd must not influence resolution."""
    env = _source_env(tmp_path)
    assert Path(env["SPARKNET_VAULT_ROOT"]) == vault_root.resolve()


def test_resolves_from_nested_dir(vault_root: Path) -> None:
    env = _source_env(vault_root / "services")
    assert Path(env["SPARKNET_VAULT_ROOT"]) == vault_root.resolve()


def test_knowledge_root_is_vault_parent(vault_root: Path) -> None:
    env = _source_env(vault_root)
    assert Path(env["SPARKNET_KNOWLEDGE_ROOT"]) == vault_root.resolve().parent


def test_state_and_cache_stay_inside_vault(vault_root: Path) -> None:
    env = _source_env(vault_root, env={"XDG_CONFIG_HOME": "", "XDG_DATA_HOME": "", "XDG_STATE_HOME": ""})
    for var in ("SPARKNET_STATE_DIR", "SPARKNET_CACHE_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        assert Path(env[var]).is_relative_to(vault_root.resolve()), f"{var} escapes the vault: {env[var]}"


def test_env_override_wins(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    env = _source_env(VAULT_ROOT, env={"SPARKNET_VAULT_ROOT": str(target)})
    assert Path(env["SPARKNET_VAULT_ROOT"]) == target
    assert Path(env["VAULT_PATH"]) == target


def test_relocated_vault_resolves_to_itself(relocated_vault: Path) -> None:
    """The relocation proof: a copied vault must point at itself, not the original."""
    env = _source_env(
        relocated_vault,
        env={"SPARKNET_VAULT_ROOT": "", "VAULT_PATH": "", "SPARKNET_KNOWLEDGE_ROOT": ""},
        script=relocated_vault / "scripts" / "sparknet-env.sh",
    )
    assert Path(env["SPARKNET_VAULT_ROOT"]) == relocated_vault.resolve()
    assert Path(env["VAULT_PATH"]) == relocated_vault.resolve()
    assert Path(env["SPARKNET_KNOWLEDGE_ROOT"]) == relocated_vault.resolve().parent
