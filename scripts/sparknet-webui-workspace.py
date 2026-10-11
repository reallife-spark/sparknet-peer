#!/usr/bin/env python3
"""Open WebUI workspace wiring for a sparknet node (peer PLAN Step 12).

Idempotent, admin-token operations that wire the node's Open WebUI surface to
its local fabric and vault, so a redeploy can reproduce the workspace instead of
clicking it. Runs against the node's own loopback WebUI (`127.0.0.1:8181`):

    status                     sign in and summarize users / KBs / skills / tools
    kb-dirs <kb_id>            ensure the `00_System`..`07_Archives` knowledge
                               directories exist inside a KB
    skills-import              import the vault's `.opencode/skills` into
                               Workspace > Skills (resources/skills = one dir per
                               skill, SKILL.md at its root)
    tools-register             register the dispatch bridge as the vault OpenAPI
                               tool server (`/v1/openapi.json`, bearer = the
                               bridge grant token) via POST /configs/tool_servers
    rebuild-structure          rebuild the `sparknet-structure` KB from
                               scripts/sparknet-rag-corpus.py (namespaced role
                               charters — no two files share a basename)
    default-model <model>      set the node's default chat model
                               (config `ui.default_models`)

The admin token, WebUI URL and password file come from the same env the
provisioner uses, so there remains exactly one signed authorization path per
node.

WebUI 0.12.0 API surface used (verified live on .131):
  POST /api/v1/auths/signin, GET /api/v1/users/
  GET/POST /api/v1/knowledge/ , /api/v1/knowledge/{id}
  POST /api/v1/files/ (multipart), /api/v1/knowledge/{id}/file/add
  POST /api/v1/knowledge/{id}/dirs/create, /{id}/file/move
  POST /api/v1/skills/import (multipart: files + decisions)
  GET/POST /api/v1/configs/tool_servers, GET/POST /api/v1/configs/models
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NUMBERED = (
    "00_System",
    "01_Inbox",
    "02_Daily",
    "03_Sources",
    "04_Evergreen",
    "05_Knowledge",
    "06_Projects",
    "07_Archives",
)

DEFAULT_KB_NAME = "sparknet-structure"
DEFAULT_BRIDGE_URL = "http://127.0.0.1:4096"
DEFAULT_SPEC_PATH = "/v1/openapi.json"


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


class Failure(RuntimeError):
    """Any step that refuses to complete. Message is the reason."""


# --- admin session ------------------------------------------------------------


def _load_provisioner() -> Any:
    """Load services/sparknet-vault-provisioner.py (sibling tree) by path.

    The provisioner is the one admin-token plumbing on the node; reusing its
    helpers keeps exactly one sign-in/JSON path.
    """
    here_dir = Path(__file__).resolve().parent
    candidates = [here_dir.parent / "services", here_dir.parent.parent / "services"]
    for parent in candidates:
        cand = (parent / "sparknet-vault-provisioner.py").resolve()
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("_sparknet_provisioner_ws", cand)
            if spec is None or spec.loader is None:
                raise Failure(f"cannot import provisioner at {cand}")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise Failure("cannot locate services/sparknet-vault-provisioner.py")


def _session(prov: Any, webui: str, email: str, password_file: Path) -> str:
    password_file = password_file.expanduser()
    if not password_file.is_file():
        raise Failure(f"missing admin password file: {password_file}")
    password = password_file.read_text().strip()
    return prov.webui_token(webui, email, password)


def _http_json(base: str, path: str, method: str, token: str, body: Any = None):
    req = urllib.request.Request(f"{base}{path}", method=method)
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw.decode("utf-8", "replace")
        return e.code, detail


def _multipart(base: str, path: str, token: str, fields: list[tuple[str, Any]]) -> Any:
    """POST a multipart form. `fields` are (name, value) or (name, (filename, bytes))."""
    boundary = "sparknet-" + hashlib.sha256(os.urandom(16)).hexdigest()
    parts: list[bytes] = []
    for name, value in fields:
        if isinstance(value, tuple):
            fname, content = value
            parts += [
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n".encode(),
                content if isinstance(content, bytes) else content.encode("utf-8"),
                b"\r\n",
            ]
        else:
            parts += [
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n".encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
    parts.append(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(f"{base}{path}", method="POST", data=b"".join(parts))
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw.decode("utf-8", "replace")
        return e.code, detail


# --- knowledge directories ----------------------------------------------------


def cmd_kb_dirs(prov: Any, webui: str, token: str, kb_id: str) -> int:
    dirs = prov.ensure_directories(webui, token, kb_id, prov.NUMBERED_TOP)
    log(f"{kb_id} directories: " + ", ".join(sorted(dirs)))
    return 0


# --- skills -------------------------------------------------------------------


def skill_dirs(skills_root: Path) -> list[Path]:
    """Skill directories (those that carry a SKILL.md), sorted."""
    result = []
    for child in sorted(skills_root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / "SKILL.md").is_file():
            result.append(child)
    return result


def build_skills_zip(skills_root: Path) -> tuple[bytes, list[dict]]:
    """One ZIP whose roots are `<skill>/SKILL.md`, plus per-package decisions."""
    buffer = io.BytesIO()
    roots = skill_dirs(skills_root)
    if not roots:
        raise Failure(f"no skill directories (with SKILL.md) under {skills_root}")
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for skill in roots:
            for path in sorted(skill.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                arc = f"{skill.name}/{path.relative_to(skill).as_posix()}"
                archive.writestr(arc, path.read_bytes())
    return buffer.getvalue(), [{"action": "create"} for _ in roots]


def cmd_skills_import(webui: str, token: str, skills_root: Path) -> int:
    zip_bytes, decisions = build_skills_zip(skills_root)
    status, resp = _multipart(
        webui,
        "/api/v1/skills/import",
        token,
        [
            ("files", ("skills.zip", zip_bytes)),
            ("decisions", json.dumps(decisions)),
        ],
    )
    if status != 200:
        raise Failure(f"skills import failed: HTTP {status}: {resp}")
    ok = [r for r in resp[:] if isinstance(r, dict) and r.get("status") == "saved"]
    errors = [r for r in resp if isinstance(r, dict) and r.get("status") == "error"]
    if errors:
        log(f"skills import: {len(ok)} saved, errors: {errors}")
    else:
        log(f"skills import: {len(ok)} saved")
    return 0 if not errors else 2


# --- tools --------------------------------------------------------------------


def bridge_connection(grant_token: str, url: str = DEFAULT_BRIDGE_URL,
                      spec_path: str = DEFAULT_SPEC_PATH) -> dict:
    return {
        "url": url,
        "path": spec_path,
        "type": "openapi",
        "auth_type": "bearer",
        "forward_cookies": False,
        "headers": None,
        "key": grant_token.strip(),
        "config": {},
        "info": {
            "name": "sparknet vault tools",
            "description": "the node's grant-checked vault tools (vault.list/cat/capture/promote/patch)",
        },
    }


def cmd_tools_register(webui: str, token: str, grant_file: Path,
                       url: str, spec_path: str) -> int:
    grant_file = grant_file.expanduser()
    if not grant_file.is_file():
        raise Failure(f"missing bridge grant token file: {grant_file}")
    grant = grant_file.read_text().strip()
    if not grant:
        raise Failure(f"empty bridge grant token: {grant_file}")

    status, resp = _http_json(webui, "/api/v1/configs/tool_servers", "GET", token)
    if status != 200 or not isinstance(resp, dict):
        raise Failure(f"tool server config read failed: HTTP {status}: {resp}")
    existing = resp.get("TOOL_SERVER_CONNECTIONS") or []

    conn = bridge_connection(grant, url=url, spec_path=spec_path)
    kept = [
        c for c in existing
        if not (isinstance(c, dict) and c.get("type") == "openapi"
                and str(c.get("url", "")).rstrip("/") == url.rstrip("/"))
    ]
    connections = [*kept, conn]

    status, resp = _http_json(
        webui, "/api/v1/configs/tool_servers", "POST", token,
        {"TOOL_SERVER_CONNECTIONS": connections},
    )
    if status != 200:
        raise Failure(f"tool server config write failed: HTTP {status}: {resp}")
    log(f"tool server registered: {url}{spec_path} ({len(kept)} kept, 1 installed)")
    return 0


# --- structure KB rebuild ------------------------------------------------------


def cmd_rebuild_structure(webui: str, token: str, vault_root: Path,
                          kb_name: str) -> int:
    from tempfile import TemporaryDirectory

    corpus_module = _load_corpus(vault_root)
    with TemporaryDirectory(prefix="sparknet-corpus-") as tmp:
        out = Path(tmp)
        written = corpus_module.build_corpus(vault_root, out)
        if not written:
            raise Failure(f"structure corpus is empty under {vault_root}")
        names = [p.name for p in written]
        if len(names) != len(set(names)):
            raise Failure(f"corpus still has duplicate basenames: {names}")

        existing = _knowledge_by_name(webui, token, kb_name)
        if existing is not None:
            status, resp = _http_json(webui, f"/api/v1/knowledge/{existing}", "DELETE", token)
            if status != 200:
                raise Failure(f"old {kb_name} delete failed: HTTP {status}: {resp}")
            log(f"deleted old {kb_name} ({existing})")

        status, resp = _http_json(webui, "/api/v1/knowledge/create", "POST", token, {
            "name": kb_name,
            "description": "structure-only fabric corpus (contracts + charters); role charters are namespaced",
            "access_grants": [],
        })
        if status != 200 or not isinstance(resp, dict) or "id" not in resp:
            raise Failure(f"knowledge create failed: HTTP {status}: {resp}")
        kb_id = resp["id"]

        added = 0
        for rel in written:
            path = out / rel
            status, resp = _multipart(webui, "/api/v1/files/", token,
                                      [("file", (path.name, path.read_bytes())),
                                       ("process", "true")])
            if status != 200 or not isinstance(resp, dict) or "id" not in resp:
                raise Failure(f"file upload failed {rel}: HTTP {status}: {resp}")
            file_id = resp["id"]
            status, resp = _http_json(webui, f"/api/v1/knowledge/{kb_id}/file/add",
                                      "POST", token, {"file_id": file_id})
            if status != 200:
                raise Failure(f"kb add failed {rel}: HTTP {status}: {resp}")
            added += 1
        log(f"{kb_name}: rebuilt with {added} files (kb {kb_id})")
    return 0


def _knowledge_by_name(webui: str, token: str, name: str) -> str | None:
    status, resp = _http_json(webui, "/api/v1/knowledge/", "GET", token)
    if status != 200 or not isinstance(resp, list):
        raise Failure(f"knowledge list failed: HTTP {status}")
    for item in resp:
        if isinstance(item, dict) and item.get("name") == name and item.get("id"):
            return item["id"]
    return None


def _load_corpus(vault_root: Path) -> Any:
    script = vault_root / "scripts" / "sparknet-rag-corpus.py"
    if not script.is_file():
        raise Failure(f"corpus builder missing: {script}")
    spec = importlib.util.spec_from_file_location("_sparknet_rag_corpus_ws", script)
    if spec is None or spec.loader is None:
        raise Failure(f"cannot import corpus builder: {script}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- default model -------------------------------------------------------------


def cmd_default_model(webui: str, token: str, model: str) -> int:
    status, resp = _http_json(webui, "/api/v1/configs/models", "POST", token,
                              {"DEFAULT_MODELS": model})
    if status != 200:
        raise Failure(f"default model update failed: HTTP {status}: {resp}")
    log(f"default chat model set to {model}")
    return 0


# --- status --------------------------------------------------------------------


def cmd_status(webui: str, token: str) -> int:
    status, users = _http_json(webui, "/api/v1/users/", "GET", token)
    if status != 200 or not isinstance(users, list):
        raise Failure(f"users list failed: HTTP {status}")
    status, kbs = _http_json(webui, "/api/v1/knowledge/", "GET", token)
    if status != 200 or not isinstance(kbs, list):
        raise Failure(f"knowledge list failed: HTTP {status}")
    status, skills = _http_json(webui, "/api/v1/skills/", "GET", token)
    skills = skills if status == 200 and isinstance(skills, list) else []
    status, tools = _http_json(webui, "/api/v1/tools/", "GET", token)
    tools = tools if status == 200 and isinstance(tools, list) else []

    log(f"users: {[(u.get('name'), u.get('role')) for u in users]}")
    log(f"knowledge: {[(k.get('name'), k.get('file_count')) for k in kbs]}")
    log(f"skills: {len(skills)}  tools: {len(tools)}")
    return 0


# --- main ----------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sparknet-webui-workspace",
        description="Wire a sparknet node's Open WebUI workspace (peer PLAN Step 12).",
    )
    p.add_argument("--webui", default=os.environ.get("SPARKNET_WEBUI") or "http://127.0.0.1:8181")
    p.add_argument("--admin-email", default=os.environ.get("SPARKNET_WEBUI_ADMIN_EMAIL")
                   or "admin@sparknet.local")
    p.add_argument("--admin-password-file", type=lambda s: Path(s).expanduser(),
                   default=Path(os.environ.get("SPARKNET_WEBUI_ADMIN_PASSWORD_FILE")
                                or "~/.local/state/sparknet/cluster2/webui-admin-password"))
    p.add_argument("--bridge-grant-file", type=lambda s: Path(s).expanduser(),
                   default=Path(os.environ.get("SPARKNET_WEBUI_BRIDGE_GRANT_FILE")
                                or "~/.local/state/sparknet/cluster2/webui-bridge-grant.token"))
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="sign in and summarize the surface")

    kb = sub.add_parser("kb-dirs", help="ensure the numbered knowledge directories in a KB")
    kb.add_argument("kb_id")

    ski = sub.add_parser("skills-import", help="import .opencode/skills into Workspace > Skills")
    ski.add_argument("--skills-dir", type=Path,
                     default=Path("~").expanduser() / "sparknet-cluster2" / ".opencode" / "skills")

    tools = sub.add_parser("tools-register", help="register the dispatch bridge as the vault tool server")
    tools.add_argument("--url", default=os.environ.get("SPARKNET_BRIDGE_URL") or DEFAULT_BRIDGE_URL)
    tools.add_argument("--spec-path", default=DEFAULT_SPEC_PATH)

    rs = sub.add_parser("rebuild-structure", help="rebuild the structure-only KB from the corpus")
    rs.add_argument("--vault-root", type=Path,
                    default=Path("~").expanduser() / "sparknet-cluster2")
    rs.add_argument("--kb-name", default=DEFAULT_KB_NAME)

    dm = sub.add_parser("default-model", help="set the default chat model")
    dm.add_argument("model")
    return p


def main(argv: list[str] | None = None) -> int:
    p = _parser()
    args = p.parse_args(argv)
    prov = _load_provisioner()
    token = _session(prov, args.webui, args.admin_email, args.admin_password_file)

    if args.command == "status":
        return cmd_status(args.webui, token)
    if args.command == "kb-dirs":
        return cmd_kb_dirs(prov, args.webui, token, args.kb_id)
    if args.command == "skills-import":
        return cmd_skills_import(args.webui, token, args.skills_dir)
    if args.command == "tools-register":
        return cmd_tools_register(args.webui, token, args.bridge_grant_file,
                                  args.url, args.spec_path)
    if args.command == "rebuild-structure":
        return cmd_rebuild_structure(args.webui, token, args.vault_root, args.kb_name)
    if args.command == "default-model":
        return cmd_default_model(args.webui, token, args.model)
    raise Failure(f"unknown command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failure as exc:
        print(f"sparknet-webui-workspace: {exc}", file=sys.stderr)
        raise SystemExit(1)