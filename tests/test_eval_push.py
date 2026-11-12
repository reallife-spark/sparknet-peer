"""The peer eval-audit pusher: dry-run builds a signed report shape."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROLE_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROLE_ROOT / "services" / "sparknet-eval-push.py"


def test_dry_run_emits_a_report_without_a_key(tmp_path: Path) -> None:
    records = tmp_path / "records.json"
    records.write_text(
        json.dumps(
            [
                {
                    "schema": "sparknet-skill-eval/1",
                    "eval_id": "fixture-1",
                    "verdict": "pass",
                    "findings": [],
                    "skill": {"name": "code-review", "origin": "mattpocock", "digest": "a" * 64},
                    "subject": {"kind": "skill", "ref": "tests/fixture.json", "digest": "b" * 64},
                    "evaluator": {"kind": "harness", "id": "fixture", "model": ""},
                    "created_at": "2026-11-12T00:00:00Z",
                    "body_sha256": "c" * 64,
                }
            ]
        )
    )
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--records", str(records), "--dry-run", "--json"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["report"]["schema"] == "sparknet-eval-report/1"
    assert payload["report"]["records"][0]["eval_id"] == "fixture-1"


def test_missing_records_file_refuses(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--records", str(tmp_path / "missing.json"), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 2
    assert "cannot read records" in proc.stderr
