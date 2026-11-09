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
    rebuild-structure          rebuild the `sparknet-structure` KB as a faithful
                               mirror of the role vault (numbered 00_System..07
                               _Archives dirs; symlink shims skipped, so AGENTS.md
                               ships once, not three times)
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
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
from sparknet_loader import load_by_path  # noqa: E402 - shared/ is added by path above

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
            return load_by_path("_sparknet_provisioner_ws", cand)
    raise Failure("cannot locate services/sparknet-vault-provisioner.py")


def _session(prov: Any, webui: str, email: str, password_file: Path) -> str:
    password_file = password_file.expanduser()
    if not password_file.is_file():
        raise Failure(f"missing admin password file: {password_file}")
    password = password_file.read_text().strip()
    return prov.webui_token(webui, email, password)


def _http_json(base: str, path: str, method: str, token: str, body: Any = None, timeout: float = 60):
    req = urllib.request.Request(f"{base}{path}", method=method)
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
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
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n".encode(),
                content if isinstance(content, bytes) else content.encode("utf-8"),
                b"\r\n",
            ]
        else:
            parts += [
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
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


def bridge_connection(grant_token: str, url: str = DEFAULT_BRIDGE_URL, spec_path: str = DEFAULT_SPEC_PATH) -> dict:
    return {
        "url": url,
        "path": spec_path,
        "type": "openapi",
        "auth_type": "bearer",
        "forward_cookies": False,
        "headers": None,
        "key": grant_token.strip(),
        "config": {"enable": True},
        "info": {
            "name": "sparknet vault tools",
            "description": "the node's grant-checked vault tools (vault.list/cat/capture/promote/patch)",
        },
    }


def cmd_functions_import(webui: str, token: str, function_file: Path) -> int:
    """Install the memory filter as an Open WebUI Filter Function."""
    if not function_file.is_file():
        raise Failure(f"filter file missing: {function_file}")
    content = function_file.read_text(encoding="utf-8")
    payload = {
        "id": "sparknet-memory",
        "name": "Sparknet Memory",
        "type": "filter",
        "content": content,
        "meta": {"description": "Per-vault Sparknet memory recall and turn capture", "manifest": {}},
    }
    status, response = _http_json(webui, "/api/v1/functions/create", "POST", token, payload)
    if status not in (200, 201):
        raise Failure(f"function import failed: HTTP {status}: {response}")
    log("memory filter installed")
    return 0


def cmd_tools_register(webui: str, token: str, grant_file: Path, url: str, spec_path: str) -> int:
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
        c
        for c in existing
        if not (
            isinstance(c, dict) and c.get("type") == "openapi" and str(c.get("url", "")).rstrip("/") == url.rstrip("/")
        )
    ]
    connections = [*kept, conn]

    status, resp = _http_json(
        webui,
        "/api/v1/configs/tool_servers",
        "POST",
        token,
        {"TOOL_SERVER_CONNECTIONS": connections},
    )
    if status != 200:
        raise Failure(f"tool server config write failed: HTTP {status}: {resp}")
    log(f"tool server registered: {url}{spec_path} ({len(kept)} kept, 1 installed)")
    return 0


# --- workspace baseline -------------------------------------------------------

