"""Vault health detects structural drift and repairs only safe derived state."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from sparknet_loader import load_by_path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "sparknet-vault-health.py"


def _load() -> object:
    return load_by_path("sparknet_vault_health_test", SCRIPT)


def _fixture(tmp_path: Path) -> Path:
    fabric = tmp_path / "fabric"
    (fabric / "contracts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "contracts" / "knowledge-flow.md", fabric / "contracts" / "knowledge-flow.md")
    os.symlink(REPO_ROOT / "scripts", fabric / "scripts")
    root = fabric / "peer-node"
    root.mkdir()
    for name in ("AGENTS.md", "CONTEXT.md", "MISSION.md", "INTENT.md", "MEMORY.md", "PLAN.md", ".env.example"):
        source = REPO_ROOT / "peer-node" / name
        if source.is_file():
            shutil.copy2(source, root / name)
    (root / "00_System").mkdir()
    (root / "00_System" / "index.md").write_text("# index\n")
    (root / "00_System" / "log.md").write_text("# log\n")
    return root


def test_diagnose_reports_a_missing_shim(tmp_path: Path) -> None:
    module = _load()
    root = _fixture(tmp_path)
    report = module.diagnose(root)
    assert any("concepts is not a symlink" in issue for issue in report["issues"])


def test_repair_recreates_the_skeleton_and_regenerates_context(tmp_path: Path) -> None:
    module = _load()
    root = _fixture(tmp_path)
    actions = module.repair(root)
    assert any(action.startswith("env-init") for action in actions)
    assert any(action.startswith("context-pack") for action in actions)
    assert (root / "05_Knowledge" / "concepts").is_dir()
    assert (root / "concepts").is_symlink()
    assert (root / "CONTEXT.md").is_file()
    report = module.diagnose(root)
    assert not any("missing directory" in issue for issue in report["issues"])
    assert not any("is not a symlink" in issue for issue in report["issues"])


def test_the_health_report_is_written(tmp_path: Path) -> None:
    module = _load()
    root = _fixture(tmp_path)
    module.repair(root)
    report = module.diagnose(root)
    (root / "00_System").mkdir(exist_ok=True)
    assert module.main(["--root", str(root)]) == 0
    assert (root / "00_System" / "vault-health.json").is_file()
    assert report["status"] in {"healthy", "needs-attention"}
