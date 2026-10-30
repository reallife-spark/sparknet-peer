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
    "INTENT.md",
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

    spec = importlib.util.spec_from_file_location("sparknet_vault_provisioner", str(PROVISIONER))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class FakeWebUI:
    """A recording stand-in for the WebUI admin API the provisioner calls.

    Enough of the surface for ``provision_user``: KB create, KB files list,
    file add, dir create/move, model create. ``kb_files`` is what the KB
    already holds, so a test can prove a page is reused and not re-uploaded.
    """

    def __init__(self, kb_files: list[dict] | None = None, missing_kbs: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.kb_files = kb_files or []
        self.missing_kbs = missing_kbs or set()
        self.kb_id = "kb-created"

    def http(self, url: str, method: str, token: str | None, body: object = None):
        self.calls.append((method, url, body))
        if method == "GET" and url.endswith("/files"):
            kb_id = url.split("/knowledge/", 1)[1].rsplit("/files", 1)[0]
            if kb_id in self.missing_kbs:
                return 400, {"detail": "We could not find what you're looking for :/"}
            return 200, {"files": list(self.kb_files), "directories": []}
        if url.endswith("/api/v1/knowledge/create"):
            return 200, {"id": self.kb_id}
        if url.endswith("/file/add"):
            return 200, {"id": "ok"}
        if url.endswith("/dirs/create"):
            return 200, {"id": f"dir-{body['name']}"}
        if url.endswith("/file/move"):
            return 200, {}
        if url.endswith("/api/v1/models/create"):
            return 200, {"id": body["id"]}
        return 200, {}


def _stub_http(mod, fake: FakeWebUI) -> None:
    mod._http_json = fake.http


def _stub_upload(mod) -> list[str]:
    """Replace the file upload with a recorder; return the list it appends to."""
    uploads: list[str] = []

    def record(base, token, page):
        uploads.append(page.name)
        return f"fid-{page.name}"

    mod._upload_and_embed = record
    return uploads


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


def test_existing_kb_files_maps_basename_to_id() -> None:
    mod = import_source()

    def _http_json(url, method, token, body=None):
        return 200, {
            "files": [{"name": "a.md", "id": "1"}, {"name": "b.md", "file_id": "2"}, {"name": "nameless"}],
            "directories": [],
        }

    orig = mod._http_json
    mod._http_json = _http_json
    try:
        assert mod.existing_kb_files("http://x", "t", "kb") == {"a.md": "1", "b.md": "2"}
    finally:
        mod._http_json = orig


def test_provision_user_resumes_when_the_vault_already_exists(tmp_path: Path) -> None:
    """An interrupted pass (folder present, stage != done) must continue, not refuse."""
    root = tmp_path / "knowledge"
    vault = root / "peer-uid1"
    mod = import_source()
    mod.materialise_vault_slice(TEMPLATE, vault)
    mod._save_state(root, {"uid1": {"stage": "embed", "kb_id": "kb-existing", "vault_path": str(vault)}})

    def _no_materialise(*a, **k):  # pragma: no cover - only fires on a regression
        raise AssertionError("a resumed pass must not re-materialise the vault")

    orig_mat = mod.materialise_vault_slice
    mod.materialise_vault_slice = _no_materialise
    fake = FakeWebUI()
    orig_http = mod._http_json
    orig_upload = mod._upload_and_embed
    _stub_http(mod, fake)
    _stub_upload(mod)
    try:
        staged = mod.provision_user("http://x", "tok", TEMPLATE, root, {"id": "uid1", "name": "A", "role": "user"})
    finally:
        mod.materialise_vault_slice = orig_mat
        mod._http_json = orig_http
        mod._upload_and_embed = orig_upload

    assert staged["stage"] == "done"
    assert staged["kb_id"] == "kb-existing"
    assert staged["model_id"] == "peer-uid1"
    creates = [c for c in fake.calls if str(c[1]).endswith("/knowledge/create")]
    assert creates == [], "a resumed pass must reuse the recorded KB, not create a second"
    assert mod._load_state(root)["uid1"]["stage"] == "done"


def test_provision_user_skips_pages_already_in_the_kb(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    mod = import_source()
    mod.materialise_vault_slice(TEMPLATE, root / "peer-uid2")
    fake = FakeWebUI(kb_files=[{"name": "user.md", "id": "fid-user"}])
    orig_http = mod._http_json
    orig_upload = mod._upload_and_embed
    _stub_http(mod, fake)
    uploads = _stub_upload(mod)
    try:
        staged = mod.provision_user("http://x", "tok", TEMPLATE, root, {"id": "uid2", "name": "B", "role": "user"})
    finally:
        mod._http_json = orig_http
        mod._upload_and_embed = orig_upload

    assert staged["stage"] == "done"
    assert "user.md" not in uploads, "a page already in the KB must not be uploaded again"
    assert uploads, "the other vault pages should still be embedded"


def test_provision_user_fresh_materialises_and_completes(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    mod = import_source()
    fake = FakeWebUI()
    orig_http = mod._http_json
    orig_upload = mod._upload_and_embed
    _stub_http(mod, fake)
    _stub_upload(mod)
    try:
        staged = mod.provision_user("http://x", "tok", TEMPLATE, root, {"id": "uid3", "name": "C", "role": "user"})
    finally:
        mod._http_json = orig_http
        mod._upload_and_embed = orig_upload

    assert (root / "peer-uid3" / "AGENTS.md").exists()
    assert staged["stage"] == "done"


def test_provision_user_mints_fresh_kb_if_recorded_kb_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    vault = root / "peer-uid4"
    mod = import_source()
    mod.materialise_vault_slice(TEMPLATE, vault)
    mod._save_state(root, {"uid4": {"stage": "embed", "kb_id": "kb-dead", "vault_path": str(vault)}})

    fake = FakeWebUI(missing_kbs={"kb-dead"})
    orig_http = mod._http_json
    orig_upload = mod._upload_and_embed
    _stub_http(mod, fake)
    _stub_upload(mod)
    try:
        staged = mod.provision_user("http://x", "tok", TEMPLATE, root, {"id": "uid4", "name": "D", "role": "user"})
    finally:
        mod._http_json = orig_http
        mod._upload_and_embed = orig_upload

    assert staged["stage"] == "done"
    assert staged["kb_id"] == "kb-created"
    creates = [c for c in fake.calls if str(c[1]).endswith("/knowledge/create")]
    assert len(creates) == 1, "a missing recorded KB must force creating a new one"


def test_prune_marks_vanished_users_orphaned(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    mod = import_source()
    mod._save_state(
        root,
        {
            "gone": {"stage": "done", "vault_path": "/x/gone"},
            "here": {"stage": "done", "vault_path": "/x/here"},
        },
    )
    orphaned = mod._prune_vanished_users(root, [{"id": "here"}])
    assert orphaned == ["gone"]
    state = mod._load_state(root)
    assert state["gone"]["stage"] == "orphaned"
    assert isinstance(state["gone"]["orphaned_at"], int)
    assert state["here"]["stage"] == "done"


def test_prune_ignores_an_empty_user_list(tmp_path: Path) -> None:
    """A transient empty user read must never mass-orphan every record."""
    root = tmp_path / "knowledge"
    mod = import_source()
    mod._save_state(root, {"gone": {"stage": "done"}})
    assert mod._prune_vanished_users(root, []) == []
    assert mod._load_state(root)["gone"]["stage"] == "done"