FUNCTION_IDS = {
    "sparknet_memory_filter.py": "sparknet_memory",
    "sparknet_capture.py": "sparknet_capture",
}
PUBLIC_READ = [{"principal_type": "user", "principal_id": "*", "permission": "read"}]
TOOL_VALVES = {
    "sparknet_vault": {"gateway_url": "http://127.0.0.1:4093"},
    "sparknet_memory": {"gateway_url": "http://127.0.0.1:4093"},
    "sparknet_rfi": {"gateway_url": "http://127.0.0.1:4093"},
    "sparknet_status": {"gateway_url": "http://127.0.0.1:4093", "engine_url": "http://127.0.0.1:4204"},
}
FUNCTION_VALVES = {
    "sparknet_memory": {"gateway_url": "http://127.0.0.1:4093", "capture": True, "top_k": 5},
    "sparknet_capture": {"gateway_url": "http://127.0.0.1:4093", "priority": 10},
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _function_type(content: str) -> str:
    if "class Action" in content:
        return "action"
    if "class Filter" in content:
        return "filter"
    if "class Pipe" in content:
        return "pipe"
    raise Failure("cannot determine function type (expected Action, Filter or Pipe)")


def _token_from_file(path: Path) -> str:
    try:
        value = path.expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value


def cmd_prompts_import(webui: str, token: str, prompts_path: Path) -> int:
    prompts = _read_json(prompts_path)
    if not isinstance(prompts, list):
        raise Failure("prompt file must be a JSON array")
    status, current = _http_json(webui, "/api/v1/prompts/", "GET", token)
    if status != 200:
        raise Failure(f"prompt list failed: HTTP {status}: {current}")
    by_command = {
        str(item.get("command")).lstrip("/"): item
        for item in (current or [])
        if isinstance(item, dict) and item.get("command")
    }
    created = updated = 0
    for item in prompts:
        if not isinstance(item, dict):
            continue
        form = dict(item)
        form["command"] = str(form.get("command") or "").lstrip("/")
        form.setdefault("access_grants", PUBLIC_READ)
        existing = by_command.get(form["command"])
        if existing and existing.get("id"):
            status, response = _http_json(webui, f"/api/v1/prompts/id/{existing['id']}/update", "POST", token, form)
            if status != 200:
                raise Failure(f"prompt update failed for {form['command']}: HTTP {status}: {response}")
            updated += 1
        else:
            status, response = _http_json(webui, "/api/v1/prompts/create", "POST", token, form)
            if status != 200:
                raise Failure(f"prompt create failed for {form['command']}: HTTP {status}: {response}")
            created += 1
    log(f"prompts: {created} created, {updated} updated")
    return 0


def cmd_tools_import(webui: str, token: str, tools_dir: Path, webui_token: str) -> int:
    if not webui_token:
        raise Failure("the WebUI service token is required to install tools")
    imported = 0
    for path in sorted(tools_dir.glob("*.py")):
        tool_id = path.stem
        content = path.read_text(encoding="utf-8")
        name = tool_id.replace("_", " ").title()
        payload = {
            "id": tool_id,
            "name": name,
            "content": content,
            "meta": {"description": f"Sparknet {name} (user-scoped, loopback gateway)"},
            "access_grants": PUBLIC_READ,
        }
        existing_status, _existing = _http_json(webui, f"/api/v1/tools/id/{tool_id}", "GET", token)
        if existing_status == 200:
            status, response = _http_json(webui, f"/api/v1/tools/id/{tool_id}/update", "POST", token, payload)
        else:
            status, response = _http_json(webui, "/api/v1/tools/create", "POST", token, payload)
        if status not in (200, 201):
            raise Failure(f"tool install failed for {tool_id}: HTTP {status}: {response}")
        valves = dict(TOOL_VALVES.get(tool_id, {}))
        valves["webui_token"] = webui_token
        status, response = _http_json(webui, f"/api/v1/tools/id/{tool_id}/valves/update", "POST", token, valves)
        if status not in (200, 201):
            raise Failure(f"tool valves failed for {tool_id}: HTTP {status}: {response}")
        imported += 1
    log(f"tools: {imported} installed/updated")
    return 0


def cmd_functions_import_all(webui: str, token: str, functions_dir: Path, webui_token: str) -> int:
    if not webui_token:
        raise Failure("the WebUI service token is required to install functions")
    imported = 0
    for path in sorted(functions_dir.glob("*.py")):
        function_id = FUNCTION_IDS.get(path.name, path.stem)
        content = path.read_text(encoding="utf-8")
        function_type = _function_type(content)
        payload = {
            "id": function_id,
            "name": function_id.replace("_", " ").title(),
            "content": content,
            "meta": {"description": f"Sparknet {function_id.replace('_', ' ')}"},
        }
        existing_status, _existing = _http_json(webui, f"/api/v1/functions/id/{function_id}", "GET", token)
        if existing_status == 200:
            status, response = _http_json(webui, f"/api/v1/functions/id/{function_id}/update", "POST", token, payload)
        else:
            status, response = _http_json(webui, "/api/v1/functions/create", "POST", token, payload)
        if status not in (200, 201):
            raise Failure(f"function install failed for {function_id}: HTTP {status}: {response}")
        status, current = _http_json(webui, f"/api/v1/functions/id/{function_id}", "GET", token)
        if status != 200 or not isinstance(current, dict):
            raise Failure(f"function read failed for {function_id}: HTTP {status}: {current}")
        if not current.get("is_active"):
            status, response = _http_json(webui, f"/api/v1/functions/id/{function_id}/toggle", "POST", token, {})
            if status != 200:
                raise Failure(f"function activation failed for {function_id}: HTTP {status}: {response}")
        if function_type in ("filter", "action") and not current.get("is_global"):
            status, response = _http_json(webui, f"/api/v1/functions/id/{function_id}/toggle/global", "POST", token, {})
            if status != 200:
                raise Failure(f"function global toggle failed for {function_id}: HTTP {status}: {response}")
        valves = dict(FUNCTION_VALVES.get(function_id, {}))
        if valves:
            valves["webui_token"] = webui_token
            status, response = _http_json(
                webui, f"/api/v1/functions/id/{function_id}/valves/update", "POST", token, valves
            )
            if status != 200:
                raise Failure(f"function valves failed for {function_id}: HTTP {status}: {response}")
        imported += 1
    log(f"functions: {imported} installed/updated and active/global where applicable")
    return 0


def cmd_settings_apply(webui: str, token: str, settings_path: Path, engine_token: str) -> int:
    settings = _read_json(settings_path)
    if not isinstance(settings, dict):
        raise Failure("settings file must be a JSON object")
    if "__ENGINE_TOKEN__" in json.dumps(settings):
        if not engine_token:
            raise Failure("the engine token is required for the OpenAI connection")
        settings = json.loads(json.dumps(settings).replace("__ENGINE_TOKEN__", engine_token))
    status, response = _http_json(webui, "/api/v1/configs/import", "POST", token, {"config": settings})
    if status != 200:
        raise Failure(f"settings import failed: HTTP {status}: {response}")
    suggestions = [
        {"title": ["Plan a change", "Änderung planen"], "content": "/plan "},
        {"title": ["Build in my vault", "In meinem Vault bauen"], "content": "/build "},
        {"title": ["Ask the Master", "Master fragen"], "content": "/rfi "},
        {"title": ["Search my memory", "Gedächtnis durchsuchen"], "content": "/memory "},
    ]
    status, response = _http_json(webui, "/api/v1/configs/suggestions", "POST", token, {"suggestions": suggestions})
    if status != 200:
        log(f"prompt suggestions update failed (non-fatal): HTTP {status}: {response}")
    log(f"settings applied from {settings_path.name}")
    return 0


def _models(webui: str, token: str) -> list[dict]:
    status, response = _http_json(webui, "/api/v1/models/all", "GET", token)
    if status == 404:
        status, response = _http_json(webui, "/api/v1/models/list", "GET", token)
    if status != 200:
        raise Failure(f"model list failed: HTTP {status}: {response}")
    if isinstance(response, list):
        return response
    if isinstance(response, dict) and isinstance(response.get("data"), list):
        return response["data"]
    if isinstance(response, dict) and isinstance(response.get("items"), list):
        return response["items"]
    return []


def _normalize_knowledge_items(items: Any) -> list[dict]:
    """0.13 expects meta.knowledge entries to be objects with an ``id``.

    Older rows carry bare KB id strings; the knowledge-delete route then calls
    ``k.get('id')`` on a str and returns 500. Normalise every row before any
    deletion or model update.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for item in items or []:
        if isinstance(item, str):
            entry = {"id": item}
        elif isinstance(item, dict) and item.get("id"):
            entry = dict(item)
        else:
            continue
        key = str(entry["id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def cmd_models_migrate(webui: str, token: str, base_model: str) -> int:
    structure_id = _knowledge_by_name(webui, token, DEFAULT_KB_NAME)
    changed = 0
    for model in _models(webui, token):
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or "")
        if not model_id.startswith("peer-"):
            continue
        meta = model.get("meta") if isinstance(model.get("meta"), dict) else {}
        knowledge = _normalize_knowledge_items(meta.get("knowledge"))
        if structure_id and not any(str(item.get("id")) == structure_id for item in knowledge):
            knowledge.append({"id": structure_id})
        meta["knowledge"] = knowledge
        form = {
            "id": model_id,
            "name": model.get("name") or model_id,
            "base_model_id": base_model,
            "meta": meta,
            "params": model.get("params") or {},
            "access_grants": model.get("access_grants") or [],
            "is_active": True,
        }
        if model.get("base_model_id") == base_model and not structure_id:
            continue
        status, response = _http_json(webui, "/api/v1/models/model/update", "POST", token, form)
        if status != 200:
            raise Failure(f"model update failed for {model_id}: HTTP {status}: {response}")
        changed += 1
    log(f"models: {changed} per-user model(s) moved to {base_model}")
    return 0


def cmd_structure_corpus(webui: str, token: str, vault_root: Path, kb_name: str, corpus_script: Path) -> int:
    if not corpus_script.is_file():
        raise Failure(f"corpus builder missing: {corpus_script}")
    with tempfile.TemporaryDirectory(prefix="sparknet-corpus-") as tmp:
        out = Path(tmp) / "corpus"
        result = subprocess.run(
            [sys.executable, str(corpus_script), str(vault_root), "--allow-list", "node", "--out", str(out)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise Failure(f"corpus build failed: {result.stderr.strip() or result.stdout.strip()}")
        existing = _knowledge_by_name(webui, token, kb_name)
        if existing is not None:
            _delete_knowledge(webui, token, existing)
        status, response = _http_json(
            webui,
            "/api/v1/knowledge/create",
            "POST",
            token,
            {
                "name": kb_name,
                "description": "structure-only fabric corpus (allow-list; no personal pages)",
                "access_grants": PUBLIC_READ,
            },
        )
        if status != 200 or not isinstance(response, dict) or not response.get("id"):
            raise Failure(f"knowledge create failed: HTTP {status}: {response}")
        kb_id = response["id"]
        dir_ids: dict[str, str] = {}
        for child in sorted(out.rglob("*")):
            if not child.is_file() or "__pycache__" in child.parts:
                continue
            rel = child.relative_to(out)
            top = rel.parts[0] if len(rel.parts) > 1 else ""
            directory_id = None
            if top:
                if top not in dir_ids:
                    status, dir_response = _http_json(
                        webui, f"/api/v1/knowledge/{kb_id}/dirs/create", "POST", token, {"name": top}
                    )
                    if status == 200 and isinstance(dir_response, dict):
                        dir_ids[top] = str(dir_response.get("id") or "")
                directory_id = dir_ids.get(top) or None
            file_id = _upload_and_embed(webui, token, child)
            _add_file_to_kb(webui, token, kb_id, file_id, directory_id)
        status, response = _http_json(
            webui,
            f"/api/v1/knowledge/{kb_id}/access/update",
            "POST",
            token,
            {"access_grants": PUBLIC_READ},
        )
        if status != 200:
            log(f"structure KB public grant failed (non-fatal): HTTP {status}: {response}")
        log(f"{kb_name}: rebuilt from the allow-list only ({kb_id})")
    return 0


def cmd_reindex(webui: str, token: str) -> int:
    status, response = _http_json(webui, "/api/v1/knowledge/reindex", "POST", token, {})
    if status != 200:
        raise Failure(f"knowledge reindex failed: HTTP {status}: {response}")
    log("knowledge reindex requested")
    return 0


def cmd_apply(args: argparse.Namespace, prov: Any, token: str, webui_token: str, engine_token: str) -> int:
    root = Path(args.vault_root).resolve()
    webui_dir = Path(args.webui_dir).resolve()
    settings = Path(args.settings).resolve()
    cmd_settings_apply(args.webui, token, settings, engine_token)
    # First pass normalises legacy string knowledge entries and detaches the old
    # structure KB from models so the delete route cannot hit the 0.13 string bug.
    cmd_models_migrate(args.webui, token, args.base_model)
    corpus_root = root.parent if root.name in ("peer-node", "master-node") else root
    cmd_structure_corpus(
        args.webui,
        token,
        corpus_root,
        DEFAULT_KB_NAME,
        corpus_root / "scripts" / "sparknet-rag-corpus.py",
    )
    # Second pass attaches the newly built structure KB.
    cmd_models_migrate(args.webui, token, args.base_model)
    cmd_prompts_import(args.webui, token, webui_dir / "prompts" / "sparknet-prompts.json")
    cmd_tools_import(args.webui, token, webui_dir / "tools", webui_token)
    cmd_functions_import_all(args.webui, token, webui_dir / "functions", webui_token)
    cmd_reindex(args.webui, token)
    log("workspace baseline applied")
    return 0


# --- structure KB rebuild ------------------------------------------------------


def _find_file_id(webui: str, token: str, path: Path) -> str | None:
    """Return the global file id for ``path``'s content, if a record exists.

    Open WebUI `file/add` runs the file through the KB's vector collection and
    rejects any content whose hash is already present under a *different* file
    id (``has_duplicate_content``). Reusing the existing record by content hash
    makes a rebuild idempotent: the same page always resolves to the same file
    id, so re-adds re-index instead of failing with "Duplicate content".
    """
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    status, resp = _http_json(webui, "/api/v1/files/", "GET", token)
    if status != 200:
        return None
    for item in _collection(resp, "items") or []:
        if isinstance(item, dict) and item.get("hash") == digest:
            return item.get("id")
    return None


def _upload_and_embed(webui: str, token: str, path: Path) -> str:
    """Upload a file and embed it into its per-file collection, deterministically.

    Open WebUI 0.12 uploads with ``process=true`` embed in a background task and
    return before the per-file chunks exist; a `file/add` fired immediately then
    fails with EMPTY_CONTENT (a race, so it fails sporadically). Upload without
    processing, run the synchronous ``/retrieval/process/file``, then return the
    file id for the caller to add to a KB.
    """
    status, resp = _multipart(
        webui, "/api/v1/files/", token, [("file", (path.name, path.read_bytes())), ("process", "false")]
    )
    if status != 200 or not isinstance(resp, dict) or "id" not in resp:
        raise Failure(f"file upload failed {path}: HTTP {status}: {resp}")
    file_id = resp["id"]
    status, resp = _http_json(webui, "/api/v1/retrieval/process/file", "POST", token, {"file_id": file_id}, timeout=600)
    if status != 200:
        raise Failure(f"file process failed {path}: HTTP {status}: {resp}")
    return file_id


def _add_file_to_kb(webui: str, token: str, kb_id: str, file_id: str, directory_id: str | None = None) -> None:
    body: dict[str, Any] = {"file_id": file_id}
    if directory_id:
        body["directory_id"] = directory_id
    status, resp = _http_json(webui, f"/api/v1/knowledge/{kb_id}/file/add", "POST", token, body, timeout=600)
    if status != 200:
        raise Failure(f"kb add failed: HTTP {status}: {resp}")


def cmd_rebuild_structure(webui: str, token: str, vault_root: Path, kb_name: str) -> int:
    """Recreate the structure KB as a faithful mirror of the role vault.

    The per-user KB mirrors the node's numbered vault (00_System..07_Archives):
    every real page is uploaded, embedded, added and filed into its matching
    numbered directory in one pass (directory_id is set at add time, no second
    move stage). Symlink shims (index.md, log.md, category links) are skipped by
    the provisioner's page walk, so the root AGENTS.md appears exactly once and
    Open WebUI's flat file list stays free of charter dupes.

    Re-runs are idempotent: pages whose content already has a global file record
    are re-attached under that record's id (never re-uploaded), which bypasses
    Open WebUI's `has_duplicate_content` guard and restores any dropped
    membership instead of failing with a 400.
    """
    prov = _load_provisioner()

    existing = _knowledge_by_name(webui, token, kb_name)
    if existing is not None:
        _delete_knowledge(webui, token, existing)
        log(f"deleted old {kb_name} ({existing})")

    status, resp = _http_json(
        webui,
        "/api/v1/knowledge/create",
        "POST",
        token,
        {
            "name": kb_name,
            "description": "mirror of the node's role vault (numbered layout); role charters are namespaced",
            "access_grants": [],
        },
    )
    if status != 200 or not isinstance(resp, dict) or "id" not in resp:
        raise Failure(f"knowledge create failed: HTTP {status}: {resp}")
    kb_id = resp["id"]

    dirs = prov.ensure_directories(webui, token, kb_id, prov.NUMBERED_TOP)

    added = 0
    moved = 0
    reused = 0
    for page in prov._vault_pages(vault_root):
        rel = page.relative_to(vault_root)
        existing = _find_file_id(webui, token, page)
        if existing:
            file_id = existing
            reused += 1
        else:
            file_id = _upload_and_embed(webui, token, page)
        top = prov._top_dir(rel)
        directory_id = dirs.get(top) if top else None
        _add_file_to_kb(webui, token, kb_id, file_id, directory_id)
        added += 1
        if directory_id:
            moved += 1

    log(
        f"{kb_name}: rebuilt with {added} files ({reused} reused by content "
        f"hash), {moved} filed into {len(dirs)} numbered directories "
        f"(kb {kb_id})"
    )
    return 0


def _collection(body: Any, key: str) -> list | None:
    """Open WebUI 0.12 paginates the admin list endpoints: ``/users/`` returns
    ``{"users": [...], "total": N}``, ``/knowledge/`` returns ``{"items": [...]}``.
    Older minor versions returned a bare list. Normalise both.
    """
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        rows = body.get(key)
        if isinstance(rows, list):
            return rows
    return None


def _knowledge_by_name(webui: str, token: str, name: str) -> str | None:
    status, resp = _http_json(webui, "/api/v1/knowledge/", "GET", token)
    items = _collection(resp, "items")
    if status != 200 or items is None:
        raise Failure(f"knowledge list failed: HTTP {status}")
    for item in items:
        if isinstance(item, dict) and item.get("name") == name and item.get("id"):
            return item["id"]
    return None


def _delete_knowledge(webui: str, token: str, kb_id: str) -> None:
    """Open WebUI 0.12 deletes a KB at ``{id}/delete``; 0.11 used plain ``{id}``."""
    status, resp = _http_json(webui, f"/api/v1/knowledge/{kb_id}/delete", "DELETE", token)
    if status != 200:
        raise Failure(f"knowledge delete failed: HTTP {status}: {resp}")


def _load_corpus(vault_root: Path) -> Any:
    script = vault_root / "scripts" / "sparknet-rag-corpus.py"
    if not script.is_file():
        raise Failure(f"corpus builder missing: {script}")
    return load_by_path("_sparknet_rag_corpus_ws", script)


# --- default model -------------------------------------------------------------


def cmd_default_model(webui: str, token: str, model: str) -> int:
    status, cur = _http_json(webui, "/api/v1/configs/models", "GET", token)
    if status != 200 or not isinstance(cur, dict):
        raise Failure(f"models config read failed: HTTP {status}: {cur}")
    pinned = cur.get("DEFAULT_PINNED_MODELS") or ""
    order = cur.get("MODEL_ORDER_LIST") or []
    status, resp = _http_json(
        webui,
        "/api/v1/configs/models",
        "POST",
        token,
        {
            "DEFAULT_MODELS": model,
            "DEFAULT_PINNED_MODELS": pinned,
            "MODEL_ORDER_LIST": order,
        },
    )
    if status != 200:
        raise Failure(f"default model update failed: HTTP {status}: {resp}")
    log(f"default chat model set to {model}")
    return 0


# --- status --------------------------------------------------------------------


def cmd_status(webui: str, token: str) -> int:
    status, users_raw = _http_json(webui, "/api/v1/users/", "GET", token)
    users = _collection(users_raw, "users")
    if status != 200 or users is None:
        raise Failure(f"users list failed: HTTP {status}")
    status, kbs_raw = _http_json(webui, "/api/v1/knowledge/", "GET", token)
    kbs = _collection(kbs_raw, "items")
    if status != 200 or kbs is None:
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
    p.add_argument("--admin-email", default=os.environ.get("SPARKNET_WEBUI_ADMIN_EMAIL") or "admin@sparknet.local")
    p.add_argument(
        "--admin-password-file",
        type=lambda s: Path(s).expanduser(),
        default=Path(
            os.environ.get("SPARKNET_WEBUI_ADMIN_PASSWORD_FILE")
            or "~/.local/state/sparknet/cluster2/webui-admin-password"
        ),
    )
    p.add_argument(
        "--bridge-grant-file",
        type=lambda s: Path(s).expanduser(),
        default=Path(
            os.environ.get("SPARKNET_WEBUI_BRIDGE_GRANT_FILE")
            or "~/.local/state/sparknet/cluster2/webui-bridge-grant.token"
        ),
    )
    p.add_argument("--role", choices=("peer", "master"), default=os.environ.get("SPARKNET_ROLE", "peer"))
    p.add_argument(
        "--vault-root",
        type=Path,
        default=Path(os.environ.get("SPARKNET_VAULT_ROOT") or Path(__file__).resolve().parent.parent),
    )
    p.add_argument(
        "--webui-dir",
        type=Path,
        default=Path(os.environ.get("SPARKNET_WEBUI_DIR") or Path(__file__).resolve().parent.parent.parent / "webui"),
    )
    p.add_argument(
        "--settings",
        type=Path,
        default=Path(os.environ.get("SPARKNET_WEBUI_SETTINGS") or ""),
    )
    p.add_argument(
        "--webui-service-token-file",
        type=Path,
        default=Path(os.environ.get("SPARKNET_MEMORY_WEBUI_TOKEN_FILE") or ""),
    )
    p.add_argument(
        "--engine-token-file",
        type=Path,
        default=Path(os.environ.get("SPARKNET_OPENCODE_ENGINE_TOKEN_FILE") or ""),
    )
    p.add_argument("--base-model", default=os.environ.get("SPARKNET_WEBUI_BASE_MODEL", "sparknet-chat"))
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="sign in and summarize the surface")

    kb = sub.add_parser("kb-dirs", help="ensure the numbered knowledge directories in a KB")
    kb.add_argument("kb_id")

    ski = sub.add_parser("skills-import", help="import .opencode/skills into Workspace > Skills")
    ski.add_argument(
        "--skills-dir",
        type=Path,
        default=Path(os.environ.get("SPARKNET_VAULT_ROOT") or Path(__file__).resolve().parent.parent)
        / ".opencode"
        / "skills",
    )

    tools = sub.add_parser("tools-register", help="register the dispatch bridge as the vault tool server")
    tools.add_argument("--url", default=os.environ.get("SPARKNET_BRIDGE_URL") or DEFAULT_BRIDGE_URL)
    tools.add_argument("--spec-path", default=DEFAULT_SPEC_PATH)

    fi = sub.add_parser("functions-import", help="install the memory filter into Workspace > Functions")
    fi.add_argument(
        "--filter-file",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent / "shared" / "sparknet-memory-filter.py",
    )

    rs = sub.add_parser("rebuild-structure", help="rebuild the KB as a mirror of the role vault")
    rs.add_argument(
        "--vault-root",
        type=Path,
        default=Path(os.environ.get("SPARKNET_VAULT_ROOT") or Path(__file__).resolve().parent.parent),
    )
    rs.add_argument("--kb-name", default=DEFAULT_KB_NAME)

    dm = sub.add_parser("default-model", help="set the default chat model")
    dm.add_argument("model")

    pr = sub.add_parser("prompts-import", help="install the bilingual prompt library")
    pr.add_argument("--file", type=Path, default=Path("prompts/sparknet-prompts.json"))

    ti = sub.add_parser("tools-import", help="install the native Sparknet tools")
    ti.add_argument("--dir", type=Path, default=Path("tools"))

    fi = sub.add_parser("functions-import-all", help="install and activate all Sparknet functions")
    fi.add_argument("--dir", type=Path, default=Path("functions"))

    sa = sub.add_parser("settings-apply", help="apply the role settings baseline")
    sa.add_argument("--file", type=Path, default=None)

    mm = sub.add_parser("models-migrate", help="move per-user models to the engine base")
    mm.add_argument("--base-model", default="sparknet-chat")

    sc = sub.add_parser("structure-corpus", help="rebuild the structure KB from the allow-list")
    sc.add_argument("--kb-name", default=DEFAULT_KB_NAME)

    sub.add_parser("reindex", help="reindex all knowledge bases")
    sub.add_parser("apply", help="apply the full workspace baseline")
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
        return cmd_tools_register(args.webui, token, args.bridge_grant_file, args.url, args.spec_path)
    if args.command == "functions-import":
        return cmd_functions_import(args.webui, token, args.filter_file)
    if args.command == "rebuild-structure":
        return cmd_rebuild_structure(args.webui, token, args.vault_root, args.kb_name)
    if args.command == "default-model":
        return cmd_default_model(args.webui, token, args.model)
    if args.command == "prompts-import":
        path = args.file if args.file.is_absolute() else args.webui_dir / args.file
        return cmd_prompts_import(args.webui, token, path)
    if args.command == "tools-import":
        webui_token = _token_from_file(args.webui_service_token_file) if str(args.webui_service_token_file) else ""
        return cmd_tools_import(
            args.webui, token, args.dir if args.dir.is_absolute() else args.webui_dir / args.dir, webui_token
        )
    if args.command == "functions-import-all":
        webui_token = _token_from_file(args.webui_service_token_file) if str(args.webui_service_token_file) else ""
        return cmd_functions_import_all(
            args.webui, token, args.dir if args.dir.is_absolute() else args.webui_dir / args.dir, webui_token
        )
    if args.command == "settings-apply":
        settings = args.file or args.settings
        if not settings or str(settings) in ("", "."):
            settings = args.webui_dir / (f"settings.{args.role}.json")
        engine_token = _token_from_file(args.engine_token_file) if str(args.engine_token_file) else ""
        return cmd_settings_apply(args.webui, token, Path(settings), engine_token)
    if args.command == "models-migrate":
        return cmd_models_migrate(args.webui, token, args.base_model)
    if args.command == "structure-corpus":
        corpus_root = (
            args.vault_root.parent if args.vault_root.name in ("peer-node", "master-node") else args.vault_root
        )
        return cmd_structure_corpus(
            args.webui,
            token,
            corpus_root,
            args.kb_name,
            corpus_root / "scripts" / "sparknet-rag-corpus.py",
        )
    if args.command == "reindex":
        return cmd_reindex(args.webui, token)
    if args.command == "apply":
        webui_token = _token_from_file(args.webui_service_token_file) if str(args.webui_service_token_file) else ""
        engine_token = _token_from_file(args.engine_token_file) if str(args.engine_token_file) else ""
        settings = args.settings
        if not settings or str(settings) in ("", "."):
            settings = args.webui_dir / (f"settings.{args.role}.json")
        return cmd_apply(args, prov, token, webui_token, engine_token)
    raise Failure(f"unknown command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failure as exc:
        print(f"sparknet-webui-workspace: {exc}", file=sys.stderr)
        raise SystemExit(1)
