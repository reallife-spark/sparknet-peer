#!/usr/bin/env python3
"""Sparknet node admin agent — auth events + approval-mediated signup (peer PLAN Step 12).

Design: ../ADMIN_FEDERATION_DESIGN.md (fabric root, fabric PLAN Step 29).

Every node runs its own Open WebUI. This agent gives the node a local, honest
record of auth activity and turns its chat surface into an approval-mediated
one, under the operator's signed-federation design:

* **Signup is enabled but gated.** `DEFAULT_USER_ROLE=pending` (in the ai-stack
  compose) means a new account reaches nothing until an admin accepts it. This
  agent records those signups and role changes so that decision is made from
  evidence, not in a vacuum.
* **Auth-event ingest.** A loopback HTTP listener receives the node's Open WebUI
  event webhooks and appends them to an append-only spool
  (`$SPARKNET_STATE_DIR/admin-events/events.jsonl`, mode 0600). The allow-list is
  closed: `auth.signup`, `auth.login`, `auth.logout`, `auth.password_changed`,
  `user.role_updated`. `auth.login` fires on *success*, so "successful logins
  only" is the catalog's default, not a filter we trust ourselves to remember.
* **Webhook registration.** Open WebUI has no supported env var for the per-event
  webhook list, so `--register-webhooks` configures it through the node's own
  admin API (`POST /api/events/webhooks`), idempotently.
* **Admin snapshot.** `--poll-once` reads `/api/v1/users/` with the node's
  service-account token and writes a normalised snapshot the Master's future
  console reconciles against.

The service account is the node's WebUI admin: password generated at deploy,
0600 under the state dir, read only by this agent and the vault provisioner. No
human logs in as administrator on a peer. The agent holds **no signing key** and
performs **no administrative mutation**; the signed cross-node route it feeds
(`admin-supervise` + the bridge `/admin/*`) is fabric Step 29 Phase 2.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROLE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROLE_ROOT.parent / "shared"))
try:
    from sparknet_loader import load_shared  # noqa: E402 - shared/ is added by path above
except ImportError:  # a role copy without shared/ can still answer --help

    def load_shared(filename, modname, **_kwargs):
        raise FileNotFoundError(f"cannot load {filename}: shared/sparknet_loader.py is not present")


#: The closed set of auth events this node records. Everything in the Open WebUI
#: auth catalog is a *success* event, so no failure capture is possible here by
#: construction — and the allow-list keeps the spool free of noise.
EVENTS_ALLOWLIST = frozenset(
    {
        "auth.signup",
        "auth.login",
        "auth.logout",
        "auth.password_changed",
        "user.role_updated",
    }
)

#: Loopback listener; the node's own Open WebUI posts webhooks here (host netns).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4130


class Refusal(RuntimeError):
    """An operation refused to run safely. Message is the reason."""


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract_name(payload: Any) -> str | None:
    """The event name, whichever key Open WebUI's version ships."""
    if isinstance(payload, dict):
        for key in ("event", "name", "__event_name__"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return None


def _extract_actor(payload: dict[str, Any]) -> dict[str, str]:
    """A minimal, safe actor record; never raw secrets."""
    actor = payload.get("actor")
    if isinstance(actor, dict):
        contact = actor.get("email") or actor.get("name") or str(actor.get("id", "") or "")
        actor_id = actor.get("id")
    else:
        user = payload.get("user")
        if isinstance(user, dict):
            contact = user.get("email") or user.get("name") or str(user.get("id", "") or "")
            actor_id = user.get("id")
        else:
            contact, actor_id = "", None
    return {
        "actor": contact,
        "actor_id": str(actor_id) if actor_id is not None else "",
    }


def normalize_event(payload: Any, fallback_ts: str | None = None) -> dict[str, Any] | None:
    """Project a webhook payload onto a compact, stable spool record.

    Returns ``None`` for anything outside the allow-list or unnameable — the
    caller records it as ignored rather than corrupting the spool.
    """
    name = _extract_name(payload)
    if name not in EVENTS_ALLOWLIST:
        return None
    body = payload if isinstance(payload, dict) else {}
    data = body.get("data")
    if not isinstance(data, dict):
        data = {}
    ts = str(data.get("timestamp") or body.get("timestamp") or fallback_ts or _now_iso())
    record: dict[str, Any] = {
        "event": name,
        "ts": ts,
        "node": os.uname().nodename,
    }
    record.update(_extract_actor(payload))
    rec_from = data.get("role") or body.get("role")
    if rec_from is not None:
        record["role"] = rec_from
    rec_to = data.get("new_role") or body.get("new_role")
    if rec_to is not None:
        record["new_role"] = rec_to
    subject = data.get("subject_id") or data.get("id") or body.get("id")
    if subject is not None:
        record["subject_id"] = str(subject)
    return record


def spool_path(state_dir: Path) -> Path:
    return state_dir / "admin-events" / "events.jsonl"


def append_event(spool: Path, record: dict[str, Any]) -> bool:
    """Append one JSON record to the spool; 0600, one object per line."""
    spool.parent.mkdir(parents=True, exist_ok=True)
    with spool.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(spool, 0o600)
    except OSError:
        pass
    return True


def read_spool(spool: Path) -> list[dict[str, Any]]:
    """Spool rows as parsed objects; a corrupt row is skipped, not fatal."""
    if not spool.exists():
        return []
    rows = []
    for line in spool.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


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


def webui_token(base: str, email: str, password: str) -> str:
    status, body = _http_json(f"{base}/api/v1/auths/signin", "POST", None, {"email": email, "password": password})
    if status != 200 or not isinstance(body, dict) or "token" not in body:
        raise Refusal(f"signin failed: HTTP {status}: {body}")
    return body["token"]


def admin_api_call(url: str, method: str, token: str | None, body: Any = None) -> tuple[int, Any]:
    """A raw call to this node's own Open WebUI admin API.

    The bridge executes signed admin requests through this client rather than
    reimplementing it, so the transport, the error decoding and the accept
    headers stay as one. ``url`` comes from the bridge's closed action mapping.
    """
    return _http_json(url, method, token, body)


def _collection(body: Any, key: str) -> list | None:
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


def ensure_auth_webhooks(
    base: str, token: str, url: str, events: tuple[str, ...] | None = None, name: str = "sparknet-admin-agent"
) -> tuple[str, str]:
    """Register (or update) this node's auth-event webhook, idempotently.

    Open WebUI 0.10+ stores event webhooks under the ``events.webhooks`` config
    key, managed through the admin API. There is no supported env var to express
    the per-event list in the compose file, so the agent configures it against
    its *own* surface at deploy time.
    """
    events = tuple(events or EVENTS_ALLOWLIST)
    status, existing = _http_json(f"{base}/api/events/webhooks", "GET", token)
    rows = _collection(existing, "webhooks")
    if rows is None:
        rows = existing if isinstance(existing, list) else []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("url") == url:
            wid = row.get("id")
            if not wid:
                continue
            res, resp = _http_json(
                f"{base}/api/events/webhooks/{wid}", "PUT", token, {"url": url, "events": list(events), "enabled": True}
            )
            if res != 200:
                raise Refusal(f"webhook update failed: HTTP {res}: {resp}")
            return "updated", str(wid)
    res, resp = _http_json(
        f"{base}/api/events/webhooks",
        "POST",
        token,
        {"name": name, "url": url, "events": list(events), "enabled": True},
    )
    if res != 200:
        raise Refusal(f"webhook create failed: HTTP {res}: {resp}")
    wid = resp.get("id") if isinstance(resp, dict) else None
    return "created", str(wid) if wid else ""


def snapshot_users(base: str, token: str, state_dir: Path) -> Path:
    """Write the current users/roles as a JSON snapshot, 0600."""
    users = fetch_users(base, token)
    tally = {"total": len(users), "pending": 0, "user": 0, "admin": 0}
    for user in users:
        role = user.get("role")
        tally[role] = tally.get(role, 0) + 1
    out = state_dir / "admin-events" / "users.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"tally": tally, "users": users, "taken_at_usec": int(time.time() * 1_000_000)}, indent=2, sort_keys=True
        )
    )
    os.chmod(out, 0o600)
    return out


