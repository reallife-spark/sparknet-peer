"""Open WebUI workspace wiring (`scripts/sparknet-webui-workspace.py`).

The workspace tool is the reproducible, committed counterpart to live admin
clicks: KB directories, skills import, tool-server registration, structure-KB
rebuild and default model. These tests pin down the parts a file can prove
without a live WebUI:

* `--help` answers without requiring an admin token (fabric-wide rule);
* the skills ZIP builder wraps each skill directory that carries a SKILL.md,
  and emits one `create` decision per package;
* the bridge tool-server connection matches the Open WebUI 0.12.0
  `ToolServerConnection` shape (url/path/type/auth_type/key);
* the corpus rebuild uses the namespaced rag-corpus builder, so no two files
  share a basename (Open WebUI flattens filenames).
"""

from __future__ import annotations

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
        (root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\nbody\n"
        )
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


def test_corpus_rebuild_uses_namespaced_names(tmp_path: Path) -> None:
    """The rebuild relies on the committed, namespaced rag-corpus builder."""
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
    def _http_json_patched(base, path, method, token, body=None):
        assert method == "GET" and path == "/api/v1/knowledge/"
        return 200, [{"id": "kb-1", "name": "sparknet-structure"}, {"id": "kb-2", "name": "other"}]

    original = ws._http_json
    ws._http_json = _http_json_patched
    try:
        assert ws._knowledge_by_name("http://x", "tok", "sparknet-structure") == "kb-1"
        assert ws._knowledge_by_name("http://x", "tok", "nope") is None
    finally:
        ws._http_json = original