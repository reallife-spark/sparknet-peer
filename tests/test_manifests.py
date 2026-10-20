"""The critical-manifest provenance map.

The vault is authoritative; these tests keep the map honest so a protected
/etc path can never be silently unaccounted for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

VAULT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = VAULT_ROOT / "manifests" / "critical-manifest.json"

KNOWN_EXTERNAL = {"/etc/wireguard/wg1.conf"}


@pytest.fixture
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def test_manifest_is_valid_json() -> None:
    json.loads(MANIFEST_PATH.read_text())


def test_protected_paths_are_absolute_etc(manifest: dict) -> None:
    for path in manifest["protected_paths"]:
        assert path.startswith("/etc/"), f"protected path should be under /etc: {path}"


def test_provenance_covers_every_protected_path(manifest: dict) -> None:
    """Every protected path is either generated from the vault or declared external.

    This is the invariant that stops a new /etc file being locked with no
    recorded source of truth.
    """
    sot = manifest["source_of_truth"]
    mapped = {e["installed_path"] for e in sot["generated"]} | {e["installed_path"] for e in sot["unmapped"]}
    missing = set(manifest["protected_paths"]) - mapped
    assert not missing, f"protected paths without provenance: {sorted(missing)}"


def test_provenance_invents_nothing(manifest: dict) -> None:
    """No mapped installed_path may be absent from protected_paths."""
    protected = set(manifest["protected_paths"])
    for entry in manifest["source_of_truth"]["generated"] + manifest["source_of_truth"]["unmapped"]:
        assert entry["installed_path"] in protected, f"{entry['installed_path']} is not a protected path"


def test_generated_entries_point_at_real_vault_files(manifest: dict) -> None:
    for entry in manifest["source_of_truth"]["generated"]:
        if entry.get("source") == "vault":
            target = VAULT_ROOT / entry["vault_path"]
            assert target.is_file(), f"provenance points at a missing file: {entry['vault_path']}"


def test_unmapped_entries_document_their_gap(manifest: dict) -> None:
    for entry in manifest["source_of_truth"]["unmapped"]:
        assert entry.get("source") == "external", f"{entry['installed_path']} must be marked external"
        assert entry.get("gap"), f"{entry['installed_path']} must explain why it is unmapped"


def test_wireguard_provenance_is_now_generated(manifest: dict) -> None:
    """The Phase 4 gap is closed: /etc/wireguard/wg0.conf has a vault renderer.

    It used to be recorded as an external gap because configs/wireguard/ was
    empty. It now renders from a tracked template, so it must appear as
    generated. If this test fails, either the renderer or the map is missing.
    """
    unmapped = {e["installed_path"] for e in manifest["source_of_truth"]["unmapped"]}
    assert "/etc/wireguard/wg1.conf" not in unmapped, "wireguard is no longer an unmapped gap"

    generated = {e["installed_path"]: e for e in manifest["source_of_truth"]["generated"]}
    entry = generated.get("/etc/wireguard/wg1.conf")
    assert entry is not None, "wireguard must have a provenance entry"
    assert entry["vault_path"] == "configs/wireguard/wg0-node.conf.template"
    assert entry.get("rendered_by") == "scripts/sparknet-wireguard-render.sh"
    assert "0600" in entry["render_target"] or "state" in entry["render_target"].lower()


def test_rendered_paths_are_declared_never_tracked(manifest: dict) -> None:
    """Anything with rendered_by must resolve to a tracked template that exists."""
    for entry in manifest["source_of_truth"]["generated"]:
        if not entry.get("rendered_by"):
            continue
        template = VAULT_ROOT / entry["vault_path"]
        assert template.is_file(), f"{entry['rendered_by']} renders from a missing template: {entry['vault_path']}"
        assert template.name.endswith(".template"), f"rendered source should be a template: {entry['vault_path']}"


def test_vault_is_declared_authoritative(manifest: dict) -> None:
    assert manifest["source_of_truth"].get("_comment"), "provenance map must state what it is"
    note = manifest["notes"].lower()
    assert "vault" in note and "derived" in note, "notes must record the vault-as-authoritative contract"


def test_install_config_manifest_assignments_are_wired(manifest: dict) -> None:
    """Entries assigned to install-config.sh are exactly the /etc/netplan copies."""
    assigned = {
        e["installed_path"]
        for e in manifest["source_of_truth"]["generated"]
        if e.get("installer") == "scripts/sparknet-install-config.sh"
    }
    assert assigned, "expected the netplan configs to be assigned to install-config.sh"
    assert all(p.startswith("/etc/netplan/") for p in assigned), f"unexpected assignment: {assigned}"
    for path in assigned:
        assert (VAULT_ROOT / "deployment" / "netplan" / Path(path).name).is_file()