def _load_http() -> Any:
    return load_shared("sparknet-http.py", "sparknet_http_admin_agent", dirs=[ROLE_ROOT.parent / "shared"])


def build_listener(spool: Path, host: str, port: int) -> Any:
    """The loopback auth-event listener, with the one route-table policy."""
    http = _load_http()

    def health(request: Any = None) -> Any:
        return http.Response(200, {"ok": True})

    def events(request: Any) -> Any:
        record = normalize_event(request.json)
        if record is None:
            # Out of allow-list, or unnameable. Acknowledge silently: Open WebUI
            # will only retry failures, and an ignored known-good name is not a
            # failure the webhook engine should retry against this endpoint.
            return http.Response(202, raw=b"")
        append_event(spool, record)
        return http.Response(204, raw=b"")

    return http.listen(
        [
            http.Route("/health", health, method="GET", source="loopback"),
            http.Route("/events", events, method="POST", source="loopback"),
        ],
        name="admin-agent",
        bind=host,
        port=port,
    )


def run_serve(spool: Path, host: str, port: int, once: bool = False) -> None:
    listener = build_listener(spool, host, port)
    log(f"admin-event listener on {host}:{port} -> {spool}")
    if once:
        listener.servers[0].handle_request()
    else:
        listener.wait()


def load_admin_password(password_file: Path) -> str:
    if not password_file.expanduser().exists():
        raise Refusal(f"missing admin password file: {password_file}")
    value = password_file.read_text().strip()
    if not value:
        raise Refusal(f"empty admin password file: {password_file}")
    return value


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Sparknet node admin agent (peer PLAN Step 12): auth-event ingest "
            "for the node's own Open WebUI, pending-signup supervision, webhook "
            "registration. Holds no signing key; performs no admin mutation."
        )
    )
    p.add_argument(
        "--webui",
        default=os.environ.get("SPARKNET_WEBUI") or "http://127.0.0.1:8181",
        help="this node's Open WebUI base URL",
    )
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
        "--state-dir",
        type=lambda s: Path(s).expanduser(),
        default=Path(os.environ.get("SPARKNET_STATE_DIR") or "~/.local/state/sparknet"),
    )
    p.add_argument(
        "--spool",
        type=lambda s: Path(s).expanduser(),
        default=None,
        help="override the event spool path (default: <state-dir>/admin-events/events.jsonl)",
    )
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--serve", action="store_true", help="run the loopback webhook listener (default mode)")
    p.add_argument(
        "--register-webhooks",
        action="store_true",
        help="idempotently configure the node's Open WebUI auth-event webhook to this node",
    )
    p.add_argument("--poll-once", action="store_true", help="snapshot the node's users/roles once, then exit")
    p.add_argument("--once", action="store_true", help="with --serve: handle one request, then exit")
    args = p.parse_args(argv)
    if args.spool is None:
        args.spool = spool_path(args.state_dir)
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)

    if args.register_webhooks or args.poll_once or args.serve:
        password = load_admin_password(args.admin_password_file)
        token = webui_token(args.webui, args.admin_email, password)
        if args.register_webhooks:
            url = f"http://{args.host}:{args.port}/events"
            action, wid = ensure_auth_webhooks(args.webui, token, url)
            log(f"webhook {action} id={wid or '-'} -> {url}")
        if args.poll_once:
            out = snapshot_users(args.webui, token, args.state_dir)
            log(f"users snapshot -> {out}")
        if args.serve or args.once:
            run_serve(args.spool, args.host, args.port, once=args.once)
    else:
        log(f"no action requested; spool={args.spool} (use --serve / --register-webhooks / --poll-once)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
