#!/usr/bin/env python3
"""Sparknet dispatch bridge on loopback 4096: real server, persistent
grant-keyed sessions, grant-checked vault tools (peer PLAN Step 4).

One listener, two personalities, one port — exactly the port the fabric grants
the dispatch bridge:

* the **signed** personality keeps the A2A/status surface: requests carry the
  X-Sparknet-* envelope and are verified against the node key, whose possession
  is the identity. `/v1/status` answers here, unchanged.
* the **plain** personality answers the Open WebUI surface — OpenAI-compatible
  `/v1/models` and `/v1/chat/completions`, `/session/<grant_id>`, and
  `/vault/*` tools (list, cat, capture, promote, patch) as described by
  `manifests/vault-openapi.json`. Every plain route is loopback-only and
  requires `Authorization: Bearer <base64url of a signed grant>`; the grant is
  verified in guard-protocol order before anything is read or written.

Sessions are **persistent**, keyed by `grant_id`, stored under the state dir
(`$SPARKNET_STATE_DIR/sessions/` or `$VAULT_ROOT/.state/sessions/`), so a
session survives a bridge restart. No conversation identity derives from
content hashing.

Mutations never invent a ledger block here: capture/promote/patch call the one
shared implementation (shared/sparknet-provenance.py), which verifies the grant
again, fail-closed, and appends the same dual-signed `grant_use` envelope an
approval carries. This file names no ledger path — it cannot write what it
cannot name.

Agent *execution* (spawning a live plan/build/vault turn) is a later boundary;
the bridge records and routes, it never impersonates an assistant. Step 6's
native conversation verbs ride the same `/session/:id` surface and need no
custom passing mechanism: `POST /session/:id/message` accepts an `agent` to
hand the conversation off with history intact, `POST /session/:id/fork` branches
at a message while the original stays whole, `GET /session/:id/children` lists
the branch, and the `share` verbs declare a session-view/session-handoff share
(the clocked, cross-node sharing regime is Step 8).
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import sys
import urllib.parse

try:  # Python 3.9+
    from collections.abc import Callable
except ImportError:  # pragma: no cover - Python 3.8
    from typing import Callable  # noqa: UP035

from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

STATES = ["PLAN", "PERMISSION_PENDING", "PERMITTED", "EXECUTING", "COMPLETE", "DENIED", "ERROR"]
DEFAULT_PORT = 4096
DEFAULT_BIND = "127.0.0.1"
PLAIN_PREFIXES = ("/v1/models", "/v1/chat/completions", "/v1/openapi.json", "/session/", "/vault/")
SESSION_SCHEMA = "sparknet-session/2"
SHARE_KINDS = ("kb-view", "kb-share", "session-view", "session-handoff", "full")

# Approvals are recorded through shared/sparknet-provenance.py, never here.
# This bridge used to carry its own `--test-approval` block builder with a
# different block shape (index/payload/block_hash, a stub signature), which is
# exactly how the two ledger shapes diverged in the first place. A second
# builder is the failure mode; the flag is gone rather than corrected.
APPROVALS_NOTE = (
    "approvals are recorded through shared/sparknet-provenance.py (make provenance-append), never through this bridge"
)


def log(level, msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [{level}] {msg}")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _shared_dir() -> Path:
    """Resolve shared/ from the environment or from the repo layout.

    A standalone bundle ships shared/ next to the role; a full clone has it at
    the fabric root. Never import by name — there is exactly one copy, loaded by
    path for the same reason authz is (see shared/README.md).
    """
    env_shared = os.environ.get("SPARKNET_SHARED_DIR", "").strip()
    if env_shared:
        return Path(env_shared)
    here = Path(__file__).resolve().parent
    candidates = [here.parent / "shared", here.parent.parent / "shared"]
    for cand in candidates:
        if (cand / "sparknet-http.py").is_file():
            return cand
    raise FileNotFoundError(
        "cannot locate shared/sparknet-http.py; this bridge refuses to reimplement the signed transport"
    )


def _load_shared(name: str, label: str, required: tuple[str, ...]) -> Any:
    """Load one shared/ implementation by path, never by import."""
    shared = _shared_dir()
    for dep in required:
        if not (shared / dep).is_file():
            raise FileNotFoundError(f"cannot locate shared/{dep}; this bridge refuses to reimplement it")
    if label in sys.modules:
        return sys.modules[label]
    spec = importlib.util.spec_from_file_location(label, shared / f"{name}.py")
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"no importable module at {shared / f'{name}.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_http() -> Any:
    return _load_shared("sparknet-http", "sparknet_http", ("sparknet-http.py",))


def _load_regime() -> Any:
    """The one share-regime engine, loaded by path from this services dir.

    Step 8: the clocked cross-node share conversation lives in
    ``sparknet-share-regime.py`` (contracts/capabilities + overlay + ports) and
    is the *only* code that knows a share's rules. The bridge delegates its
    share verbs to a ``Regime`` instead of growing its own — a second copy can
    drift and then silently disagree about a deadline.
    """
    path = Path(__file__).resolve().parent / "sparknet-share-regime.py"
    if not path.is_file():
        raise FileNotFoundError(
            f"share regime missing at {path}; the bridge refuses to reimplement the share lifecycle"
        )
    if "sparknet_share_regime_bridge" in sys.modules:
        return sys.modules["sparknet_share_regime_bridge"]
    spec = importlib.util.spec_from_file_location("sparknet_share_regime_bridge", path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"no importable module at {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_provenance() -> Any:
    return _load_shared(
        "sparknet-provenance",
        "sparknet_provenance_bridge",
        ("sparknet-authz.py", "sparknet-provenance.py"),
    )


def _state_dir(vault_root: Path) -> Path:
    """Runtime state: keys and sessions. Never the vault, never the repo."""
    env = os.environ.get("SPARKNET_STATE_DIR", "").strip()
    return Path(env) if env else vault_root / ".state"


def _load_keypair(vault_root: Path, authz: Any) -> Any:
    """The node's own signing key, proving it possesses its identity.

    The bridge is local-only and injects the user environment, so the client it
    trusts is the node itself. The key lives in the state dir (mode 0600) and is
    never shipped; when the node has not run `make user-init`, there is nothing
    to bind with and serve refuses rather than serving unsigned.
    """
    base = _state_dir(vault_root)
    candidates = [
        base / "keys" / "ed25519-node.key.b64",
        base / "keys" / "user.ed25519",
        vault_root / ".state" / "keys" / "ed25519-node.key.b64",
        vault_root / ".state" / "keys" / "user.ed25519",
    ]
    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        return None
    return authz.load_private_key(path)


def _agent_inventory(vault_root: Path) -> list[str]:
    """The node's agents, discovered on disk: opencode agents and agent pods.

    A model id must name an agent this node actually ships; discovering the
    inventory from files is what keeps a UI from selecting a model that routes
    to nothing. The Step 5 *declared* surface (plan/build/vault/node-orchestrator,
    ``manifests/agent-models.json``) is layered on top by ``_available_models``:
    those are roles an opencode node births (plan/build are built-ins, vault and
    the node orchestrator are the node's own agent papers), declared as config
    rather than fabricated by the bridge.
    """
    names: set[str] = set()
    agents_md = vault_root / ".opencode" / "agents"
    if agents_md.is_dir():
        names.update(p.stem for p in agents_md.glob("*.md"))
    pods = vault_root / "agents"
    if pods.is_dir():
        for child in sorted(pods.iterdir()):
            if child.is_dir() and ((child / "agent.py").is_file() or (child / "docker-compose.yml").is_file()):
                names.add(child.name)
    return sorted(names)


def _declared_models(vault_root: Path) -> list[dict[str, Any]]:
    """The Step 5 selectable surface from ``manifests/agent-models.json``.

    Committed, versioned config (DoD 5): Open WebUI's picker may be a database
    click, but the source of truth is this file, because it survives a redeploy.
    An absent or malformed file selects nothing — there is no fallback model list.
    """
    path = vault_root / "manifests" / "agent-models.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
        entries = data.get("selectable") or []
    except (json.JSONDecodeError, OSError):
        return []
    models: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") and entry.get("agent"):
            models.append(
                {
                    "id": str(entry["id"]),
                    "agent": str(entry["agent"]),
                    "label": str(entry.get("label") or entry["id"]),
                    "selectable": True,
                }
            )
    return models


def _available_models(vault_root: Path) -> list[dict[str, Any]]:
    """The complete model surface: declared roles first, discovered agents after."""
    models = _declared_models(vault_root)
    known = {m["id"] for m in models}
    for name in _agent_inventory(vault_root):
        if name not in known:
            models.append({"id": name, "agent": name, "label": name, "selectable": False})
    return models


def _rel_of(raw: str) -> PurePosixPath:
    """Canonicalise a caller-supplied vault path so nothing escapes the vault.

    Mirrors shared/sparknet-provenance.py `_safe_rel`: the write path refuses
    the same inputs again, but reads never reach provenance, so the bridge does
    the canonicalisation for them here rather than invent a second policy.
    """
    if raw is None or raw.strip() in ("", ".") or "/" == raw:
        raise _HttpRequestError(400, "bad-path", "a vault-relative path is required")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise _HttpRequestError(400, "bad-path", f"path escapes the vault: {raw!r}")
    return path


class _HttpRequestError(Exception):
    """A refusal that maps straight to an HTTP status and the stable reason."""

    def __init__(self, status: int, reason: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.reason = reason
        self.detail = detail


class Bridge:
    """The plain Open WebUI surface: models, persistent sessions, vault tools.

    Grant verification happens here for every plain route (guard-protocol order
    via shared/sparknet-authz.py); mutations additionally re-verify inside
    shared/sparknet-provenance.py's grant-scoped writers, so the bridge never
    becomes a second authority. Reads never reach provenance, so they are
    scope-checked here with the same two shared checkers (`_safe_rel`-equivalent
    canonicalisation and `grant_scope_allows`).
    """

    def __init__(
        self,
        vault_root: Path,
        *,
        registry: str | Path | None = None,
        node_identity: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.vault_root = Path(vault_root)
        self.registry_path = Path(registry or (self.vault_root / "manifests" / "node-authorizations.json"))
        self.identity_path = Path(node_identity or (self.vault_root / "manifests" / "node-identity.json"))
        self.clock = clock
        self.http = _load_http()
        self.authz = self.http._load_authz()
        self.prov = _load_provenance()
        self._registry: dict[str, Any] | None = None
        self._identity: dict[str, Any] | None = None
        self._regime: Any | None = None
        self._regime_mod: Any | None = None

    def _share_regime(self) -> Any:
        """The one share-regime engine, built once against this node's state.

        The mesh size is taken from the registry this node holds (DoD note:
        mode is recomputed from the registry node count on every share request,
        never cached). With the empty registry this host actually carries, the
        mode is ``solo``: offers may be *drafted*, but acceptance is refused and
        nothing on this host can open a cross-node share by itself.
        """
        if self._regime is None:
            mod = _load_regime()
            self._regime_mod = mod
            count = 1 + len(list(self.registry().get("authorizations") or []))
            node_id = str(self.node_identity().get("node_id") or "peer-node-bridge")
            self._regime = mod.Regime(node_id, now=self._now, node_count=lambda: count)
        return self._regime

    def _share_verbs(self, session: dict[str, Any]) -> tuple[Any, list[dict]]:
        """The regime plus the session's share records, hydrated from disk so a
        bridge restart doesn't lose a share's deadlines (restore() is a no-op
        for records the regime already holds)."""
        regime = self._share_regime()
        records = list(session.get("shares") or [])
        for record in records:
            if record.get("share_id") not in regime.shares:
                regime.restore(record)
        return regime, records

    def _now(self) -> datetime:
        return (self.clock or _utcnow)()

    def registry(self) -> dict[str, Any]:
        if self._registry is None:
            self._registry = json.loads(self.registry_path.read_text())
        return self._registry

    def node_identity(self) -> dict[str, Any]:
        if self._identity is None:
            self._identity = json.loads(self.identity_path.read_text())
        return self._identity

    def sessions_dir(self) -> Path:
        d = _state_dir(self.vault_root) / "sessions"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    # -- grants -----------------------------------------------------------------

    def decode_grant(self, token: str) -> dict[str, Any]:
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            grant = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _HttpRequestError(401, "missing-grant", f"Bearer must be base64url grant JSON: {exc}")
        if not isinstance(grant, dict) or not grant.get("grant_id"):
            raise _HttpRequestError(401, "missing-grant", "Bearer grant has no grant_id")
        return grant

    def verify_grant(self, grant: dict[str, Any], *, allow_spent_nonce: bool = False) -> None:
        """Guard-protocol order, exactly as shared/sparknet-authz.py's verify_grant.

        ``allow_spent_nonce`` is set only for a grant whose session has turned on
        the ``vault_access: all`` toggle: its own nonce is dropped from the
        consumed set so a session the operator made reusable keeps working after
        its first mutation. No other grant's nonce is discounted, and every other
        guard-protocol step still runs.
        """
        master_b64 = str(self.registry().get("master_public_key_b64") or "")
        if not master_b64:
            raise _HttpRequestError(503, "no-verifier", "registry carries no master_public_key_b64")
        consumed = self.prov.consumed_grant_nonces(self.vault_root)
        if allow_spent_nonce:
            consumed = set(consumed)
            consumed.discard(str(grant.get("nonce") or ""))
        try:
            decision = self.authz.verify_grant(
                grant,
                master_public_key=self.authz.public_key_from_b64(master_b64),
                registry=self.registry(),
                node_identity=self.node_identity(),
                consumed_nonces=consumed,
                now=self._now(),
            )
        except (self.authz.AuthzError, self.prov.ProvenanceError) as exc:
            raise _HttpRequestError(403, "grant-refused", str(exc))
        if not decision.allowed:
            raise _HttpRequestError(
                403, "grant-refused", f"{decision.reason}: grant {grant.get('grant_id', '?')} refused"
            )

    def session_allows_reuse(self, grant: dict[str, Any]) -> bool:
        """True when the grant's session has enabled ``vault_access: all``.

        Read on every plain request (reads included) so a session that has taken
        the toggle and performed a write can still read and chat with the same
        grant. A missing session is ``False`` — the default stays one-shot.
        """
        session = self.load_session(str(grant.get("grant_id") or ""))
        return bool(session) and session.get("vault_access") == "all"

    # -- sessions (persistent, keyed by grant_id) --------------------------------

    def _session_path(self, grant_id: str) -> Path:
        return self.sessions_dir() / f"{grant_id}.json"

    def load_session(self, grant_id: str) -> dict[str, Any] | None:
        path = self._session_path(grant_id)
        if not path.is_file():
            return None
        try:
            session = json.loads(path.read_text())
        except json.JSONDecodeError:
            raise _HttpRequestError(500, "corrupt-session", f"session {grant_id} is not JSON")
        return session if isinstance(session, dict) else None

    def _save_session(self, session: dict[str, Any]) -> None:
        path = self._session_path(str(session.get("grant_id") or session.get("session_id")))
        path.write_text(json.dumps(session, indent=2, sort_keys=True) + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _require_owned_session(self, session_id: str, grant: dict[str, Any]) -> dict[str, Any]:
        """Load a session by id and confirm the presenting grant owns it.

        A missing session is 404 no-session (nothing to guard); an existing one
        that belongs to a different grant is 403 not-your-session. The session's
        *id* is decoupled from the grant because forks create sibling sessions
        under the same owner.
        """
        session = self.load_session(session_id)
        if session is None:
            raise _HttpRequestError(404, "no-session", f"no session {session_id!r}")
        owner = str(session.get("owner_grant_id") or session.get("session_id") or session_id)
        if owner != grant.get("grant_id"):
            raise _HttpRequestError(403, "not-your-session", f"session {session_id} belongs to grant {owner}")
        return session

    # -- Step 6: native conversation verbs (agent hand-off, fork, share) --------

    def session_message(self, body: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        """Hand the conversation off: keep the session, change the agent.

        DoD 1 — history survives the hand-off intact because this appends a turn
        to the same grant-owned session instead of starting a new one.
        """
        for key in ("agent", "role", "content"):
            value = body.get(key)
            if not isinstance(value, str) or not value.strip():
                raise _HttpRequestError(400, "malformed", f"message needs a non-empty string {key!r}")
        agent = body["agent"].strip()
        known = {m["id"] for m in _available_models(self.vault_root)}
        if agent not in known:
            raise _HttpRequestError(400, "unknown-model", f"agent {agent!r} is not one of this node's agents")
        turn = {
            "role": body["role"].strip(),
            "content": body["content"].strip(),
            "at": self._now().isoformat(),
            "agent": agent,
        }
        session["conversation"].append(turn)
        session["agent"] = agent
        session["updated_at"] = self._now().isoformat()
        self._save_session(session)
        return {"ok": True, "turn": turn, "turns": len(session["conversation"])}

    def session_fork(self, body: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        """Branch at a message: the child carries the shared prefix, the original
        stays whole (DoD 2). The child is a sibling session under the same owner
        grant; pending permission cards stay on the originating session."""
        conversation = session["conversation"]
        if not conversation:
            raise _HttpRequestError(400, "malformed", "cannot fork an empty conversation")
        at = body.get("at")
        if at is None:
            at = len(conversation) - 1
        if not isinstance(at, int) or isinstance(at, bool) or not (0 <= at < len(conversation)):
            raise _HttpRequestError(
                400, "malformed", f"fork 'at' must be a message index in 0..{len(conversation) - 1}"
            )
        children = session.setdefault("children", [])
        child_id = f"{session['session_id']}-f{len(children) + 1}"
        last = conversation[at]
        child = {
            "schema": SESSION_SCHEMA,
            "session_id": child_id,
            "owner_grant_id": str(session.get("owner_grant_id") or session.get("session_id") or ""),
            "parent_id": session["session_id"],
            "fork_at": at,
            "created_at": self._now().isoformat(),
            "model": session.get("model"),
            "agent": last.get("agent") or session.get("agent"),
            "conversation": conversation[: at + 1],
            "permissions": [],
            "children": [],
            "updated_at": self._now().isoformat(),
        }
        children.append(child_id)
        session["updated_at"] = self._now().isoformat()
        self._save_session(session)
        self._save_session(child)
        return {"ok": True, "session": child, "parent_turns": len(conversation)}

    def session_children(self, session: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "children": list(session.get("children") or [])}

    # -- Step 6/8 share verbs (delegated to the clocked regime engine) ----------

    def share_create(self, body: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        """Draft a share offer through the regime engine (Step 8).

        The engine owns every rule — the five share types, the narrowing-only
        acceptance, the four clocks and the governance mode — so the bridge
        cannot drift from ``contracts/``. On a solo node this records an
        *invite* (a draft with a 1 h window); opening one requires a second
        node to accept, which is `DoD: confirm: user` on this host.
        """
        kind = body.get("kind")
        if kind not in SHARE_KINDS:
            raise _HttpRequestError(400, "malformed", f"kind must be one of {', '.join(SHARE_KINDS)}")
        temporary = bool(body.get("temporary", True))
        regime, records = self._share_verbs(session)
        try:
            offer = regime.offer(
                kind,
                toward=str(body.get("toward") or self.node_identity().get("node_id") or "peer"),
                temporary=temporary,
                scope=body.get("scope"),
            )
        except self._regime_mod.ShareError as exc:
            raise _HttpRequestError(exc.status, exc.reason, exc.detail)
        record = regime.get(offer["share_id"])
        records.append(record)
        session["shares"] = records
        session["updated_at"] = self._now().isoformat()
        self._save_session(session)
        return {"ok": True, "share": self._view_share(record)}

    def share_list(self, session: dict[str, Any]) -> dict[str, Any]:
        regime, records = self._share_verbs(session)
        return {"ok": True, "shares": [self._view_share(regime.shares.get(r["share_id"], r)) for r in records]}

    def share_close(self, session: dict[str, Any], share_id: str) -> dict[str, Any]:
        regime, records = self._share_verbs(session)
        try:
            regime.close(share_id, reason="explicit")
        except self._regime_mod.ShareError as exc:
            raise _HttpRequestError(exc.status, exc.reason, exc.detail)
        record = regime.get(share_id)
        session["updated_at"] = self._now().isoformat()
        self._save_session(session)
        return {"ok": True, "share": self._view_share(record)}

    @staticmethod
    def _view_share(record: dict[str, Any]) -> dict[str, Any]:
        """The client-facing share view: the lifecycle state is surfaced as
        ``status`` (the Step 6 vocabulary), with the regime's own clocks and
        action list kept alongside rather than hidden."""
        share_id = record.get("share_id")
        return {
            "share_id": share_id,
            "kind": record.get("kind"),
            "accepted_kind": record.get("accepted_kind"),
            "status": record.get("state") or record.get("status"),
            "temporary": record.get("temporary"),
            "mode": record.get("mode"),
            "created_at": record.get("created_at"),
            "accept_deadline": record.get("accept_deadline"),
            "idle_deadline": record.get("idle_deadline"),
            "absolute_deadline": record.get("absolute_deadline"),
            "closed_at": record.get("closed_at"),
            "outstanding_actions": list(record.get("outstanding_actions") or []),
        }

    def chat_completions(self, body: dict[str, Any], grant: dict[str, Any]) -> dict[str, Any]:
        model = str(body.get("model") or "")
        known = {m["id"] for m in _available_models(self.vault_root)}
        if model not in known:
            raise _HttpRequestError(400, "unknown-model", f"model {model!r} is not one of this node's agents")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise _HttpRequestError(400, "malformed", "messages must be a non-empty list")

        grant_id = str(grant["grant_id"])
        session = self.load_session(grant_id)
        if session is None:
            session = {
                "schema": SESSION_SCHEMA,
                "session_id": grant_id,
                "owner_grant_id": grant_id,
                "grant_id": grant_id,
                "created_at": self._now().isoformat(),
                "model": model,
                "agent": model,
                "conversation": [],
                "children": [],
            }
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if role and content:
                session["conversation"].append(
                    {"role": role, "content": content, "at": self._now().isoformat(), "agent": model}
                )
        session["model"] = model
        session["agent"] = model
        session["updated_at"] = self._now().isoformat()
        self._save_session(session)

        ack = (
            f"[recorded] session {grant_id}: {len(session['conversation'])} turn(s) routed to "
            f"agent {model!r}. The bridge records and routes; it never impersonates an "
            f"assistant — agent execution arrives with a later step."
        )
        return {
            "id": f"chatcmpl-{grant_id}",
            "object": "chat.completion",
            "created": int(self._now().timestamp()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ack},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": sum(len(m.get("content", "").split()) for m in messages if isinstance(m, dict)),
                "completion_tokens": len(ack.split()),
                "total_tokens": 0,
            },
        }

    # -- permissions (the Approve/Deny card a human resolves) -------------------

    def _require_session(self, grant_id: str) -> dict[str, Any]:
        session = self.load_session(grant_id)
        if session is None:
            raise _HttpRequestError(404, "no-session", f"no session for grant {grant_id}")
        return session

    def permission_register(self, body: dict[str, Any], grant: dict[str, Any]) -> dict[str, Any]:
        """Raise a permission request a human resolves through the card."""
        for key in ("action", "target", "reason"):
            value = body.get(key)
            if not isinstance(value, str) or not value.strip():
                raise _HttpRequestError(400, "malformed", f"permission request needs a non-empty string {key!r}")
        grant_id = str(grant["grant_id"])
        session = self._require_session(grant_id)
        permissions = session.setdefault("permissions", [])
        record = {
            "permission_id": f"perm-{len(permissions) + 1}",
            "action": body["action"].strip(),
            "target": body["target"].strip(),
            "reason": body["reason"].strip(),
            "status": "pending",
            "raised_at": self._now().isoformat(),
        }
        permissions.append(record)
        session["updated_at"] = self._now().isoformat()
        self._save_session(session)
        return {"ok": True, "permission": record}

    def permission_resolve(self, body: dict[str, Any], grant: dict[str, Any], permission_id: str) -> dict[str, Any]:
        """Resolve a pending request — the Approve/Deny decision, persisted."""
        decision = body.get("decision")
        if decision not in ("allow", "deny"):
            raise _HttpRequestError(400, "malformed", f"decision must be 'allow' or 'deny', got {decision!r}")
        grant_id = str(grant["grant_id"])
        session = self._require_session(grant_id)
        permissions = session.get("permissions") or []
        record = next((p for p in permissions if p.get("permission_id") == permission_id), None)
        if record is None:
            raise _HttpRequestError(404, "unknown-permission", f"no permission {permission_id!r} in session {grant_id}")
        if record.get("status") != "pending":
            raise _HttpRequestError(409, "already-resolved", f"permission {permission_id} is {record.get('status')}")
        record["status"] = "resolved"
        record["decision"] = decision
        record["decided_at"] = self._now().isoformat()
        session["updated_at"] = self._now().isoformat()
        self._save_session(session)
        return {"ok": True, "permission": record}

    # -- session vault access (the "all vault tools" toggle) --------------------

    def session_vault_access(self, body: dict[str, Any], grant: dict[str, Any]) -> dict[str, Any]:
        """Turn the session-wide "all vault tools" mode on or off.

        ``mode: "all"`` makes every vault tool usable repeatedly in this session
        by dropping *this grant's own* spent nonce from the reuse set until the
        grant expires; ``mode: "per-tool"`` restores the one-shot default. The
        toggle is gated on a valid, presented grant with a non-empty vault_scope
        (a scope-less grant can reach no note, so there is nothing to widen), and
        it opens no scope the grant did not already carry — only how often it may
        be used. Enabling requires the grant the caller already presents; it is
        not a capability a session can self-grant beyond what the Master signed.
        """
        mode = str(body.get("mode") or "")
        if mode not in ("all", "per-tool"):
            raise _HttpRequestError(400, "malformed", "mode must be 'all' or 'per-tool'")
        if mode == "all" and not list(grant.get("vault_scope") or []):
            raise _HttpRequestError(
                403, "no-scope", "this grant carries no vault_scope; there is no vault tool to widen"
            )
        grant_id = str(grant["grant_id"])
        session = self.load_session(grant_id)
        if session is None:
            session = {
                "schema": SESSION_SCHEMA,
                "session_id": grant_id,
                "owner_grant_id": grant_id,
                "grant_id": grant_id,
                "created_at": self._now().isoformat(),
                "model": "plan",
                "agent": "plan",
                "conversation": [],
                "children": [],
                "permissions": [],
            }
        session["vault_access"] = mode
        session["vault_access_set_at"] = self._now().isoformat()
        session["updated_at"] = self._now().isoformat()
        self._save_session(session)
        return {
            "ok": True,
            "session_id": session["session_id"],
            "vault_access": mode,
            "vault_scope": list(grant.get("vault_scope") or []),
        }

    # -- vault tools -------------------------------------------------------------

    @staticmethod
    def _scope_guard(scope: list[str], rel: str) -> None:
        prov = _load_provenance()
        if not prov.grant_scope_allows(scope, rel):
            raise _HttpRequestError(403, "out-of-scope", f"{rel} is outside grant vault_scope {scope}")

    def vault_list(self, params: dict[str, str], grant: dict[str, Any]) -> dict[str, Any]:
        scope = list(grant.get("vault_scope") or [])
        prefix = str(params.get("prefix") or "")
        if prefix:
            prefix_parts = [p for p in PurePosixPath(prefix).parts if p not in ("", ".")]
        else:
            prefix_parts = []
        recursive = str(params.get("recursive") or "0") in ("1", "true", "yes")
        paths: list[str] = []
        for note in self.prov.note_paths(self.vault_root):
            rel = str(note.relative_to(self.vault_root))
            if not self.prov.grant_scope_allows(scope, rel):
                continue
            parts = PurePosixPath(rel).parts
            if prefix_parts and parts[: len(prefix_parts)] != tuple(prefix_parts):
                continue
            if not recursive and len(parts) != len(prefix_parts) + 1:
                continue
            paths.append(rel)
        return {"ok": True, "scope": scope, "paths": sorted(paths)}

    def vault_cat(self, params: dict[str, str], grant: dict[str, Any]) -> dict[str, Any]:
        rel = _rel_of(str(params.get("path") or ""))
        self._scope_guard(list(grant.get("vault_scope") or []), str(rel))
        note = self.vault_root / rel
        if not note.is_file():
            raise _HttpRequestError(404, "no-note", f"{rel} does not exist")
        content = note.read_bytes().decode("utf-8", "replace")
        return {"ok": True, "path": str(rel), "content": content}

    def vault_capture(self, body: dict[str, Any], grant: dict[str, Any]) -> dict[str, Any]:
        target = str(body.get("target") or "")
        _rel_of(target)
        raw_body = body.get("body")
        if not isinstance(raw_body, str):
            raise _HttpRequestError(400, "malformed", "capture body must be a string")
        try:
            note, decision, block = self.prov.write_granted_note(
                self.vault_root,
                grant,
                target=target,
                body=raw_body.encode("utf-8"),
                session=str(body.get("session") or grant["grant_id"]),
                registry=self.registry(),
                node_identity=self.node_identity(),
                agent=str(body.get("agent") or "grant"),
                model=str(body.get("model") or "local"),
                clock=self.clock,
                allow_reused_nonce=self.session_allows_reuse(grant),
            )
        except self.prov.ProvenanceError as exc:
            raise _HttpRequestError(403 if "grant refused" in str(exc) else 400, "refused", str(exc))
        return {
            "ok": True,
            "note": str(note.relative_to(self.vault_root)),
            "decision": decision,
            "grant_use_id": block["data"].get("grant_use_id"),
        }

    def vault_promote(self, body: dict[str, Any], grant: dict[str, Any]) -> dict[str, Any]:
        source = str(body.get("source") or "")
        target = str(body.get("target") or "")
        _rel_of(source)
        _rel_of(target)
        try:
            note, decision, block = self.prov.promote_granted_note(
                self.vault_root,
                grant,
                source=source,
                target=target,
                session=str(body.get("session") or grant["grant_id"]),
                registry=self.registry(),
                node_identity=self.node_identity(),
                agent=str(body.get("agent") or "grant"),
                model=str(body.get("model") or "local"),
                clock=self.clock,
                allow_reused_nonce=self.session_allows_reuse(grant),
            )
        except self.prov.ProvenanceError as exc:
            raise _HttpRequestError(403 if "grant refused" in str(exc) else 400, "refused", str(exc))
        return {
            "ok": True,
            "note": str(note.relative_to(self.vault_root)),
            "decision": decision,
            "grant_use_id": block["data"].get("grant_use_id"),
        }

    def vault_patch(self, body: dict[str, Any], grant: dict[str, Any]) -> dict[str, Any]:
        target = str(body.get("target") or "")
        _rel_of(target)
        if not isinstance(body.get("patch"), dict):
            raise _HttpRequestError(400, "malformed", "patch must be an object with `old` and `new`")
        try:
            note, decision, block = self.prov.patch_granted_note(
                self.vault_root,
                grant,
                target=target,
                patch=body.get("patch"),
                session=str(body.get("session") or grant["grant_id"]),
                registry=self.registry(),
                node_identity=self.node_identity(),
                agent=str(body.get("agent") or "grant"),
                model=str(body.get("model") or "local"),
                clock=self.clock,
                allow_reused_nonce=self.session_allows_reuse(grant),
            )
        except self.prov.ProvenanceError as exc:
            raise _HttpRequestError(403 if "grant refused" in str(exc) else 400, "refused", str(exc))
        return {
            "ok": True,
            "note": str(note.relative_to(self.vault_root)),
            "decision": decision,
            "grant_use_id": block["data"].get("grant_use_id"),
        }

    # -- dispatch ----------------------------------------------------------------

    def handle(
        self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any], grant: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/v1/models":
            return 200, {
                "object": "list",
                "data": [
                    {
                        "id": m["id"],
                        "object": "model",
                        "owned_by": "sparknet-peer",
                        "created": 0,
                        "agent": m["agent"],
                        "selectable": m["selectable"],
                    }
                    for m in _available_models(self.vault_root)
                ],
            }
        if method == "GET" and path == "/v1/openapi.json":
            spec_path = self.vault_root / "manifests" / "vault-openapi.json"
            if not spec_path.is_file():
                raise _HttpRequestError(404, "missing-spec", "no manifests/vault-openapi.json in this deployment")
            return 200, json.loads(spec_path.read_text(encoding="utf-8"))
        if method == "POST" and path == "/v1/chat/completions":
            return 200, self.chat_completions(body, grant)
        if path.startswith("/session/"):
            rest = urllib.parse.unquote(path[len("/session/") :].strip("/"))
            parts = [part for part in rest.split("/") if part]
            if not parts:
                raise _HttpRequestError(404, "unknown-path", f"{method} /session/")
            if method == "POST" and len(parts) == 2 and parts[1] == "vault-access":
                return 200, self.session_vault_access(body, grant)
            session = self._require_owned_session(parts[0], grant)
            if method == "GET" and len(parts) == 1:
                return 200, {"ok": True, "session": session}
            if method == "POST" and len(parts) == 2 and parts[1] == "message":
                return 200, self.session_message(body, session)
            if method == "POST" and len(parts) == 2 and parts[1] == "fork":
                return 200, self.session_fork(body, session)
            if method == "GET" and len(parts) == 2 and parts[1] == "children":
                return 200, self.session_children(session)
            if method == "POST" and len(parts) == 2 and parts[1] == "share":
                return 200, self.share_create(body, session)
            if method == "GET" and len(parts) == 2 and parts[1] == "shares":
                return 200, self.share_list(session)
            if method == "DELETE" and len(parts) == 3 and parts[1] == "share":
                return 200, self.share_close(session, parts[2])
            if method == "POST" and len(parts) == 2 and parts[1] == "permissions":
                return 200, self.permission_register(body, grant)
            if method == "POST" and len(parts) == 3 and parts[1] == "permissions":
                return 200, self.permission_resolve(body, grant, parts[2])
            raise _HttpRequestError(404, "unknown-path", f"{method} /session/{rest}")
        if path == "/vault/list":
            params = {k: v[0] if v else "" for k, v in query.items()}
            return 200, self.vault_list(params, grant)
        if path == "/vault/cat":
            params = {k: v[0] if v else "" for k, v in query.items()}
            return 200, self.vault_cat(params, grant)
        if method == "POST" and path == "/vault/capture":
            return 200, self.vault_capture(body, grant)
        if method == "POST" and path == "/vault/promote":
            return 200, self.vault_promote(body, grant)
        if method == "POST" and path == "/vault/patch":
            return 200, self.vault_patch(body, grant)
        raise _HttpRequestError(404, "unknown-path", f"{method} {path}")


def _make_router(states: list[str]) -> Callable[[str, str, dict[str, Any], dict[str, Any]], tuple[int, dict[str, Any]]]:
    """The signed personality's router: /v1/status and A2A answers only."""

    def router(method: str, path: str, body: dict[str, Any], identity: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/v1/status":
            return 200, {"ok": True, "states": states, "note": APPROVALS_NOTE}
        if method == "POST" and path == "/v1/status":
            return 200, {"ok": True, "states": states, "note": APPROVALS_NOTE}
        return 404, {"ok": False, "reason": "unknown-path", "path": path}

    return router


def _make_handler(bridge: Bridge, states: list[str], signed_router: Callable) -> type:
    """A handler that multiplexes the signed envelope and the plain surface.

    Every caller is loopback-local; the node's own Open WebUI and agent sessions
    are the only clients. The signed personality passes through to
    shared/sparknet-http.py's behavior untouched, so the fabric's existing
    transport tests keep applying.
    """

    class Handler(bridge.http._Handler):
        server_version = "sparknet-bridge/1"

        def _is_plain(self) -> bool:
            return self.path.split("?", 1)[0].startswith(PLAIN_PREFIXES)

        def _loopback_guard(self) -> None:
            source = self.client_address[0]
            if source not in ("127.0.0.1", "::1"):
                raise _HttpRequestError(403, "off-loopback", f"this surface is loopback-only; caller {source}")

        def _plain(self, method: str, body: dict[str, Any]) -> None:
            try:
                self._loopback_guard()
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer "):
                    raise _HttpRequestError(401, "missing-grant", "Authorization: Bearer <grant> is required")
                grant = bridge.decode_grant(auth[len("Bearer ") :].strip())
                bridge.verify_grant(grant, allow_spent_nonce=bridge.session_allows_reuse(grant))
                path, _, raw_query = self.path.partition("?")
                query = urllib.parse.parse_qs(raw_query)
                code, payload = bridge.handle(method, path, query, body, grant)
            except _HttpRequestError as exc:
                self._send(exc.status, {"ok": False, "reason": exc.reason, "detail": exc.detail})
                return
            except Exception as exc:  # noqa: BLE001 - never leak internals to a caller
                print(f"[bridge] internal error on {self.command} {self.path}: {exc!r}", file=sys.stderr)
                self._send(500, {"ok": False, "reason": "internal"})
                return
            self._send(code, payload)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            if self._is_plain():
                self._plain("GET", {})
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            if self._is_plain():
                try:
                    raw = self._read_body()
                except bridge.http.RequestError as exc:
                    self._refuse(exc)
                    return
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._refuse(bridge.http.RequestError(400, "malformed", f"body is not JSON: {exc}"))
                    return
                if not isinstance(body, dict):
                    self._refuse(bridge.http.RequestError(400, "malformed", "body is not a JSON object"))
                    return
                self._plain("POST", body)
                return
            super().do_POST()

        def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
            if self._is_plain():
                self._plain("DELETE", {})
                return
            super().do_DELETE()

    return Handler


def serve(
    bind,
    port,
    vault_root,
    *,
    registry=None,
    node_identity=None,
    ready=None,
    clock=None,
):
    """Serve the combined surface on (bind, port). Refuses to bind unsigned."""
    http = _load_http()
    authz = http._load_authz()
    private_key = _load_keypair(vault_root, authz)
    if private_key is None:
        log("ERROR", "no node key in the state dir; run `make user-init` (serve refuses to bind unsigned)")
        return 1
    bridge = Bridge(vault_root, registry=registry, node_identity=node_identity, clock=clock)
    signed_router = _make_router(STATES)
    server = ThreadingHTTPServer((bind, port), _make_handler(bridge, STATES, signed_router))
    server.router = signed_router  # type: ignore[attr-defined] - signed personality
    server.public_key = private_key.public_key()  # type: ignore[attr-defined]
    server.clock = clock  # type: ignore[attr-defined]
    server.max_age_seconds = http.DEFAULT_MAX_AGE_SECONDS  # type: ignore[attr-defined]
    server.loopback_only_routes = ()  # type: ignore[attr-defined]
    if ready is not None:
        ready()
    log("INFO", f"serving signed + OpenAI surface on {bind}:{port} (PPP states: {', '.join(STATES)})")
    log("INFO", APPROVALS_NOTE)
    log("INFO", f"models: {', '.join(m['id'] for m in _available_models(vault_root)) or '(none)'}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main():
    p = argparse.ArgumentParser(description="Sparknet Dispatch Bridge with PPE (server)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--health", action="store_true", help="Health check")
    p.add_argument("--serve", action="store_true", help="Serve the combined signed + OpenAI surface")
    p.add_argument("--bind", default=DEFAULT_BIND, help=f"bind address for --serve (default {DEFAULT_BIND})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port for --serve (default {DEFAULT_PORT})")
    p.add_argument("--registry", type=Path, help="override the Master-signed registry manifest")
    p.add_argument("--node-identity", type=Path, help="override the node identity manifest")
    args = p.parse_args()

    script_dir = Path(__file__).resolve().parent
    vault_root = Path(os.environ.get("SPARKNET_VAULT_ROOT") or script_dir.parent)

    log("INFO", "sparknet-dispatch-bridge starting")
    log("INFO", f"sessions (persistent, grant-keyed): {(_state_dir(vault_root) / 'sessions')}")

    if args.health:
        log("INFO", "Health check: OK")
        log("INFO", f"PPE states: {', '.join(STATES)}")
        log("INFO", f"models: {', '.join(m['id'] for m in _available_models(vault_root)) or '(none)'}")
        return

    if args.serve:
        return serve(args.bind, args.port, vault_root, registry=args.registry, node_identity=args.node_identity)

    if args.dry_run:
        log("INFO", "DRY-RUN: PPE workflow ready (PLAN→PERMISSION→EXECUTE)")
        log("INFO", "sparknet-dispatch-bridge completed (DRY-RUN)")
        return

    log("INFO", APPROVALS_NOTE)
    log("INFO", "sparknet-dispatch-bridge completed (template mode)")
    return


if __name__ == "__main__":
    main()
