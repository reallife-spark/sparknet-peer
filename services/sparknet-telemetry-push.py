#!/usr/bin/env python3
"""The peer's telemetry pusher — evidence to the Master, never authority.

Phase 4 is Master observability *without touching the data path*: peers talk to
each other directly and the Master is authority-plane only, so monitoring means
evidence the peer **pushes**, never a Master that sits in the middle. This is the
peer half of that channel. Contract: ``contracts/telemetry-schema.json``; design:
``MASTER_OBSERVABILITY_DESIGN.md``.

A peer without the capability is silent
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The pusher reads its own identity, the Master-signed registry and the Master's
public key, and calls ``authz.evaluate`` exactly as the guard's
``authorization_capabilities`` does. It pushes **only if ``telemetry`` is
granted**. A peer the Master has not granted it exits 0 having sent nothing —
the push-side mirror of the intake's ``capability_missing`` gate. Silence is not
a failure: a timer that went red on every ungranted node would train an operator
to ignore it.

Attribution is the authenticated overlay
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
There is deliberately no signature here. Nothing in the fabric binds a peer
fingerprint to a node signing public key (Step 68A), so a node-key signature
would be unverifiable theatre. WireGuard authenticates the source address and the
Master's registry maps that address to a fingerprint; the report names its own
fingerprint so the intake can refuse a mismatch (``wrong_peer``). ``nonce`` plus
``expires_at`` are the replay/freshness guard a signed envelope would otherwise
have.

What is pushed
~~~~~~~~~~~~~~
``status`` and ``metrics`` by default, built with
``authz.build_telemetry_report`` so the peer never hand-rolls the envelope:

* ``status`` — ``node_id``, ``role``, ``overlay_ipv4`` and the capabilities the
  Master granted. (Step 70G adds the retained trust flags here; no new kind.)
* ``metrics`` — uptime, load average, memory, the vault filesystem's free
  space, and the local idle evidence (session/GPU/WireGuard probes), each read
  from the host and omitted as ``null`` when unavailable. Idle evidence is
  evidence only: the scheduler, not this pusher, decides what is idle.

Telemetry carries no authority
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Nothing here signs, grants, revokes, escalates, widens a permission or approves
a guard application. The payload is opaque to the envelope by contract; this
module only measures and reports. A report that could not be built is skipped
with a reason on stderr, never invented.

Usage::

    sparknet-telemetry-push.py                 # status + metrics to the Master
    sparknet-telemetry-push.py --kind status   # one kind
    sparknet-telemetry-push.py --dry-run       # print the reports, send nothing
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
try:
    from sparknet_loader import load_shared  # noqa: E402 - shared/ is added by path above
except ImportError:  # a role copy without shared/ can still answer --help

    def load_shared(filename, modname, **_kwargs):
        raise FileNotFoundError(f"cannot load {filename}: shared/sparknet_loader.py is not present")


SERVICE_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT_ROOT = SERVICE_DIR.parent

DEFAULT_PORT = 8090
ROUTE = "/api/v1/telemetry"
DEFAULT_ANCHOR = "10.100.0.1"
DEFAULT_KINDS = ("status", "metrics")
DEFAULT_TIMEOUT = 10


def _shared_dirs() -> list[Path]:
    dirs = []
    env_shared = os.environ.get("SPARKNET_SHARED_DIR", "").strip()
    if env_shared:
        dirs.append(Path(env_shared))
    dirs.extend([DEFAULT_VAULT_ROOT.parent / "shared", SERVICE_DIR])
    return dirs


def _load_authz() -> Any:
    """Load the one shared authorization implementation through the one loader."""
    return load_shared("sparknet-authz.py", "sparknet_telemetry_authz", dirs=_shared_dirs(), missing_ok=True)


def _load_idle() -> Any:
    """Load the shared idle-evidence collector through the one loader."""
    return load_shared("sparknet-idle.py", "sparknet_telemetry_idle", dirs=_shared_dirs(), missing_ok=True)


def _load_share_regime() -> Any:
    """Load the one trust-state implementation through the one loader."""
    return load_shared(
        "sparknet-share-regime.py",
        "sparknet_telemetry_share_regime",
        dirs=[SERVICE_DIR, DEFAULT_VAULT_ROOT / "services"],
        missing_ok=True,
    )


def _state_dir(vault_root: Path) -> Path:
    """The bridge's runtime state, read exactly where the bridge writes it."""
    env = os.environ.get("SPARKNET_STATE_DIR", "").strip()
    return Path(env) if env else vault_root / ".state"


