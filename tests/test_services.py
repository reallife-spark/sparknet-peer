"""Service-level behaviour and the zero-dependency guarantee.

Also covers the sharpest edge in this repo: `--test-approval` without
`--dry-run` appends to the ledger. Every test here works on a copy so the real
blockchain/ledger.json is never touched.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

VAULT_ROOT = Path(__file__).resolve().parent.parent
SERVICES = sorted((VAULT_ROOT / "services").glob("*.py"))
LEDGER = VAULT_ROOT / "blockchain" / "ledger.json"


# --- dependency policy -------------------------------------------------------

#: The only permitted third-party import. Ed25519 is not in the stdlib, so the
#: guard and the authz library cannot be stdlib-only. Anything else is a
#: regression until someone justifies it.
ALLOWED_THIRD_PARTY = {"cryptography"}


def _stdlib_only(path: Path) -> list[str]:
    """Return non-stdlib imports that are not on the allow-list."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return sorted(name for name in found if name not in sys.stdlib_module_names and name not in ALLOWED_THIRD_PARTY)


def test_services_exist() -> None:
    assert SERVICES, "no services found under services/"


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_service_imports_only_allowed_dependencies(service: Path) -> None:
    """Stdlib plus `cryptography`. A new third-party import must be justified."""
    offenders = _stdlib_only(service)
    assert not offenders, f"{service.name} imports unapproved modules: {offenders}"


def test_pyproject_dependency_list_matches_the_allowlist() -> None:
    import tomllib

    data = tomllib.loads((VAULT_ROOT / "pyproject.toml").read_text())
    declared = {d.split(">=")[0].split("==")[0].strip() for d in data["project"]["dependencies"]}
    assert declared == ALLOWED_THIRD_PARTY, f"pyproject dependencies drifted: {declared}"
    assert data["project"]["requires-python"] == ">=3.10"


def test_cryptography_is_importable() -> None:
    """Fail loudly with an actionable message rather than at first request."""
    try:
        import cryptography  # noqa: F401
    except ImportError as exc:
        pytest.fail(f"cryptography is required but not installed ({exc}); run: make devtools")


# --- relocation of the services themselves ------------------------------------


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_service_runs_from_a_relocated_copy(relocated_vault: Path, service: Path) -> None:
    """A copied service resolves manifests relative to itself."""
    target = relocated_vault / "services" / service.name
    proc = subprocess.run(
        [sys.executable, str(target), "--help"],
        cwd="/",
        env={**os.environ, "SPARKNET_VAULT_ROOT": "", "VAULT_PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"{service.name} failed to run relocated:\n{proc.stderr}"
    assert "/mnt/knowledge/sparknet" not in proc.stdout + proc.stderr, f"{service.name} leaked the original path"


@pytest.mark.parametrize("service", SERVICES, ids=lambda p: p.name)
def test_service_help_works(service: Path) -> None:
    proc = subprocess.run([sys.executable, str(service), "--help"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0


# --- guard daemon -------------------------------------------------------------


def test_guard_validates_manifest() -> None:
    proc = subprocess.run(
        [sys.executable, str(VAULT_ROOT / "services" / "sparknet-guard-d.py"), "--check-manifest"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_guard_refuses_to_listen_without_an_anchor() -> None:
    """The guard must not open a port while the overlay is down.

    Replaces the old 'dry-run succeeds' check: --dry-run alone no longer means
    'exit cleanly', because the daemon now genuinely tries to serve.
    """
    proc = subprocess.run(
        [sys.executable, str(VAULT_ROOT / "services" / "sparknet-guard-d.py"), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 3, f"expected a standalone-mode refusal:\n{proc.stdout}{proc.stderr}"
    assert "standalone" in (proc.stdout + proc.stderr).lower()


def test_guard_writes_only_via_atomic_replace() -> None:
    """No direct truncating open() of a manifest path.

    The guard does write (that is its job), but only by staging a temp file in
    the target directory and os.replace-ing it, then restoring chattr +i.
    """
    import re

    source = (VAULT_ROOT / "services" / "sparknet-guard-d.py").read_text()
    assert "os.replace" in source, "guard must write atomically via os.replace"
    assert "os.fsync" in source, "guard must fsync before replacing"
    assert not re.search(r"open\([^)]*,\s*['\"][wa]", source), "guard must not open a manifest path for writing"
    assert not re.search(r"\.open\(\s*['\"][wa]", source), "guard must not Path.open() in write mode"


def test_guard_never_uses_a_shell() -> None:
    """Subprocess calls take argument lists, so a manifest value cannot become a command."""
    source = (VAULT_ROOT / "services" / "sparknet-guard-d.py").read_text()
    assert "shell=True" not in source
    assert "os.system" not in source


# --- dispatch bridge ----------------------------------------------------------


def test_health_check() -> None:
    proc = subprocess.run(
        [sys.executable, str(VAULT_ROOT / "services" / "sparknet-dispatch-bridge.py"), "--health"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "dispatch" in (proc.stdout + proc.stderr).lower()


def test_test_approval_does_not_touch_real_ledger() -> None:
    """The important safety property: run it for real, against a copy."""
    before = LEDGER.read_bytes()

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "vault"
        shutil.copytree(
            VAULT_ROOT,
            dest,
            ignore=shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "references"),
        )
        proc = subprocess.run(
            [sys.executable, str(dest / "services" / "sparknet-dispatch-bridge.py"), "--test-approval"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "SPARKNET_VAULT_ROOT": str(dest), "VAULT_PATH": str(dest)},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # The copy changed...
        assert (dest / "blockchain" / "ledger.json").read_bytes() != before, (
            "expected the copy's ledger to be appended to"
        )
        # ...and the real one did not.
        assert LEDGER.read_bytes() == before, "REAL LEDGER MUTATED — this is a serious bug"


def test_real_ledger_is_valid_json() -> None:
    data = json.loads(LEDGER.read_text())
    assert isinstance(data, (dict, list)), "ledger should be a JSON object or array"


def _entries(data: object) -> list[dict]:
    if isinstance(data, dict):
        entries = data.get("entries", [])
    elif isinstance(data, list):
        entries = data
    else:
        entries = []
    return [e for e in entries if isinstance(e, dict)]


def test_ledger_entries_are_chained() -> None:
    """prev_hash linkage, where the ledger carries it."""
    entries = _entries(json.loads(LEDGER.read_text()))
    for index in range(1, len(entries)):
        prev, current = entries[index - 1], entries[index]
        if "hash" in prev and "prev_hash" in current:
            assert current["prev_hash"] == prev["hash"], f"broken hash chain at entry {index}"
