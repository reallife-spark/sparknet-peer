"""Open WebUI workspace wiring (`scripts/sparknet-webui-workspace.py`).

The workspace tool is the reproducible, committed counterpart to live admin
clicks: KB directories, skills import, tool-server registration, structure-KB
rebuild and default model. These tests pin down the parts a file can prove
without a live WebUI:

* `--help` answers without requiring an admin token (fabric-wide rule);
* the skills ZIP builder wraps each skill directory that carries a SKILL.md,
  and emits one `create` decision per package;
* the bridge tool-server connection matches the Open WebUI 0.12.0
  `ToolServerConnection` shape (url/path/type/auth_type/key), including the
  `config.enable` flag without which `get_tool_servers` hides the server;
* the structure-KB rebuild is idempotent against 0.12's
  `has_duplicate_content` guard: pages reuse their global file record by
  content hash and file into their numbered directory at add time.
"""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

VAULT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = VAULT_ROOT / "scripts" / "sparknet-webui-workspace.py"


def _load():
    spec = importlib.util.spec_from_file_location("sparknet_webui_workspace", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ws = _load()


def test_help_exits_zero_and_needs_no_state(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode == 0
    assert "kb-dirs" in proc.stdout
    assert "skills-import" in proc.stdout
    assert "tools-register" in proc.stdout


def test_build_skills_zip_wraps_each_skill_and_decides_create(tmp_path: Path) -> None:
    import io as _io

    root = tmp_path / "skills"
    for name in ("plan-writer", "vault-curator"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n")
        (root / name / "ref.txt").write_text("helper")
    (root / "LICENSE").mkdir(exist_ok=True)
    data, decisions = ws.build_skills_zip(root)

    assert len(decisions) == 2
    assert all(d == {"action": "create"} for d in decisions)
    with zipfile.ZipFile(_io.BytesIO(data)) as archive:
        names = archive.namelist()
    assert "plan-writer/SKILL.md" in names
    assert "vault-curator/ref.txt" in names
    assert "vault-curator/SKILL.md" in names


def test_skill_dirs_only_picks_directories_with_skil_md(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / "good").mkdir(parents=True)
    (root / "good" / "SKILL.md").write_text("---\nname: good\n---\n")
    (root / "empty").mkdir()
    assert [p.name for p in ws.skill_dirs(root)] == ["good"]


def test_bridge_connection_matches_the_webui_tool_server_shape() -> None:
    conn = ws.bridge_connection("abc.def", url="http://127.0.0.1:4096", spec_path="/v1/openapi.json")
    assert conn["type"] == "openapi"
    assert conn["auth_type"] == "bearer"
    assert conn["url"] == "http://127.0.0.1:4096"
    assert conn["path"] == "/v1/openapi.json"
    assert conn["key"] == "abc.def"
    for required in ("url", "path", "type", "auth_type", "forward_cookies", "headers", "key", "config"):
        assert required in conn
    assert conn["config"].get("enable") is True, (
        "a connection without config.enable is never surfaced by get_tool_servers"
    )


def test_corpus_builder_namespaces_role_charters(tmp_path: Path) -> None:
    """The rag-corpus builder (RAG ingest, not the KB rebuild) still has to
    namespace role charters so its own duplicate-basename rule holds."""
    corpus = ws._load_corpus(Path(__file__).resolve().parent.parent.parent)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "AGENTS.md").write_text("root")
    (vault / "peer-node").mkdir()
    (vault / "peer-node" / "AGENTS.md").write_text("peer")
    (vault / "master-node").mkdir()
    (vault / "master-node" / "AGENTS.md").write_text("master")

    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        written = corpus.build_corpus(vault, Path(tmp))
        names = [p.name for p in written]
        assert len(names) == len(set(names)), names
        assert {"AGENTS.md", "peer-node__AGENTS.md", "master-node__AGENTS.md"} == set(names)


def test_knowledge_by_name_returns_matching_id() -> None:
    def _http_json_patched(base, path, method, token, body=None, timeout=None):
        assert method == "GET" and path == "/api/v1/knowledge/"
        return 200, [{"id": "kb-1", "name": "sparknet-structure"}, {"id": "kb-2", "name": "other"}]

    original = ws._http_json
    ws._http_json = _http_json_patched
    try:
        assert ws._knowledge_by_name("http://x", "tok", "sparknet-structure") == "kb-1"
        assert ws._knowledge_by_name("http://x", "tok", "nope") is None
    finally:
        ws._http_json = original


def test_knowledge_by_name_accepts_the_open_webui_012_envelope() -> None:
    """0.12 paginates /knowledge/ as {"items": [...]}; earlier minors a list."""

    def _http_json_patched(base, path, method, token, body=None, timeout=None):
        assert method == "GET" and path == "/api/v1/knowledge/"
        return 200, {"items": [{"id": "kb-1", "name": "sparknet-structure"}], "total": 1}

    original = ws._http_json
    ws._http_json = _http_json_patched
    try:
        assert ws._knowledge_by_name("http://x", "tok", "sparknet-structure") == "kb-1"
    finally:
        ws._http_json = original


def test_knowledge_delete_uses_the_012_id_delete_route() -> None:
    """0.12 deletes a KB at {id}/delete; the bare {id} DELETE returns 405."""

    def _http_json_patched(base, path, method, token, body=None, timeout=None):
        assert method == "DELETE" and path == "/api/v1/knowledge/kb-1/delete"
        return 200, {"id": "kb-1"}

    original = ws._http_json
    ws._http_json = _http_json_patched
    try:
        ws._delete_knowledge("http://x", "tok", "kb-1")
    finally:
        ws._http_json = original


def test_add_file_to_kb_passes_directory_id_when_given() -> None:
    """0.12 files go straight into their numbered directory at add time."""
    seen: dict = {}

    def _http_json_patched(base, path, method, token, body=None, timeout=None):
        seen["body"] = body
        return 200, {"done": True}

    original = ws._http_json
    ws._http_json = _http_json_patched
    try:
        ws._add_file_to_kb("http://x", "tok", "kb-1", "f-1", directory_id="dir-7")
    finally:
        ws._http_json = original
    assert seen["body"] == {"file_id": "f-1", "directory_id": "dir-7"}


def test_add_file_to_kb_without_directory_id_omits_the_key() -> None:
    seen: dict = {}

    def _http_json_patched(base, path, method, token, body=None, timeout=None):
        seen["body"] = body
        return 200, {"done": True}

    original = ws._http_json
    ws._http_json = _http_json_patched
    try:
        ws._add_file_to_kb("http://x", "tok", "kb-1", "f-1")
    finally:
        ws._http_json = original
    assert seen["body"] == {"file_id": "f-1"}


def test_find_file_id_reuses_a_global_record_by_content_hash(tmp_path: Path) -> None:
    """Re-adding identical content must reuse the recorded file id, or 0.12's
    has_duplicate_content guard fails the add with a 400."""
    page = tmp_path / "p.md"
    page.write_text("hello vault")
    summary = hashlib.sha256(b"hello vault").hexdigest()

    def _http_json_patched(base, path, method, token, body=None, timeout=None):
        assert (path, method) == ("/api/v1/files/", "GET")
        return 200, {
            "items": [
                {"id": "f-old", "hash": summary},
            ],
            "total": 1,
        }

    original = ws._http_json
    ws._http_json = _http_json_patched
    try:
        found = ws._find_file_id("http://x", "tok", page)
    finally:
        ws._http_json = original
    assert found == "f-old"


def test_find_file_id_returns_none_when_content_is_new(tmp_path: Path) -> None:
    page = tmp_path / "p.md"
    page.write_text("fresh content")

    def _http_json_patched(base, path, method, token, body=None, timeout=None):
        return 200, {"items": [], "total": 0}

    original = ws._http_json
    ws._http_json = _http_json_patched
    try:
        found = ws._find_file_id("http://x", "tok", page)
    finally:
        ws._http_json = original
    assert found is None


def test_upload_and_embed_runs_process_even_with_a_background_embed(tmp_path: Path) -> None:
    """0.12 uploads with process=true embed asynchronously; we do upload +
    synchronous /retrieval/process/file so a follow-up add never races
    EMPTY_CONTENT."""
    calls: list[tuple] = []
    page = tmp_path / "p.md"
    page.write_text("body")

    def _multipart_patched(base, path, token, fields):
        calls.append(("multipart", path, [n for n, _ in fields]))
        return 200, {"id": "f-1"}

    def _http_json_patched(base, path, method, token, body=None, timeout=None):
        calls.append((method, path, body))
        return 200, {"done": True}

    a, b = ws._multipart, ws._http_json
    ws._multipart, ws._http_json = _multipart_patched, _http_json_patched
    try:
        fid = ws._upload_and_embed("http://x", "tok", page)
    finally:
        ws._multipart, ws._http_json = a, b
    assert fid == "f-1"
    assert ("multipart", "/api/v1/files/", ["file", "process"]) in calls
    assert ("POST", "/api/v1/retrieval/process/file", {"file_id": "f-1"}) in calls
    assert any(method == "GET" for method, *_ in calls) is False


def test_functions_import_posts_the_memory_filter(tmp_path: Path, monkeypatch) -> None:
    filter_file = tmp_path / "filter.py"
    filter_file.write_text("class Filter: pass\n")
    calls: list[tuple] = []

    def fake_http(base, path, method, token, body=None, timeout=60):
        calls.append((base, path, method, token, body))
        return 200, {"ok": True}

    monkeypatch.setattr(ws, "_http_json", fake_http)
    assert ws.cmd_functions_import("http://127.0.0.1:8181", "token", filter_file) == 0
    _base, path, method, token, body = calls[-1]
    assert path == "/api/v1/functions/create"
    assert method == "POST"
    assert token == "token"
    assert body["id"] == "sparknet-memory"
    assert body["type"] == "filter"
    assert "class Filter" in body["content"]
