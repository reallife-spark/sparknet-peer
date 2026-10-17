#!/usr/bin/env python3
"""Sparknet vault provisioner — per-user knowledge vaults and KBs (peer PLAN Step 11).

Design: ../PER_USER_VAULTS_DESIGN.md.

Owns `/srv/sparknet/knowledge/` on a node. On each poll it reads the node's Open
WebUI users with the admin token and provisions a `peer-<user_id>` knowledge
vault for every approved account:

* the account was created as `pending` and the node admin Accept'd it
  (role flip `pending` → `user`), or
* the account already exists with an approved role and is first seen.

For each new account it:
  1. materialises a **knowledge-vault slice** of the peer-node template at
     `/srv/sparknet/knowledge/peer-<user_id>/` (numbered categories, shims,
     AGENTS.md, docs, schema — never `.state/`, `blockchain/`, `services/`,
     `scripts/`, `container/`, `configs/`, `tests/`, `manifests/`, `.git/`,
     and never the template's own `.env`),
  2. regenerates the copy's `.env` with `VAULT_PATH` pointed at the copy,
  3. creates the user's KB in Open WebUI (admin token; access grant `read` to
     that user),
  4. embeds the vault markup into the KB (upload files → `knowledge/<id>/file/add`),
     creating a knowledge **directory** per numbered category (`00_System` …
     `07_Archives`) and filing each page into its own, so the Knowledge tab shows
     the same structure the vault has,
  5. defaults the user's own chat model to the KB (`meta.knowledge`), and
  6. records the provisioning state under `/srv/sparknet/knowledge/.state/`.

A content-digest pass adds pages the user later writes into their vault, so the
KB follows what they add ("customized to the knowledge they add"). Pages are
matched by basename and already-present ones are reused, so re-indexing never
piles up duplicate rows; the accepted trade-off is that editing an existing
page in place does not re-embed it.

A pass is **resumable**. If it is interrupted after the vault is materialised
but before the record reaches `done`, the next poll reuses the existing vault
and its recorded KB id and skips pages already in the KB, instead of refusing
on "destination exists" and skipping that user forever. A user deleted from the
WebUI has its record marked `orphaned` (folder, KB and model left in place)
rather than retried each poll.

The `master-<id>` copy rule exists but is **inactive**: it provisions a
`master-<id>` vault only when the node is elected Master AND its WebUI admin is
signed in — never in the cluster2 pair, where every user (including admins, and
users on the Master node) receives a `peer-` copy.

WebUI 0.12.0 API surface in use (verified live on .131):
  POST /api/v1/auths/signin        {email, password} -> {token}
  GET  /api/v1/users/              admin -> users (id, name, email, role)
  POST /api/v1/knowledge/create    {name, description, access_grants}
  POST /api/v1/files/              multipart upload -> {id}
  POST /api/v1/knowledge/{id}/file/add   {file_id}
  POST /api/v1/models/create       ModelForm with meta.knowledge + access_grants

Grants are flat dicts: {principal_type, principal_id, permission}.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # Python 3.10+
    import tomllib
except ImportError:  # pragma: no cover - Python 3.8/3.9
    tomllib = None

SLICE_EXCLUDED = frozenset(
    {
        ".git",
        ".gitignore",
        ".env",
        ".pytest_cache",
        ".opencode",
        ".state",
        "blockchain",
        "container",
        "configs",
        "deployment",
        "manifests",
        "scripts",
        "services",
        "tests",
        "Makefile",
        "requirements-dev.txt",
        "requirements.txt",
    }
)

SLICE_INCLUDED_TOP = frozenset(
    {
        ".env.example",
        "00_System",
        "01_Inbox",
        "02_Daily",
        "03_Sources",
        "04_Evergreen",
        "05_Knowledge",
        "06_Projects",
        "07_Archives",
        "_staging",
        "AGENTS.md",
        "MISSION.md",
        "docs",
        "audit",
        "_archives",
        "_raw",
        "concepts",
        "entities",
        "skills",
        "synthesis",
        "journal",
        "projects",
        "references",
    }
)

MASTER_COPY_REQUIRED = ("elected_master", "admin_signed_in")

#: The numbered PARA categories. Each one that has pages becomes a knowledge
#: *directory* inside the user's KB, so the Open WebUI Knowledge tab shows
#: `00_System` … `07_Archives` instead of one flat file list.
NUMBERED_TOP = (
    "00_System",
    "01_Inbox",
    "02_Daily",
    "03_Sources",
    "04_Evergreen",
    "05_Knowledge",
    "06_Projects",
    "07_Archives",
)


class Refusal(RuntimeError):
    """A provisioning step refused to run. Message is the reason."""


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def _http_json(url: str, method: str, token: str | None, body: Any = None) -> tuple[int, Any]:
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw.decode("utf-8", "replace")
        return e.code, detail


def _upload_and_embed(url: str, token: str, path: Path) -> str:
    """Upload a file and embed it into its per-file collection, deterministically.

    Uploading with ``process=true`` embeds in a background task and returns
    before the per-file chunks exist; a `file/add` fired immediately then fails
    with EMPTY_CONTENT (a race, so it fails sporadically). Upload without
    processing, run the synchronous ``/retrieval/process/file``, then return
    the file id for the caller to attach to a KB.
    """
    boundary = "sparknet-" + hashlib.sha256(os.urandom(16)).hexdigest()
    name = path.name
    content = path.read_bytes()
    parts = []
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: text/markdown\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(b"\r\n")
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="process"\r\n\r\nfalse\r\n'.encode())
    parts.append(f"--{boundary}--\r\n".encode())
    data = b"".join(parts)

    req = urllib.request.Request(f"{url}/api/v1/files/", method="POST", data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    file_id = result["id"]

    status, body = _http_json(f"{url}/api/v1/retrieval/process/file", "POST", token, {"file_id": file_id})
    if status != 200:
        raise Refusal(f"file process failed ({name}): HTTP {status}: {body}")
    return file_id


def _state_dir(knowledge_root: Path) -> Path:
    return knowledge_root / ".state"


def _load_state(knowledge_root: Path) -> dict[str, Any]:
    d = _state_dir(knowledge_root)
    f = d / "provisioned.json"
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def _save_state(knowledge_root: Path, state: dict[str, Any]) -> None:
    d = _state_dir(knowledge_root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "provisioned.json").write_text(json.dumps(state, indent=2, sort_keys=True))
    os.chmod(d / "provisioned.json", 0o600)


def materialise_vault_slice(template: Path, dest: Path) -> None:
    """Copy the knowledge-vault slice of `template` into `dest` (atoms only).

    Everything enumerated in SLICE_EXCLUDED is skipped before any copy starts
    (fail-closed: an unexpected walkable item is omitted only if excluded, so a
    template that grows a new directory keeps it out unless someone adds it to
    the allow-list). Symlink shims are copied as symlinks so the copy keeps the
    numbered/legacy dual spelling.
    """
    if dest.exists():
        raise Refusal(f"destination already exists: {dest}")
    src = template.absolute()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir(mode=0o750)

    for child in sorted(src.iterdir()):
        if child.name in SLICE_EXCLUDED:
            continue
        if child.name not in SLICE_INCLUDED_TOP:
            continue
        target = dest / child.name
        if child.is_symlink():
            target.symlink_to(os.readlink(child))
        elif child.is_dir():
            _copytree(child, target, exclude=SLICE_EXCLUDED)
        else:
            import shutil

            shutil.copy2(child, target)


def _copytree(src: Path, dest: Path, exclude: frozenset) -> None:
    import shutil

    dest.mkdir(mode=0o750)
    for child in sorted(src.iterdir()):
        if child.name in exclude:
            continue
        target = dest / child.name
        if child.is_symlink():
            target.symlink_to(os.readlink(child))
        elif child.is_dir():
            _copytree(child, target, exclude=exclude)
        else:
            shutil.copy2(child, target)


def write_vault_env(vault: Path) -> Path:
    """Write `<vault>/.env` with VAULT_PATH pointing at `vault` itself.

    Mirrors the shape scripts/sparknet-env-init.sh writes for a role vault, but
    with the copy as both vault and fabric root so the wiki skills resolve the
    copy and nothing else. The copy has no contracts/ so env-init's
    fabric-root detection would refuse it; the provisioner writes the file it
    means directly.
    """
    src = vault / ".env.example"
    if src.exists():
        lines = []
        for line in src.read_text().splitlines():
            if line.startswith("VAULT_PATH="):
                line = f'VAULT_PATH="{vault}"'
            elif line.startswith("VAULT_SOURCES_DIR="):
                line = f'VAULT_SOURCES_DIR="{vault}/_raw"'
            elif line.startswith("VAULT_CATEGORIES="):
                line = 'VAULT_CATEGORIES="concepts,entities,skills,references,synthesis,journal"'
            lines.append(line)
        env_text = "\n".join(lines) + "\n"
    else:
        env_text = (
            f'VAULT_PATH="{vault}"\n'
            f'VAULT_SOURCES_DIR="{vault}/_raw"\n'
            'VAULT_CATEGORIES="concepts,entities,skills,references,synthesis,journal"\n'
            "WIKI_STAGED_WRITES=false\n"
        )
    env_file = vault / ".env"
    env_file.write_text(env_text)
    os.chmod(env_file, 0o600)
    if "VAULT_PATH=" not in env_text:
        raise Refusal(f"generated {env_file} without a VAULT_PATH line")
    return env_file


def _vault_pages(vault: Path) -> list[Path]:
    """Real markdown pages in the copy, each reached exactly once.

    Walks without following symlinks: the vault's legacy shims are symlinks to
    their numbered home (`index.md` -> `00_System/index.md`,
    `concepts` -> `05_Knowledge/concepts`), and following them would embed the
    same page twice. Open WebUI flattens an ingested file to its basename, so a
    followed shim surfaces as a duplicate row in the KB. `.state/` is skipped.
    """
    pages: list[Path] = []
    for root, dirs, files in os.walk(vault, followlinks=False):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if d != ".state" and not (root_path / d).is_symlink()]
        for name in files:
            page = root_path / name
            if page.suffix.lower() != ".md" or page.is_symlink():
                continue
            pages.append(page)
    return sorted(pages)


def _top_dir(rel: Path) -> str | None:
    """The numbered category a vault-relative page belongs to, else None."""
    if rel.parts and rel.parts[0] in NUMBERED_TOP:
        return rel.parts[0]
    return None


def _content_digest(vault: Path) -> str:
    """Digest over the vault's markdown files, for the re-index trigger."""
    h = hashlib.sha256()
    for p in _vault_pages(vault):
        rel = str(p.relative_to(vault))
        h.update(rel.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _grant(principal_id: str, permission: str = "read") -> dict:
    return {
        "principal_type": "user",
        "principal_id": principal_id,
        "permission": permission,
    }


def webui_token(base: str, email: str, password: str) -> str:
    status, body = _http_json(f"{base}/api/v1/auths/signin", "POST", None, {"email": email, "password": password})
    if status != 200 or not isinstance(body, dict) or "token" not in body:
        raise Refusal(f"signin failed: HTTP {status}: {body}")
    return body["token"]


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


def fetch_users(base: str, token: str) -> list[dict]:
    status, body = _http_json(f"{base}/api/v1/users/", "GET", token)
    users = _collection(body, "users")
    if status != 200 or users is None:
        raise Refusal(f"users list failed: HTTP {status}")
    return users


def create_kb(base: str, token: str, name: str, user_id: str) -> str:
    body = {
        "name": name,
        "description": f"Knowledge base owned by user {user_id}",
        "access_grants": [_grant(user_id, "read")],
    }
    status, resp = _http_json(f"{base}/api/v1/knowledge/create", "POST", token, body)
    if status != 200 or not isinstance(resp, dict) or "id" not in resp:
        raise Refusal(f"knowledge create failed: HTTP {status}: {resp}")
    return resp["id"]


def embed_vault_into_kb(
    base: str,
    token: str,
    kb_id: str,
    vault: Path,
    progress: Any = None,
    existing: dict[str, str] | None = None,
) -> list[tuple[Path, str]]:
    """Attach each vault page to the KB, uploading only pages not already there.

    Returns ``(vault-relative path, file id)`` pairs for **every** vault page —
    newly uploaded and already-present alike — so the caller can file each page
    into its numbered knowledge directory. ``existing`` maps a page basename to
    its file id in the KB (WebUI flattens files to basename); a page already
    present is reused rather than uploaded a second time, which is what makes a
    resumed or re-indexed pass idempotent instead of piling up duplicate rows.
    """
    present = existing or {}
    added: list[tuple[Path, str]] = []
    for page in _vault_pages(vault):
        rel = page.relative_to(vault)
        file_id = present.get(page.name)
        if file_id:
            added.append((rel, file_id))
            if progress:
                progress(f"present {rel}")
            continue
        try:
            file_id = _upload_and_embed(f"{base}", token, page)
        except Exception as e:
            if progress:
                progress(f"upload failed {rel}: {e}")
            continue
        status, resp = _http_json(f"{base}/api/v1/knowledge/{kb_id}/file/add", "POST", token, {"file_id": file_id})
        if status != 200:
            if progress:
                progress(f"kb add failed {rel}: HTTP {status}")
            continue
        added.append((rel, file_id))
        if progress:
            progress(f"embedded {rel}")
    return added


def list_knowledge_directories(base: str, token: str, kb_id: str) -> dict[str, str]:
    """The KB's existing directories as a ``name -> id`` map."""
    status, body = _http_json(f"{base}/api/v1/knowledge/{kb_id}/files", "GET", token)
    if status != 200 or not isinstance(body, dict):
        raise Refusal(f"knowledge dir list failed: HTTP {status}")
    dirs = body.get("directories") or []
    return {d["name"]: d["id"] for d in dirs if isinstance(d, dict) and "name" in d}


def existing_kb_files(base: str, token: str, kb_id: str) -> dict[str, str]:
    """The KB's attached files as a ``basename -> file id`` map.

    Open WebUI flattens a file to its basename, so matching on the basename is
    how the embed pass tells "already here" from "new". Best-effort: an
    unexpected response shape yields an empty map so provisioning still runs
    (it would then simply re-add, no worse than before this helper existed).
    """
    status, body = _http_json(f"{base}/api/v1/knowledge/{kb_id}/files", "GET", token)
    if status != 200 or not isinstance(body, dict):
        return {}
    present: dict[str, str] = {}
    for f in body.get("files") or []:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        file_id = f.get("id") or f.get("file_id")
        if name and file_id:
            present[name] = file_id
    return present


def kb_exists(base: str, token: str, kb_id: str) -> bool:
    """Whether a knowledge base id still exists in the WebUI.

    A recorded ``kb_id`` can outlive the KB itself (the KB was deleted, or the
    WebUI database was reset). Reusing a dead id makes every ``file/add`` fail
    with HTTP 400, so a resumed pass checks first and mints a fresh KB instead.
    """
    status, _ = _http_json(f"{base}/api/v1/knowledge/{kb_id}/files", "GET", token)
    return status == 200


def create_knowledge_directory(base: str, token: str, kb_id: str, name: str) -> str:
    status, resp = _http_json(
        f"{base}/api/v1/knowledge/{kb_id}/dirs/create", "POST", token, {"name": name, "parent_id": None}
    )
    if status != 200 or not isinstance(resp, dict) or "id" not in resp:
        raise Refusal(f"knowledge dir create failed ({name}): HTTP {status}: {resp}")
    return resp["id"]


def ensure_directories(base: str, token: str, kb_id: str, names: tuple[str, ...] | list[str]) -> dict[str, str]:
    """Create any missing numbered directories; return ``name -> id``."""
    existing = list_knowledge_directories(base, token, kb_id)
    for name in names:
        if name not in existing:
            existing[name] = create_knowledge_directory(base, token, kb_id, name)
    return existing


def move_file_to_directory(base: str, token: str, kb_id: str, file_id: str, dir_id: str) -> bool:
    status, _ = _http_json(
        f"{base}/api/v1/knowledge/{kb_id}/file/move", "POST", token, {"file_id": file_id, "directory_id": dir_id}
    )
    return status == 200


def organize_into_directories(
    base: str, token: str, kb_id: str, added: list[tuple[Path, str]], progress: Any = None
) -> int:
    """Create the numbered directories and file each embedded page into its own."""
    dirs = ensure_directories(base, token, kb_id, NUMBERED_TOP)
    moved = 0
    for rel, file_id in added:
        top = _top_dir(rel)
        if not top:
            continue
        dir_id = dirs.get(top)
        if not dir_id:
            continue
        if move_file_to_directory(base, token, kb_id, file_id, dir_id):
            moved += 1
        elif progress:
            progress(f"move failed {rel} -> {top}")
    return moved


def create_user_model(
    base: str, token: str, user_id: str, kb_id: str, base_model: str = "plan", display_name: str | None = None
) -> str:
    """Create the user's own model bound to `base_model` with their KB attached."""
    model_id = f"peer-{user_id}"
    body = {
        "id": model_id,
        "base_model_id": base_model,
        "name": display_name or f"peer-{user_id}",
        "meta": {"knowledge": [kb_id]},
        "params": {},
        "access_grants": [_grant(user_id, "read")],
        "is_active": True,
    }
    status, resp = _http_json(f"{base}/api/v1/models/create", "POST", token, body)
    if status != 200:
        raise Refusal(f"model create failed: HTTP {status}: {resp}")
    return model_id


def provision_user(
    base: str,
    token: str,
    template: Path,
    knowledge_root: Path,
    user: dict,
    base_model: str = "plan",
    progress: Any = None,
) -> dict:
    """Provision a peer- vault + KB + default model for one approved user.

    Resumable and idempotent. A pass that failed after the vault was
    materialised but before the record reached ``done`` (the folder exists,
    ``stage`` is ``embed``) continues instead of restarting: the existing vault
    is reused rather than refused, the recorded ``kb_id`` is reused instead of
    creating a second KB (unless the recorded KB has since been deleted, in
    which case a fresh one is minted), and pages already in the KB are not
    re-uploaded. This is what stops a user — an admin in practice — whose first
    pass was interrupted from being skipped in a loop forever.
    """
    user_id = user.get("id")
    if not user_id:
        raise Refusal("user record has no id")
    name = user.get("name") or user.get("email") or user_id
    slug = _slug(user_id)
    vault = knowledge_root / f"peer-{slug}"

    state = _load_state(knowledge_root)
    rec = state.get(user_id)
    if rec and rec.get("stage") == "done":
        return rec

    staged: dict[str, Any] = {
        "user_id": user_id,
        "name": name,
        "role": user.get("role"),
        "stage": "vault",
        "vault_path": str(vault),
        "updated_at": int(time.time()),
    }
    if rec and rec.get("kb_id"):
        staged["kb_id"] = rec["kb_id"]

    try:
        if vault.exists():
            if progress:
                progress(f"reusing existing vault {vault}")
        else:
            if progress:
                progress(f"materialising {vault}")
            materialise_vault_slice(template, vault)
        env_file = write_vault_env(vault)
        if progress:
            progress(f"wrote {env_file}")

        kb_id = staged.get("kb_id")
        if kb_id and not kb_exists(base, token, kb_id):
            if progress:
                progress(f"recorded KB {kb_id} no longer exists; creating a new one")
            kb_id = None
            staged.pop("kb_id", None)
        if not kb_id:
            if progress:
                progress("creating KB")
            kb_id = create_kb(base, token, f"peer-{slug}", user_id)
            staged["kb_id"] = kb_id
        staged["stage"] = "embed"

        if progress:
            progress("embedding vault pages")
        added = embed_vault_into_kb(
            base, token, kb_id, vault, progress=progress, existing=existing_kb_files(base, token, kb_id)
        )
        staged["files"] = len(added)
        staged["digest"] = _content_digest(vault)

        if progress:
            progress("filing pages into numbered directories")
        staged["dirs_moved"] = organize_into_directories(base, token, kb_id, added, progress=progress)

        if progress:
            progress("creating default model")
        model_id = create_user_model(base, token, user_id, kb_id, base_model=base_model, display_name=f"peer-{name}")
        staged["model_id"] = model_id
        staged["stage"] = "done"
    finally:
        state = _load_state(knowledge_root)
        state[user_id] = staged
        _save_state(knowledge_root, state)

    return staged


def _slug(user_id: str) -> str:
    safe = "".join(c for c in user_id if c.isalnum() or c in "-_")
    return safe[:64] or "user"


def reindex_user(base: str, token: str, vault: Path, kb_id: str, progress: Any = None) -> bool:
    """Re-embed a provisioned user's vault, adding only pages not already in the KB.

    Pages already present (matched by basename) are reused, not re-uploaded, so
    re-indexing never piles up duplicate rows. The known limitation, accepted
    for now: a page edited in place keeps its basename, so it is **not**
    re-embedded — only genuinely new pages are added. The digest gate in
    ``run_watch`` still decides when this pass runs at all.
    """
    digest = _content_digest(vault)
    if progress:
        progress(f"re-index scan digest={digest[:12]}")
    added = embed_vault_into_kb(
        base, token, kb_id, vault, progress=progress, existing=existing_kb_files(base, token, kb_id)
    )
    organize_into_directories(base, token, kb_id, added, progress=progress)
    return bool(added)


def _prune_vanished_users(knowledge_root: Path, users: list[dict]) -> list[str]:
    """Mark records for users the WebUI no longer has as ``orphaned``.

    Non-destructive: the vault copy, KB and model are left where they are, and
    the record stays visible in the state file (with ``orphaned_at``) so an
    operator can see what was left behind. Refuses to act on an empty user list
    — a transient empty read must never mass-orphan every user. Returns the
    ids marked orphaned this pass.
    """
    live = {u.get("id") for u in users if u.get("id")}
    if not live:
        return []
    state = _load_state(knowledge_root)
    orphaned: list[str] = []
    for uid, rec in state.items():
        if not rec or rec.get("stage") == "orphaned" or uid in live:
            continue
        rec["stage"] = "orphaned"
        rec["orphaned_at"] = int(time.time())
        orphaned.append(uid)
        log(f"user {uid} no longer exists; record marked orphaned (vault/KB/model left in place)")
    if orphaned:
        _save_state(knowledge_root, state)
    return orphaned


def run_watch(
    base: str,
    email: str,
    password: str,
    template: Path,
    knowledge_root: Path,
    base_model: str = "plan",
    interval: int = 90,
    provision_existing: bool = True,
    master_copy=False,
    once=False,
) -> None:
    state = _load_state(knowledge_root)
    while True:
        try:
            token = webui_token(base, email, password)
            users = fetch_users(base, token)
            _prune_vanished_users(knowledge_root, users)
            state = _load_state(knowledge_root)
            for user in users:
                role = user.get("role")
                if role not in ("user", "admin"):
                    continue
                rec = state.get(user.get("id"))
                if rec and rec.get("stage") == "done":
                    continue
                if not provision_existing and not rec:
                    continue
                try:
                    provision_user(base, token, template, knowledge_root, user, base_model=base_model, progress=log)
                    state = _load_state(knowledge_root)
                except Refusal as e:
                    log(f"provision skipped {user.get('name')}: {e}")
                    continue
            # Re-index pass over already-provisioned vaults whose digest moved.
            for uid, rec in state.items():
                if not rec or rec.get("stage") != "done":
                    continue
                vault = Path(rec.get("vault_path", ""))
                kb_id = rec.get("kb_id")
                if not vault.exists() or not kb_id:
                    continue
                if _content_digest(vault) != rec.get("digest"):
                    log(f"content changed for {uid}; re-embedding")
                    token = webui_token(base, email, password)
                    reindex_user(base, token, vault, kb_id, progress=log)
                    state = _load_state(knowledge_root)
                    state[uid]["digest"] = _content_digest(vault)
                    _save_state(knowledge_root, state)
        except Refusal as e:
            log(f"watch error: {e}")
        except Exception as e:
            log(f"watch error (unexpected): {e!r}")
        if once:
            return
        time.sleep(interval)


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Sparknet vault provisioner (peer PLAN Step 11): per-user peer-<user_id> knowledge vaults + WebUI KBs."
        )
    )
    p.add_argument(
        "--template",
        type=lambda s: Path(s).expanduser(),
        default=Path(__file__).resolve().parent.parent,
        help="peer-node template directory (default: repo peer-node)",
    )
    p.add_argument(
        "--knowledge-root",
        type=lambda s: Path(s).expanduser(),
        default=Path(os.environ.get("SPARKNET_VAULT_KNOWLEDGE_ROOT") or "/srv/sparknet/knowledge"),
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
    p.add_argument("--base-model", default="plan")
    p.add_argument("--interval", type=int, default=90)
    p.add_argument("--no-provision-existing", action="store_true")
    p.add_argument("--once", action="store_true", help="one poll, then exit")
    p.add_argument(
        "--master-copy", action="store_true", help="allow master-<id> copies (elected-Master + admin signed in)"
    )
    args = p.parse_args()

    password_file = args.admin_password_file.expanduser()
    if not password_file.exists():
        print(f"sparknet-vault-provisioner: missing admin password file: {password_file}", file=sys.stderr)
        sys.exit(1)
    password = password_file.read_text().strip()

    template = args.template.absolute()
    if not (template / "AGENTS.md").exists():
        print(f"sparknet-vault-provisioner: template missing AGENTS.md: {template}", file=sys.stderr)
        sys.exit(1)

    if args.master_copy:
        print(
            "sparknet-vault-provisioner: --master-copy is inactive in this pair; "
            "every user (incl. admins) receives a peer- copy.",
            file=sys.stderr,
        )

    knowledge_root = args.knowledge_root.absolute()
    (knowledge_root / ".state").mkdir(parents=True, exist_ok=True)
    log(f"vault provisioner: webui={args.webui} template={template} root={knowledge_root}")

    run_watch(
        args.webui,
        args.admin_email,
        password,
        template,
        knowledge_root,
        base_model=args.base_model,
        interval=args.interval,
        provision_existing=not args.no_provision_existing,
        master_copy=args.master_copy,
        once=args.once,
    )
    log("vault provisioner completed")


if __name__ == "__main__":
    main()
