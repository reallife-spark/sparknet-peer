"""Phase 4's viability checks, converted from manual ✓ marks into assertions.

Before this file, master-node/plans/PHASE_VIABILITY_REPORT.md recorded things like
"sync.sh --dry-run: ✓ (env validated, paths checked, structured logging)". Those
were one-off manual runs on one machine: nobody re-ran them and nothing asserted
behaviour. The difference here is that each check asserts **no mutation** by
hashing the tree before and after, so "exit 0" alone can never pass.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

VAULT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = VAULT_ROOT / "scripts"

# Directories whose churn is expected during a test run and must not count as
# a mutation by the scripts under test.
IGNORED_DIRS = {".git", ".venv", ".state", ".cache", "__pycache__", ".pytest_cache", "node_modules", "references"}

LOG_LINE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] \[(?:INFO|WARN|ERROR)\] .+")


def tree_fingerprint(root: Path) -> str:
    """Hash every tracked file's path and content. Detects any write, add or delete."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(root)).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def run_script(
    name: str,
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPTS / name), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd) if cwd else str(VAULT_ROOT),
        env={**os.environ, **(env or {})},
        timeout=120,
    )


# --- Phase 4 item: sync.sh --dry-run ----------------------------------------


def test_sync_dry_run_succeeds_and_logs_structurally() -> None:
    proc = run_script("sparknet-sync.sh", ["--dry-run"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, "expected log output"
    for line in lines:
        assert LOG_LINE.match(line), f"unstructured log line: {line!r}"


def test_sync_dry_run_reports_its_vault_root() -> None:
    proc = run_script("sparknet-sync.sh", ["--dry-run"])
    assert "SPARKNET_VAULT_ROOT=" in proc.stdout
    assert "DRY-RUN" in proc.stdout


def test_sync_dry_run_mutates_nothing() -> None:
    before = tree_fingerprint(VAULT_ROOT)
    proc = run_script("sparknet-sync.sh", ["--dry-run"])
    assert proc.returncode == 0
    assert tree_fingerprint(VAULT_ROOT) == before, "sync.sh --dry-run modified the vault"


def test_sync_without_dry_run_also_does_not_sync_yet() -> None:
    """Template mode: the non-dry-run path is still a no-op and must stay one."""
    before = tree_fingerprint(VAULT_ROOT)
    proc = run_script("sparknet-sync.sh", [])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert tree_fingerprint(VAULT_ROOT) == before


# --- Phase 4 item: wlan-guard.sh --dry-run ----------------------------------


def test_wlan_guard_dry_run_succeeds() -> None:
    proc = run_script("sparknet-wlan-guard.sh", ["--dry-run"])
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_wlan_guard_emits_a_master_only_policy() -> None:
    proc = run_script("sparknet-wlan-guard.sh", ["--dry-run"])
    assert "10.100.0.1" in proc.stdout, "the WLAN policy must pin the Master Anchor"


def test_wlan_guard_dry_run_mutates_nothing() -> None:
    before = tree_fingerprint(VAULT_ROOT)
    proc = run_script("sparknet-wlan-guard.sh", ["--dry-run"])
    assert proc.returncode == 0
    assert tree_fingerprint(VAULT_ROOT) == before, "wlan-guard --dry-run modified the vault"


# --- node containment switch (root gating) ----------------------------------


def test_node_containment_refuses_non_root() -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root: cannot exercise the non-root refusal")
    proc = subprocess.run(
        [str(VAULT_ROOT / "scripts" / "sparknet-node-containment.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 1
    assert "requires root" in (proc.stdout + proc.stderr)


def test_node_containment_dry_run_mutates_nothing() -> None:
    before = tree_fingerprint(VAULT_ROOT)
    proc = subprocess.run(
        [str(VAULT_ROOT / "scripts" / "sparknet-node-containment.sh"), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    # Exit 1 when non-root (the refusal), 0 when root.
    assert proc.returncode in (0, 1)
    assert tree_fingerprint(VAULT_ROOT) == before


def test_node_containment_help_documents_root_gate() -> None:
    proc = subprocess.run(
        [str(VAULT_ROOT / "scripts" / "sparknet-node-containment.sh"), "--help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0
    assert "ROOT-GATED" in proc.stdout


def test_node_containment_is_not_the_cluster_killswitch() -> None:
    """The peer's containment switch must not claim cluster authority.

    A node holds no authority over the fabric. If this script ever drifts into
    looking like the cluster KillSwitch, an operator could reasonably believe a
    single node can halt the whole network — and the documentation would be
    lying about the trust model.
    """
    text = (VAULT_ROOT / "scripts" / "sparknet-node-containment.sh").read_text()
    assert "THIS NODE ONLY" in text
    assert "master-node/" in text, "must point at the Master-side KillSwitch spec"


# --- Phase 4 item: guard-d --dry-run --check-manifest ------------------------


def test_guard_manifest_validation_reports_provenance() -> None:
    proc = subprocess.run(
        ["python3", str(VAULT_ROOT / "services" / "sparknet-guard-d.py"), "--check-manifest"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "provenance complete" in proc.stdout


def test_guard_manifest_validation_fails_when_provenance_is_missing(tmp_path: Path) -> None:
    """Prove the check actually has teeth by breaking the map in a copy."""
    dest = tmp_path / "vault"
    shutil.copytree(
        VAULT_ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "references", ".opencode"),
    )
    manifest = json.loads((dest / "manifests" / "critical-manifest.json").read_text())
    manifest["protected_paths"].append("/etc/sparknet/invented.yaml")
    (dest / "manifests" / "critical-manifest.json").write_text(json.dumps(manifest, indent=2))
    proc = subprocess.run(
        ["python3", str(dest / "services" / "sparknet-guard-d.py"), "--check-manifest"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 1
    assert "without provenance" in (proc.stdout + proc.stderr)


# --- dispatch bridge: health, no ledger write path, canonical approvals ---


def test_dispatch_health_ok() -> None:
    proc = subprocess.run(
        ["python3", str(VAULT_ROOT / "services" / "sparknet-dispatch-bridge.py"), "--health"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Health check: OK" in proc.stdout
    for state in ("PLAN", "PERMISSION_PENDING", "PERMITTED", "EXECUTING", "COMPLETE", "DENIED", "ERROR"):
        assert state in proc.stdout, f"PPE state {state} missing from health output"


def test_dispatch_never_writes_the_ledger(tmp_path: Path) -> None:
    """Every surviving mode leaves the ledger byte-identical, and the old flag is refused.

    `--test-approval` used to append blocks in a shape the chain never
    understood; it is deleted, so the guarantee is now "cannot append at all".
    """
    ledger = VAULT_ROOT / "blockchain" / "ledger.json"
    before = ledger.read_bytes()
    dest = tmp_path / "vault"
    shutil.copytree(
        VAULT_ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "references", ".opencode"),
    )
    for mode in ([], ["--dry-run"], ["--health"]):
        proc = subprocess.run(
            ["python3", str(dest / "services" / "sparknet-dispatch-bridge.py"), *mode],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env={**os.environ, "SPARKNET_VAULT_ROOT": str(dest), "VAULT_PATH": str(dest)},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (dest / "blockchain" / "ledger.json").read_bytes() == before, f"mode {mode} wrote"

    refused = subprocess.run(
        ["python3", str(dest / "services" / "sparknet-dispatch-bridge.py"), "--test-approval"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert refused.returncode != 0, "--test-approval came back"
    assert ledger.read_bytes() == before, "the real ledger must never be written by a test"


def _write_node_key(authz_path: Path, dest: Path) -> None:
    """Generate the node's countersignature key at 0600 in a throwaway state dir.

    The approval blocks are dual-signed, so an append test needs both halves of
    the key custody — the user half comes from `user-init`, this is the node's.
    """
    spec = importlib.util.spec_from_file_location("sparknet_authz_for_approvals", authz_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    private_b64, _ = module.generate_keypair()
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest.write_text(private_b64 + "\n")
    dest.chmod(0o600)


def test_approvals_are_appended_through_the_shared_tool(tmp_path: Path) -> None:
    """One append path, the canonical block shape, and a chain that re-links.

    The bridge cannot do this any more, so if `--test-approval` ever returns the
    failure lands here: the canonical shape, the dual signatures and the
    `prev_hash` link are all asserted from an append that only shared/ can make.
    """
    fabric = VAULT_ROOT.parent
    tool = fabric / "shared" / "sparknet-provenance.py"
    authz_tool = fabric / "shared" / "sparknet-authz.py"
    assert tool.is_file() and authz_tool.is_file(), "the shared implementation must exist"

    root = tmp_path / "vault"
    (root / "00_System").mkdir(parents=True)
    (root / "01_Inbox").mkdir()
    (root / "blockchain").mkdir()
    (root / "00_System" / "user.md").write_text(
        "---\n"
        "type: user-record\n"
        "schema: sparknet-user/1\n"
        "status: unfilled\n"
        'owner_id: ""\n'
        'owner_node: ""\n'
        "role: owner\n"
        "immutable: true\n"
        "key:\n"
        "  alg: ed25519\n"
        '  public_key_b64: ""\n'
        '  fingerprint: ""\n'
        '  key_ref: "$SPARKNET_STATE_DIR/keys/user.ed25519"\n'
        '  created: ""\n'
        "---\n\n# user record\n"
    )
    env = {**os.environ, "SPARKNET_STATE_DIR": str(tmp_path / "state")}

    def run(*args: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["python3", str(tool), "--vault", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=env,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc

    run("user-init", "--owner-id", "U-10001")
    _write_node_key(authz_tool, tmp_path / "state" / "keys" / "ed25519-node.key.b64")
    run("append-approval", "--question", "may this node ingest the capture?", "--decision", "approved",
        "--agent", "tester", "--model", "none")
    run("append-approval", "--question", "a second one?", "--decision", "clarified",
        "--agent", "tester", "--model", "none")

    blocks = json.loads((root / "blockchain" / "ledger.json").read_text())["blocks"]
    assert len(blocks) == 2
    for index, block in enumerate(blocks):
        assert set(block) == {"block_id", "prev_hash", "timestamp", "signature", "data", "hash"}, (
            f"block {index} is not the canonical shape: {sorted(block)}"
        )
        assert block["block_id"] == f"{index:08d}"
        assert block["signature"] == block["data"]["signatures"]["node"]
        assert block["prev_hash"] == (blocks[index - 1]["hash"] if index else "")
        # The shape the deleted flag invented must not reappear anywhere.
        assert not {"index", "payload", "block_hash"} & set(block)

    verified = subprocess.run(
        ["python3", str(tool), "--vault", str(root), "verify"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


# --- Phase 4 item: identity capture + provision -----------------------------


def test_identity_capture_is_deterministic_on_stable_fields(tmp_path: Path) -> None:
    """Two captures must agree on identity, differing only by timestamp."""
    dest = tmp_path / "vault"
    shutil.copytree(
        VAULT_ROOT,
        dest,
        ignore=shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "references", ".opencode"),
    )
    target = dest / "manifests" / "user-env-manifest.json"
    subprocess.run(
        [str(dest / "scripts" / "sparknet-identity-capture.sh")],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    first = json.loads(target.read_text())
    subprocess.run(
        [str(dest / "scripts" / "sparknet-identity-capture.sh")],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    second = json.loads(target.read_text())
    for stable in ("user", "system"):
        assert first[stable] == second[stable], f"{stable} fields should be stable across captures"
    assert first["privilege_boundaries"]["is_root"] is False


def test_provision_runs_against_a_deployed_copy() -> None:
    """Provision must work on a real deployment, not only in the working tree.

    A peer's deployable unit is the subvault *together with* the fabric root that
    holds contracts/ and shared/. Copying peer-node/ on its own is not a valid
    deployment — validation would correctly fail because the contract it obeys
    is absent. This stages the pair the way a deployment would lay it out, at a
    different path, so it also proves nothing depends on this checkout.
    """
    dest_root = Path(os.environ.get("SPARKNET_TEST_VAULT", "/tmp/opencode/sparknet-provision-check"))
    shutil.rmtree(dest_root, ignore_errors=True)
    ignore = shutil.ignore_patterns(".git", ".venv", ".state", ".cache", "__pycache__", "references", ".opencode")
    peer_dest = dest_root / "peer-node"
    peer_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(VAULT_ROOT, peer_dest, ignore=ignore)
    for shared_dir in ("contracts", "shared"):
        shutil.copytree(VAULT_ROOT.parent / shared_dir, dest_root / shared_dir, ignore=ignore)

    proc = subprocess.run(
        [str(peer_dest / "scripts" / "sparknet-provision.sh")],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The captured identity must land in the copy, not back in the real vault.
    assert (peer_dest / "manifests" / "user-env-manifest.json").is_file()
    shutil.rmtree(dest_root, ignore_errors=True)


# --- Phase 4 item: "Python/bash syntax: ✓ All valid" ------------------------


@pytest.mark.parametrize("script", sorted(SCRIPTS.glob("*.sh")), ids=lambda p: p.name)
def test_every_bash_script_parses(script: Path) -> None:
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False, timeout=60)
    assert proc.returncode == 0, f"{script.name} does not parse:\n{proc.stdout}{proc.stderr}"


@pytest.mark.parametrize("script", sorted((VAULT_ROOT / "systemd").glob("*.sh")), ids=lambda p: p.name)
def test_every_systemd_shell_script_parses(script: Path) -> None:
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False, timeout=60)
    assert proc.returncode == 0, f"{script.name} does not parse:\n{proc.stdout}{proc.stderr}"


@pytest.mark.parametrize("service", sorted((VAULT_ROOT / "services").glob("*.py")), ids=lambda p: p.name)
def test_every_service_compiles(service: Path) -> None:
    proc = subprocess.run(
        ["python3", "-m", "py_compile", str(service)], capture_output=True, text=True, check=False, timeout=60
    )
    assert proc.returncode == 0, f"{service.name} does not compile:\n{proc.stdout}{proc.stderr}"


# --- every executable script advertises --help -------------------------------


@pytest.mark.parametrize(
    "script",
    # sparknet-env.sh is a sourced library, not a CLI: it must set variables and
    # stay silent, so it has no --help by design.
    sorted(p for p in SCRIPTS.glob("*.sh") if p.name != "sparknet-env.sh"),
    ids=lambda p: p.name,
)
def test_every_script_has_help(script: Path) -> None:
    proc = subprocess.run([str(script), "--help"], capture_output=True, text=True, check=False, timeout=60)
    assert proc.returncode == 0, f"{script.name} --help failed:\n{proc.stdout}{proc.stderr}"
    assert (proc.stdout + proc.stderr).strip(), f"{script.name} --help printed nothing"


def test_env_library_stays_quiet_when_executed() -> None:
    """Sourcing must not print anything; a stray echo would corrupt every caller's stdout."""
    proc = subprocess.run([str(SCRIPTS / "sparknet-env.sh")], capture_output=True, text=True, check=False, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"sourced env library leaked to stdout: {proc.stdout!r}"
