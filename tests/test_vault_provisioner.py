"""Vault provisioner: knowledge-vault slice materialisation and KB wiring.

The provisioner (`services/sparknet-vault-provisioner.py`) makes a per-user
`peer-<user_id>` copy of the peer-node knowledge vault and connects it to Open
WebUI as a user-owned KB after an approval. These tests pin down the parts a
file can prove:

* the **copy is a knowledge-vault slice**, not a deployment — `.state/`,
  `blockchain/`, `services/`, `scripts/`, `container/`, `configs/`, `tests/`,
  `manifests/`, deploy artifacts and the template's own `.env` never cross;
* the copy's `.env` resolves `VAULT_PATH` to **itself**;
* the KB grant the admin mints for a user is a flat `user` read grant in the
  shape WebUI 0.12.0 verifies;
* a re-provision attempt refuses to overwrite the existing vault;
* `--help` answers without doing anything (fabric-wide rule).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

VAULT_ROOT = Path(__file__).resolve().parent.parent
PROVISIONER = VAULT_ROOT / "services" / "sparknet-vault-provisioner.py"
TEMPLATE = VAULT_ROOT

ALLOWED_TRACKED_IN_COPY = {
    ".env.example",
    "00_System",
    "01_Inbox",
    "02_Daily",
    "03_Sources",
    "04_Evergreen",
    "05_Knowledge",
    "06_Projects",
    "07_Archives",
    "AGENTS.md",
    "MISSION.md",
    "audit",
    "docs",
}


def _run(argv: list[str], cwd: Path | None = None, env: dict[str, str] | None = None):
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, str(PROVISIONER), *argv],
        cwd=str(cwd) if cwd else None,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _materialise(tmp_path: Path) -> tuple[Path, Path]:
    """Materialise a slice into a temp knowledge root; return (dest, root)."""
    mod = import_source()
    root = tmp_path / "knowledge"
    dest = root / "peer-abc-123"
    mod.materialise_vault_slice(TEMPLATE, dest)
    return dest, root


def import_source():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sparknet_vault_provisioner", str(PROVISIONER)
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_help_exits_zero_and_does_nothing(tmp_path: Path) -> None:
    proc = _run(["--help"])
    assert proc.returncode == 0
    assert "sparknet-vault-provisioner.py" in proc.stdout


def test_materialise_copies_only_the_knowledge_slice(tmp_path: Path) -> None:
    dest, _ = _materialise(tmp_path)
    names = {p.name for p in dest.iterdir()}
    assert "AGENTS.md" in names
    assert "00_System" in names
    assert "docs" in names

    for forbidden in (
        ".state",
        "blockchain",
        "services",
        "scripts",
        "container",
        "configs",
        "tests",
        "manifests",
        ".git",
        ".env",
        "Makefile",
        "deployment",
    ):
        assert forbidden not in names, f"{forbidden} leaked into the copy"


def test_materialise_refuses_existing_destination(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    dest = root / "peer-x"
    dest.mkdir(parents=True)
    mod = import_source()
    with pytest.raises(mod.Refusal):
        mod.materialise_vault_slice(TEMPLATE, dest)


def test_written_env_points_vault_path_at_the_copy(tmp_path: Path) -> None:
    dest, _ = _materialise(tmp_path)
    mod = import_source()
    env_file = mod.write_vault_env(dest)
    source = env_file.read_text()
    assert f'VAULT_PATH="{dest}"' in source
    assert "VAULT_SOURCES_DIR" in source
    assert str(TEMPLATE) not in source, "the copy must not point back at the template"


def test_env_example_travels_and_is_the_template(tmp_path: Path) -> None:
    dest, _ = _materialise(tmp_path)
    assert (dest / ".env.example").exists()
    assert "VAULT_PATH" in (dest / ".env.example").read_text()


def test_grant_shape_is_flat_user_read(tmp_path: Path) -> None:
    mod = import_source()
    grant = mod._grant("user-123", "read")
    assert grant == {
        "principal_type": "user",
        "principal_id": "user-123",
        "permission": "read",
    }


def test_slug_sanitises(tmp_path: Path) -> None:
    mod = import_source()
    assert mod._slug("my user/123!") == "myuser123"
    assert len(mod._slug("")) == 4 and mod._slug("") == "user"


def test_vault_pages_skip_symlink_shims_and_state(tmp_path: Path) -> None:
    """A page is embedded once, via its numbered home, never via a shim."""
    dest, _ = _materialise(tmp_path)
    mod = import_source()
    (dest / ".state").mkdir(exist_ok=True)
    (dest / ".state" / "provisioned.json").write_text("{}")
    (dest / "dup.md").symlink_to(dest / "00_System" / "index.md")

    pages = {str(p.relative_to(dest)) for p in mod._vault_pages(dest)}
    assert "dup.md" not in pages, "a symlinked shim must not be embedded twice"
    assert "index.md" not in pages, "the top-level index.md shim must be skipped"
    assert "00_System/index.md" in pages
    assert "00_System/user.md" in pages
    assert all(".state" not in Path(p).parts for p in pages)


def test_top_dir_maps_numbered_categories(tmp_path: Path) -> None:
    mod = import_source()
    assert mod._top_dir(Path("00_System/log.md")) == "00_System"
    assert mod._top_dir(Path("05_Knowledge/concepts/x.md")) == "05_Knowledge"
    assert mod._top_dir(Path("AGENTS.md")) is None
    assert mod._top_dir(Path("docs/readme.md")) is None


def test_numbered_top_is_the_eight_categories() -> None:
    mod = import_source()
    assert mod.NUMBERED_TOP == (
        "00_System",
        "01_Inbox",
        "02_Daily",
        "03_Sources",
        "04_Evergreen",
        "05_Knowledge",
        "06_Projects",
        "07_Archives",
    )


def test_content_digest_follows_changes(tmp_path: Path) -> None:
    dest, _ = _materialise(tmp_path)
    mod = import_source()
    d1 = mod._content_digest(dest)
    (dest / "00_System" / "new.md").write_text("a page")
    d2 = mod._content_digest(dest)
    assert d1 != d2


def test_state_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    mod = import_source()
    assert mod._load_state(root) == {}
    mod._save_state(root, {"u1": {"stage": "done"}})
    assert mod._load_state(root) == {"u1": {"stage": "done"}}
    import os
    assert os.stat(root / ".state" / "provisioned.json").st_mode & 0o077 == 0


def test_fetch_users_accepts_the_open_webui_012_envelope() -> None:
    """0.12 paginates /users/ as {"users": [...], "total": N}; earlier minors a list."""
    mod = import_source()

    def _http_json(url, method, token, body=None):
        if method == "GET" and url == "http://x/api/v1/users/":
            return 200, {"users": [{"id": "u1", "role": "admin"}], "total": 1}
        raise AssertionError(f"unexpected call {method} {url}")

    orig = mod._http_json
    mod._http_json = _http_json
    try:
        users = mod.fetch_users("http://x", "tok")
    finally:
        mod._http_json = orig
    assert users == [{"id": "u1", "role": "admin"}]


def test_fetch_users_still_accepts_a_bare_list() -> None:
    mod = import_source()

    def _http_json(url, method, token, body=None):
        return 200, [{"id": "u1"}]

    orig = mod._http_json
    mod._http_json = _http_json
    try:
        assert mod.fetch_users("http://x", "tok") == [{"id": "u1"}]
    finally:
        mod._http_json = orig