def _share_records(vault_root: Path) -> list[dict[str, Any]]:
    """Read persisted share records from the bridge's grant-keyed sessions.

    A session can name the same share more than once after a restore, so identity
    is ``share_id``. An unreadable session is skipped rather than allowed to fail
    the whole telemetry run: status is evidence, and one corrupt local file must
    not silence a node.
    """
    sessions = _state_dir(vault_root) / "sessions"
    if not sessions.is_dir():
        return []
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(sessions.glob("*.json")):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(session, dict):
            continue
        for record in session.get("shares") or []:
            if isinstance(record, dict) and record.get("share_id"):
                records[str(record["share_id"])] = record
    return [records[share_id] for share_id in sorted(records)]


def trust_state(vault_root: Path, node_id: str) -> dict[str, Any]:
    """This peer's trust belief and retained evidence, for the status payload."""
    regime = _load_share_regime()
    if regime is None:
        raise RuntimeError("peer-node/services/sparknet-share-regime.py not found; cannot summarize trust")
    return regime.trust_summary(_share_records(vault_root), node_id)


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _uptime_seconds() -> int | None:
    try:
        return int(float(_read_text("/proc/uptime").split()[0]))
    except (IndexError, ValueError):
        return None


def _load_average() -> list[float] | None:
    try:
        values = [float(part) for part in _read_text("/proc/loadavg").split()[:3]]
    except ValueError:
        return None
    return values if len(values) == 3 else None


