"""Shared fixtures and helpers for the sparknet test suite."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

VAULT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(VAULT_ROOT.parent / "shared"))


def _run(
    argv: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command with a minimal, predictable environment."""
    import os

    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def vault_root() -> Path:
    return VAULT_ROOT


@pytest.fixture
def relocated_vault(tmp_path: Path) -> Path:
    """A throwaway copy of the vault at a different path.

    Proves the setup is genuinely relocatable: anything that still resolves to
    the original location shows up immediately.
    """
    dest = tmp_path / "relocated"
    shutil.copytree(
        VAULT_ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "node_modules"),
    )
    return dest