def _meminfo_kb(field: str) -> int | None:
    for line in _read_text("/proc/meminfo").splitlines():
        if line.startswith(field + ":"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _disk_usage(vault_root: Path) -> tuple[int, int] | None:
    try:
        usage = shutil.disk_usage(vault_root)
    except OSError:
        return None
    return usage.total, usage.free


def memory_payload(vault_root: Path) -> dict[str, Any] | None:
    """Return content-free memory counts for the status payload.

    The Master gets posture, never content. A missing gateway or token is an
    omitted block, not a fabricated zero.
    """
    state = Path(os.environ.get("SPARKNET_STATE_DIR", "").strip() or (vault_root / ".state"))
    try:
        token = (state / "memory.local.token").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not token:
        return None
    base = os.environ.get("SPARKNET_MEMORY_GATEWAY_URL", "http://127.0.0.1:4093").rstrip("/")
    query = urllib.parse.urlencode({"vault_path": str(vault_root)})
    request = urllib.request.Request(base + "/v1/memory/status?" + query)
    request.add_header("Accept", "application/json")
    request.add_header("X-Sparknet-Local-Token", token)
    request.add_header("X-Sparknet-Vault-Path", str(vault_root))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            status = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    policy = status.get("policy") if isinstance(status.get("policy"), dict) else {}
    return {
        "schema": status.get("schema") or "sparknet-memory-status/1",
        "vaults": 1,
        "records": status.get("records", 0),
        "bytes": status.get("bytes", 0),
        "by_type": status.get("by_type") or {},
        "by_state": status.get("by_state") or {},
        "capture": bool(policy.get("capture", True)),
        "last_write": status.get("newest"),
    }


def status_payload(
    identity: dict[str, Any],
    capabilities: list[str],
    trust: dict[str, Any] | None = None,
    memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The stable facts the Master needs to place this node. No authority in it.

    Trust is a belief copied from the share regime's own summary. It is surfaced
    for a human to review; it is not a permission and cannot move one.
    """
    payload = {
        "node_id": identity.get("node_id"),
        "role": identity.get("role"),
        "overlay_ipv4": identity.get("overlay_ipv4"),
        "capabilities": sorted(capabilities),
    }
    if trust is not None:
        payload["trust"] = trust
    if memory:
        payload["memory"] = memory
    return payload


def idle_evidence() -> dict[str, Any]:
    """Collect local idle evidence without letting one bad probe silence metrics."""
    module = _load_idle()
    if module is None:
        return {"schema_version": "sparknet-idle/1", "sampled_at": None, "errors": ["idle_module_missing"]}
    try:
        return module.collect_idle_evidence()
    except Exception:
        return {"schema_version": "sparknet-idle/1", "sampled_at": None, "errors": ["idle_probe_failed"]}


def metrics_payload(vault_root: Path) -> dict[str, Any]:
    """Host measurements, read defensively: a missing probe becomes null, not zero.

    Zero would look like a real measurement (a node with no load, no memory);
    null says the probe did not answer, which is the truthful report.
    """
    disk = _disk_usage(vault_root)
    return {
        "uptime_seconds": _uptime_seconds(),
        "load_average": _load_average(),
        "memory_total_kb": _meminfo_kb("MemTotal"),
        "memory_available_kb": _meminfo_kb("MemAvailable"),
        "vault_disk_total_bytes": disk[0] if disk else None,
        "vault_disk_free_bytes": disk[1] if disk else None,
        "idle": idle_evidence(),
    }


def resolve_endpoint(vault_root: Path, override: str | None = None) -> str:
    """Where to POST: ``--endpoint`` > ``SPARKNET_TELEMETRY_ENDPOINT`` > anchor.

    The shipped security manifest names the anchor; the environment override is
    for a deployment staged elsewhere, and ``--endpoint`` is for a loopback
    intake in tests or an operator pointing at a relocated Master. A daemon
    caller (the guard's inbox_alert) has no CLI, so ``SPARKNET_TELEMETRY_ENDPOINT``
    accepts a complete URL for relocation and hermetic tests. The path is always
    the contract's route, so an override cannot silently invent a second
    telemetry surface.
    """
    if override:
        return override.rstrip("/")
    complete = os.environ.get("SPARKNET_TELEMETRY_ENDPOINT", "").strip()
    if complete:
        return complete.rstrip("/")
    anchor = os.environ.get("SPARKNET_MASTER_ANCHOR_IP", "").strip()
    if not anchor:
        try:
            policies = json.loads((vault_root / "manifests" / "security-policies.json").read_text())
        except (OSError, json.JSONDecodeError):
            policies = {}
        wireguard = policies.get("wireguard") if isinstance(policies, dict) else None
        if isinstance(wireguard, dict):
            anchor = str(wireguard.get("master_anchor_ip") or "").strip()
        if not anchor and isinstance(policies, dict):
            anchor = str(policies.get("master_anchor_ip") or "").strip()
    return f"http://{anchor or DEFAULT_ANCHOR}:{DEFAULT_PORT}{ROUTE}"


def _load_material(vault_root: Path) -> tuple[dict[str, Any], dict[str, Any], Any] | None:
    identity_path = vault_root / "manifests" / "node-identity.json"
    registry_path = vault_root / "manifests" / "node-authorizations.json"
    pubkey_path = vault_root / "manifests" / "keys" / "master-ed25519.pub.b64"
    if not (identity_path.is_file() and registry_path.is_file() and pubkey_path.is_file()):
        return None
    try:
        identity = json.loads(identity_path.read_text())
        registry = json.loads(registry_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    authz = _load_authz()
    if authz is None:
        return None
    try:
        pubkey = authz.load_public_key(pubkey_path)
    except Exception:
        return None
    return identity, registry, pubkey


def granted_capabilities(vault_root: Path) -> tuple[list[str], dict[str, Any] | None, str]:
    """Return ``(capabilities, identity, refusal)`` from the signed registry.

    A vault that cannot answer is treated like an ungranted one (empty list, a
    reason for the log, exit 0). The guard does the same, and for the same
    reason: an unprovisioned node is not an error condition for a timer.
    """
    material = _load_material(vault_root)
    if material is None:
        return [], None, "no readable identity/registry/master key"
    identity, registry, pubkey = material
    authz = _load_authz()
    try:
        decision = authz.evaluate(identity, registry, pubkey)
    except Exception as exc:  # a malformed vault must not crash the timer
        return [], identity, f"authorization could not be evaluated ({exc})"
    if not decision.allowed:
        return [], identity, decision.reason
    return list(decision.capabilities), identity, ""


def build_reports(
    identity: dict[str, Any],
    capabilities: list[str],
    kinds: list[str],
    vault_root: Path,
) -> list[dict[str, Any]]:
    authz = _load_authz()
    if authz is None:
        raise RuntimeError("shared/sparknet-authz.py not found; cannot build a report")
    fingerprint = authz.fingerprint(identity)
    trust = trust_state(vault_root, str(identity.get("node_id") or "")) if "status" in kinds else None
    reports = []
    for kind in kinds:
        if kind == "status":
            payload = status_payload(identity, capabilities, trust, memory_payload(vault_root))
        elif kind == "metrics":
            payload = metrics_payload(vault_root)
        else:  # the CLI restricts this; a library caller gets a real error
            raise ValueError(f"unsupported kind: {kind}")
        reports.append(authz.build_telemetry_report(kind=kind, peer_fingerprint=fingerprint, payload=payload))
    return reports


def post_report(endpoint: str, report: dict[str, Any], timeout: int) -> tuple[bool, str]:
    """POST one report; return ``(ok, detail)`` and never raise.

    The detail is the intake's refusal body when it answered, or the transport
    error when it did not, so the timer's journal names what happened rather
    than only that it happened.
    """
    body = json.dumps(report, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if 200 <= response.status < 300:
                return True, f"HTTP {response.status}"
            return False, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            detail = ""
        return False, f"HTTP {exc.code} {detail}".strip()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push peer telemetry evidence to the Master (no authority)")
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
        help="peer vault root (default: the directory above this service)",
    )
    parser.add_argument(
        "--endpoint",
        help="override the POST URL (default: master_anchor_ip:8090/api/v1/telemetry)",
    )
    parser.add_argument(
        "--kind",
        action="append",
        choices=DEFAULT_KINDS,
        help="report kind to push (repeatable; default: status and metrics)",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout in seconds")
    parser.add_argument("--dry-run", action="store_true", help="print the reports and send nothing")
    args = parser.parse_args(argv)

    kinds = args.kind or list(DEFAULT_KINDS)
    capabilities, identity, refusal = granted_capabilities(args.vault_root)
    if identity is None or refusal:
        print(f"sparknet-telemetry-push: not pushing: {refusal}", file=sys.stderr)
        return 0
    if "telemetry" not in capabilities:
        print("sparknet-telemetry-push: not pushing: telemetry capability not granted", file=sys.stderr)
        return 0

    reports = build_reports(identity, capabilities, kinds, args.vault_root)
    endpoint = resolve_endpoint(args.vault_root, args.endpoint)
    if not endpoint.startswith(("http://", "https://")):
        print(f"sparknet-telemetry-push: refusing non-HTTP endpoint: {endpoint}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(json.dumps(reports, indent=2, sort_keys=True))
        return 0

    failed = False
    for report in reports:
        ok, detail = post_report(endpoint, report, args.timeout)
        if not ok:
            failed = True
            print(
                f"sparknet-telemetry-push: {report['kind']} to {endpoint} failed: {detail}",
                file=sys.stderr,
            )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
