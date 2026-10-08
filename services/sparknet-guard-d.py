#!/usr/bin/env python3
"""Sparknet Critical File Guard — Master-Gated Immutable File Guard.

Replaces the earlier stub, which only read the manifest and exited. This is the
real Plan-Permission-Execute enforcement point for system-critical paths.

Modes
-----
``anchored``
    The Master Anchor (wg0) is up. The daemon binds the overlay address only
    and accepts signed approval payloads from the Master.

``standalone``
    No overlay. The node still works on its own vault, and the guard still
    *enforces* immutability on the critical paths, but it refuses every remote
    mutation with an explicit reason. This is what "standalone-capable" means:
    degraded, not broken.

Safety posture
--------------
Applying a patch means lifting ``chattr +i`` on a root-owned file under /etc and
writing it. That requires ALL of:

1. a valid Ed25519 signature from the Master over the approval envelope,
2. an unexpired, un-replayed request (nonce + request_id + expiry),
3. a target listed in ``protected_paths`` of the critical manifest,
4. the node's own authorization entry to grant ``guard-apply``,
5. euid == 0,
6. ``--apply-enabled`` on the command line.

Any missing condition is a refusal, never a partial write. Verification and
auditing happen regardless of (5) and (6), so an unarmed daemon still leaves a
complete audit trail.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError as exc:  # pragma: no cover - environment guard
    print(
        f"[guard] FATAL: 'cryptography' is required ({exc}). Install with: make devtools",
        file=sys.stderr,
    )
    raise SystemExit(2) from exc

SERVICE_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT_ROOT = SERVICE_DIR.parent

# --- approval envelope -------------------------------------------------------
#
# The Master signs this canonical JSON. Every field that carries authority *or
# content* is inside it. content_b64 must be signed, not merely hashed-and-sent:
# signing the hash alone would let anything on the path substitute the bytes and
# still match the digest.
APPROVAL_FIELDS = ("file_path", "content_b64", "content_sha256", "issued_at", "expires_at", "nonce", "request_id")

#: Fallback when manifests/security-policies.json is unreadable. Fail to the
#: documented topology rather than to "accept everything".
DEFAULT_MASTER_ANCHOR_IP = "10.100.0.1"


def approval_message(payload: dict[str, Any]) -> bytes:
    """Deterministic bytes the Master signs."""
    try:
        envelope = {field: payload[field] for field in APPROVAL_FIELDS}
    except KeyError as exc:
        raise ValueError(f"approval payload missing field: {exc}") from exc
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Refusal(Exception):
    """A denied request. Carries the HTTP status and a machine-readable code."""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail


# --- audit ledger ------------------------------------------------------------


def block_hash(prev_hash: str, index: int, timestamp: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update((str(prev_hash) + str(index) + str(timestamp) + json.dumps(payload, sort_keys=True)).encode())
    return digest.hexdigest()


class AuditLedger:
    """Append-only, hash-chained record of every guard decision.

    Writes atomically. Callers must never mutate an existing block.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": "1.0",
                "description": "Sparknet guard audit ledger (hash-chained)",
                "genesis_hash": "",
                "blocks": [],
            }
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            # Corrupt ledger must not silently swallow history; quarantine it.
            backup = self.path.with_suffix(".corrupt")
            try:
                self.path.rename(backup)
                print(f"[guard] WARNING: audit ledger unreadable, quarantined to {backup}", file=sys.stderr)
            except OSError:
                pass
            return {
                "version": "1.0",
                "description": "Sparknet guard audit ledger (hash-chained)",
                "genesis_hash": "",
                "blocks": [],
            }

    def append(self, event: str, detail: dict[str, Any]) -> dict[str, Any]:
        ledger = self._load()
        blocks = ledger.setdefault("blocks", [])
        index = len(blocks)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        prev_hash = blocks[-1]["hash"] if blocks else ""
        payload = {"event": event, "detail": detail, "index": index, "timestamp": timestamp, "prev_hash": prev_hash}
        payload["hash"] = block_hash(prev_hash, index, timestamp, payload)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=self.path.parent, prefix=".ledger.", suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            with tmp:
                json.dump(ledger | {"blocks": [*blocks, payload]}, tmp, indent=2)
                tmp.write("\n")
            os.replace(tmp.name, self.path)
        except Exception:
            os.unlink(tmp.name)
            raise
        return payload


# --- replay protection -------------------------------------------------------


class ReplayGuard:
    """nonce / request_id / expiry exactly as manifests/security-policies.json demands.

    Replay state is persisted when ``store_path`` is given, so a restart
    cannot re-open a used approval: without this, bouncing the daemon erased
    the only record that a nonce was spent.  The store is written atomically
    (temp + ``os.replace``) on every spend and pruned to the last two TTLs on
    load, the same bound as in-memory state.
    """

    def __init__(self, ttl_seconds: int = 60, store_path: Path | None = None) -> None:
        self.ttl = ttl_seconds
        self._seen: dict[str, float] = {}
        self.store_path = store_path
        if store_path is not None:
            self._load()

    def _load(self) -> None:
        if self.store_path is None or not self.store_path.is_file():
            return
        try:
            raw = json.loads(self.store_path.read_text())
            cutoff = datetime.now(timezone.utc).timestamp() - self.ttl * 2
            self._seen = {
                str(key): float(value)
                for key, value in raw.get("seen", {}).items()
                if float(value) >= cutoff
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            # A corrupt store must not silently reset replay protection, but it
            # also cannot be trusted; warn loudly and start empty so the daemon
            # still comes up.
            print(f"[guard] WARNING: replay store unreadable ({exc}); starting empty", file=sys.stderr)
            self._seen = {}

    def _save(self) -> None:
        if self.store_path is None:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            "w", dir=self.store_path.parent, prefix=".replay.", suffix=".tmp", delete=False, encoding="utf-8"
        )
        try:
            with tmp:
                json.dump({"version": "1.0", "seen": self._seen}, tmp)
                tmp.write("\n")
            os.replace(tmp.name, self.store_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp.name)
            raise

    def check(self, nonce: str, request_id: str, issued_at: str, expires_at: str, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)

        def parse(value: str) -> datetime:
            text = str(value).replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError as exc:
                raise Refusal(400, "malformed_timestamp", f"{value!r} is not ISO-8601") from exc
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        try:
            issued = parse(issued_at)
            expires = parse(expires_at)
        except Refusal:
            raise

        if now > expires:
            raise Refusal(403, "expired", "approval expired")
        window = timedelta(seconds=self.ttl * 2)
        if now < issued - window:
            raise Refusal(403, "not_yet_valid", "approval issued in the future")
        if (expires - issued) > window:
            raise Refusal(403, "window_too_wide", f"approval window exceeds {self.ttl}s policy")

        key = f"{nonce}:{request_id}"
        if key in self._seen:
            raise Refusal(409, "replayed", f"nonce/request_id already used at {self._seen[key]:.0f}")
        self._seen[key] = now.timestamp()
        # Bound memory: prune anything past twice the TTL.
        cutoff = now.timestamp() - self.ttl * 2
        self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        self._save()


# --- privileged file operations ---------------------------------------------


def _run(argv: list[str], *, dry_run: bool) -> None:
    if dry_run:
        print(f"[guard] DRY-RUN: would run {' '.join(argv)}")
        return
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise Refusal(500, "privileged_command_failed", f"{argv[0]}: {detail}")


def apply_patch(path: Path, content: bytes, *, dry_run: bool = False) -> None:
    """Lift immutability, write atomically, restore root ownership + immutability.

    The write is staged in the same directory so os.replace is atomic on the
    target filesystem, and the immutable bit is restored even if the write
    fails partway.
    """
    if dry_run:
        print(f"[guard] DRY-RUN: would patch {path} ({len(content)} bytes)")
        return
    if not path.exists():
        raise Refusal(404, "target_missing", f"{path} does not exist")

    had_immutable = False
    try:
        _run(["chattr", "-i", str(path)], dry_run=False)
        had_immutable = True
    except Refusal:
        # Not already immutable, which is fine. We still attempt to set it below.
        pass

    try:
        tmp = tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", suffix=".guard", delete=False)
        try:
            with tmp:
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.chmod(tmp.name, 0o644)
            os.replace(tmp.name, path)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp.name)
            raise

        _run(["chown", "root:root", str(path)], dry_run=False)
        _run(["chmod", "644", str(path)], dry_run=False)
    except Refusal:
        raise
    except OSError as exc:
        raise Refusal(500, "write_failed", str(exc)) from exc
    finally:
        # Always attempt to restore immutability, even if the write failed.
        with contextlib.suppress(Refusal):
            _run(["chattr", "+i", str(path)], dry_run=False)

    print(f"[guard] patched and re-locked {path} (was_immutable={had_immutable})")


def enforce_immutability(paths: list[str], *, dry_run: bool = False) -> list[str]:
    """Lock critical paths that are not already immutable. Standalone mode uses this."""
    enforced: list[str] = []
    for raw in paths:
        target = Path(raw)
        if not target.exists():
            continue
        try:
            flags = subprocess.run(["lsattr", "-d", str(target)], capture_output=True, text=True, check=False)
            already = "i" in (flags.stdout.split(" ", 1)[0] if flags.stdout else "")
        except OSError:
            already = False
        if already:
            continue
        try:
            _run(["chattr", "+i", str(target)], dry_run=dry_run)
            enforced.append(raw)
        except Refusal:
            continue
    return enforced


def _load_shared_module(filename: str, modname: str):
    """Import a shared `filename` (hyphenated, so importlib) from the parent vault.

    It lives once in <fabric>/shared/ because the Master and a peer must agree
    on the byte-for-byte implementation — two copies could drift and then
    silently stop validating each other. SPARKNET_SHARED_DIR overrides the
    location for a deployment that stages contracts and shared code elsewhere.

    The filename contains a hyphen, so it cannot be imported by name; use
    importlib and register it in sys.modules (needed for @dataclass).
    """
    import importlib.util

    candidates = []
    env_shared = os.environ.get("SPARKNET_SHARED_DIR", "").strip()
    if env_shared:
        candidates.append(Path(env_shared) / filename)
    candidates.append(DEFAULT_VAULT_ROOT.parent / "shared" / filename)
    candidates.append(SERVICE_DIR / filename)

    path = next((c for c in candidates if c.is_file()), None)
    if path is None:
        return None
    if modname in sys.modules:
        return sys.modules[modname]
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_authz_module():
    """The shared sparknet-authz.py: verification and signing, one copy."""
    return _load_shared_module("sparknet-authz.py", "sparknet_authz")


def _load_http_module():
    """The shared sparknet-http.py: the one handler base every listener uses.

    The handler class is where the socket timeout lives (see
    TimeoutHandler.DEFAULT_REQUEST_TIMEOUT_SECONDS).  The guard must not fall
    back to a bare BaseHTTPRequestHandler when the shared file is missing —
    that would silently revert to unbounded reads — so a hard error beats a
    quiet degradation here.
    """
    shared = _load_shared_module("sparknet-http.py", "sparknet_http")
    if shared is None:
        raise RuntimeError("shared/sparknet-http.py not found; the guard handler cannot be built")
    return shared


# --- guard -------------------------------------------------------------------


class Guard:
    def __init__(
        self,
        vault_root: Path,
        *,
        apply_enabled: bool = False,
        replay: ReplayGuard | None = None,
        master_pubkey: Ed25519PublicKey | None = None,
        ledger: AuditLedger | None = None,
        dry_run: bool = False,
    ) -> None:
        self.vault_root = vault_root
        self.apply_enabled = apply_enabled
        self.dry_run = dry_run
        self.replay = replay or ReplayGuard(
            store_path=vault_root / ".state" / "guard-replay.json"
        )
        self.master_pubkey = master_pubkey
        self.ledger = ledger or AuditLedger(vault_root / "blockchain" / "guard-audit.json")
        # One apply at a time: ThreadingHTTPServer hands each request its own
        # thread, and verification, replay bookkeeping, ledger appends and the
        # privileged write must never interleave.
        self._apply_lock = threading.Lock()

    # -- configuration ------------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.vault_root / "manifests" / "critical-manifest.json").read_text())

    def protected_paths(self) -> list[str]:
        return list(self.manifest().get("protected_paths", []))

    def policies(self) -> dict[str, Any]:
        path = self.vault_root / "manifests" / "security-policies.json"
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def authorization_capabilities(self) -> list[str]:
        """Capabilities the Master granted this node, per the signed registry."""
        sparknet_authz = _load_authz_module()
        if sparknet_authz is None:
            return []
        identity_path = self.vault_root / "manifests" / "node-identity.json"
        registry_path = self.vault_root / "manifests" / "node-authorizations.json"
        pubkey_path = self.vault_root / "manifests" / "keys" / "master-ed25519.pub.b64"
        if not (identity_path.is_file() and registry_path.is_file() and pubkey_path.is_file()):
            return []
        try:
            identity = json.loads(identity_path.read_text())
            registry = json.loads(registry_path.read_text())
            pubkey = sparknet_authz.load_public_key(pubkey_path)
        except Exception:
            return []
        decision = sparknet_authz.evaluate(identity, registry, pubkey)
        return decision.capabilities if decision.allowed else []

    def mode(self) -> str:
        """anchored when an overlay address is configured and reachable."""
        address = self.overlay_address()
        if not address:
            return "standalone"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.settimeout(0.5)
                # The anchor comes from security-policies.json, never a literal:
                # a re-added constant would silently carry a stale private range
                # through a topology change.
                probe.connect((self.master_anchor_address(), 51820))
                local = probe.getsockname()[0]
            return "anchored" if local == address else "standalone"
        except OSError:
            return "standalone"

    def overlay_address(self) -> str | None:
        address = os.environ.get("SPARKNET_OVERLAY_IP")
        if address:
            return address
        identity_path = self.vault_root / "manifests" / "node-identity.json"
        try:
            identity = json.loads(identity_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        value = identity.get("overlay_ipv4")
        return value.split("/")[0] if isinstance(value, str) else None

    def master_anchor_address(self) -> str:
        """The Master's overlay address, from manifests/security-policies.json.

        The guard binds the *node's* overlay address, so every legitimate
        request arrives with the *Master's* source address. Allowing the node's
        own address here would therefore reject the Master, and allowing it on
        principle would also let a local process spoof a source and pass.
        """
        try:
            policies = json.loads((self.vault_root / "manifests" / "security-policies.json").read_text())
        except (OSError, json.JSONDecodeError):
            return DEFAULT_MASTER_ANCHOR_IP
        value = policies.get("wireguard", {}).get("master_anchor_ip")
        return value if isinstance(value, str) else DEFAULT_MASTER_ANCHOR_IP

    def permitted_sources(self) -> set[str]:
        """Client addresses allowed to submit a patch request."""
        return {"127.0.0.1", "::1", self.master_anchor_address()}

    # -- request handling ---------------------------------------------------

    def handle_patch(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Decide and apply one patch request.

        Serialized: the ledger append is a read-modify-write and the write is
        privileged, so ``ThreadingHTTPServer`` concurrency must not interleave
        two requests.  The logic lives in ``_handle_patch_locked``.
        """
        with self._apply_lock:
            return self._handle_patch_locked(packet)

    def _handle_patch_locked(self, packet: dict[str, Any]) -> dict[str, Any]:
        payload_b64 = packet.get("payload")
        signature_b64 = packet.get("signature")
        if not isinstance(payload_b64, str) or not isinstance(signature_b64, str):
            raise Refusal(400, "malformed_packet", "expected base64 payload and signature strings")

        import base64

        try:
            message = base64.b64decode(payload_b64, validate=True)
            signature = base64.b64decode(signature_b64, validate=True)
        except Exception as exc:
            raise Refusal(400, "malformed_packet", str(exc)) from exc

        # 1. Master signature, before anything else is trusted.
        if self.master_pubkey is None:
            raise Refusal(503, "no_master_key", "guard has no Master public key loaded")
        try:
            self.master_pubkey.verify(signature, message)
        except InvalidSignature as exc:
            self.ledger.append("patch_refused", {"code": "bad_signature"})
            raise Refusal(403, "bad_signature", "Ed25519 verification failed") from exc

        try:
            payload = json.loads(message.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Refusal(400, "malformed_payload", str(exc)) from exc

        # 2. Replay / expiry.
        try:
            self.replay.check(
                str(payload.get("nonce", "")),
                str(payload.get("request_id", "")),
                str(payload.get("issued_at", "")),
                str(payload.get("expires_at", "")),
            )
        except Refusal as refusal:
            self.ledger.append("patch_refused", {"code": refusal.code, "detail": refusal.detail})
            raise

        # 3. Content integrity.
        content_b64 = payload.get("content_b64", "")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except Exception as exc:
            raise Refusal(400, "malformed_content", str(exc)) from exc
        actual = hashlib.sha256(content).hexdigest()
        if actual != payload.get("content_sha256"):
            self.ledger.append("patch_refused", {"code": "content_hash_mismatch"})
            raise Refusal(400, "content_hash_mismatch", "content does not match content_sha256")

        # 4. Target must be a registered critical path.
        target_path = payload.get("file_path")
        if not isinstance(target_path, str):
            raise Refusal(400, "missing_file_path", "file_path required")
        protected = self.protected_paths()
        if target_path not in protected:
            self.ledger.append("patch_refused", {"code": "not_protected", "file_path": target_path})
            raise Refusal(403, "not_protected", f"{target_path} is not in protected_paths")

        # 5. This node must be authorized to apply at all.
        capabilities = self.authorization_capabilities()
        if "guard-apply" not in capabilities:
            self.ledger.append(
                "patch_refused", {"code": "capability_missing", "file_path": target_path, "granted": capabilities}
            )
            raise Refusal(403, "capability_missing", f"guard-apply not granted (have: {capabilities or 'none'})")

        # 6. Dry-run short-circuits after verification: the whole point of
        #    --dry-run is to exercise signature, replay, manifest and capability
        #    checks and leave an audit trail without touching the filesystem.
        if self.dry_run:
            block = self.ledger.append(
                "patch_verified_dry_run",
                {"file_path": target_path, "sha256": actual, "request_id": payload.get("request_id")},
            )
            return {
                "status": "PATCH_VERIFIED_DRY_RUN",
                "file_path": target_path,
                "content_sha256": actual,
                "audit_block": block["hash"],
            }

        # 7. Arming, then root.
        if not self.apply_enabled:
            self.ledger.append(
                "patch_verified_not_applied", {"code": "unarmed", "file_path": target_path, "sha256": actual}
            )
            raise Refusal(403, "unarmed", "guard is not running with --apply-enabled")
        if os.geteuid() != 0:
            self.ledger.append("patch_verified_not_applied", {"code": "not_root", "file_path": target_path})
            raise Refusal(403, "not_root", "applying requires euid 0")

        self.ledger.append("patch_applying", {"file_path": target_path, "sha256": actual})
        apply_patch(Path(target_path), content, dry_run=False)
        block = self.ledger.append(
            "patch_applied",
            {"file_path": target_path, "sha256": actual, "request_id": payload.get("request_id")},
        )
        return {
            "status": "PATCH_APPLIED_AND_RE-LOCKED",
            "file_path": target_path,
            "content_sha256": actual,
            "audit_block": block["hash"],
        }


def _make_handler(guard: Guard, server_name: str):  # noqa: C901 - HTTP surface is inherently branchy
    _shared_http = _load_http_module()

    class Handler(_shared_http.TimeoutHandler):
        server_version = "sparknet-guard/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than BaseHTTPRequestHandler
            print(f"[guard] {fmt % args}", file=sys.stderr)

        def _send(self, status: int, body: dict[str, Any]) -> None:
            blob = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/api/v1/status":
                self._send(404, {"error": "not found"})
                return
            self._send(
                200,
                {
                    "service": server_name,
                    "mode": guard.mode(),
                    "apply_enabled": guard.apply_enabled,
                    "dry_run": guard.dry_run,
                    "protected_paths": len(guard.protected_paths()),
                    "capabilities": guard.authorization_capabilities(),
                },
            )

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/api/v1/critical-file/patch":
                self._send(404, {"error": "not found"})
                return
            client = self.client_address[0]
            permitted = guard.permitted_sources()
            # Loopback is allowed so the daemon is testable and so local tooling
            # can reach it. Every other accepted source must be the Master
            # anchor: the server binds the node's own overlay address, so a
            # legitimate request arrives *from* the Master, never from us.
            if client not in permitted:
                # "permitted" is deliberately absent from the body: disclosing
                # the source allow-list to a caller the protocol already
                # rejected hands an off-overlay probe the exact addresses to
                # spoof next time. The refusal is reason enough.
                self._send(
                    403,
                    {
                        "error": "off-overlay",
                        "code": "off_overlay",
                        "client": client,
                    },
                )
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send(400, {"error": "bad content-length", "code": "malformed_packet"})
                return
            if length <= 0 or length > 8 * 1024 * 1024:
                self._send(400, {"error": "bad body length", "code": "malformed_packet"})
                return
            try:
                packet = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send(400, {"error": str(exc), "code": "malformed_packet"})
                return
            try:
                self._send(200, guard.handle_patch(packet))
            except Refusal as refusal:
                self._send(refusal.status, {"error": refusal.detail or refusal.code, "code": refusal.code})
            except Exception as exc:  # pragma: no cover - defensive
                # Never echo the exception: it can name files, keys or paths
                # the caller has no right to know. Log it where only an
                # operator will read it.
                print(f"[guard] internal error on {self.path}: {exc!r}", file=sys.stderr)
                self._send(500, {"error": "internal error", "code": "internal_error"})

    return Handler


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sparknet Critical File Guard (Master-Gated Immutable File Guard)")
    parser.add_argument("--dry-run", action="store_true", help="Verify and audit, never mutate")
    parser.add_argument("--check-manifest", action="store_true", help="Validate the manifest and exit")
    parser.add_argument(
        "--apply-enabled",
        action="store_true",
        help="Arm the daemon to actually apply approved patches (requires euid 0)",
    )
    parser.add_argument(
        "--listen",
        default=None,
        help="Overlay address to bind (default: the node's authorized overlay IP)",
    )
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--bind-any", action="store_true", help="TEST ONLY: allow binding a non-overlay address")
    parser.add_argument("--status", action="store_true", help="Print status as JSON and exit")
    parser.add_argument(
        "--enforce-immutability",
        action="store_true",
        help="Lock critical paths that are not already immutable",
    )
    parser.add_argument("--vault-root", default=None)
    args = parser.parse_args(argv)

    vault_root = (
        Path(args.vault_root).resolve()
        if args.vault_root
        else Path(os.environ.get("SPARKNET_VAULT_ROOT") or DEFAULT_VAULT_ROOT).resolve()
    )

    master_pubkey = None
    pubkey_path = vault_root / "manifests" / "keys" / "master-ed25519.pub.b64"
    if pubkey_path.is_file():
        import base64

        try:
            master_pubkey = Ed25519PublicKey.from_public_bytes(
                base64.b64decode(pubkey_path.read_text().strip(), validate=True)
            )
        except Exception as exc:
            print(f"[guard] ERROR: master public key unusable: {exc}", file=sys.stderr)
            return 1

    guard = Guard(vault_root, apply_enabled=args.apply_enabled, master_pubkey=master_pubkey, dry_run=args.dry_run)

    if args.check_manifest:
        try:
            manifest = guard.manifest()
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[guard] ERROR: manifest unreadable: {exc}", file=sys.stderr)
            return 1
        protected = manifest.get("protected_paths")
        if not isinstance(protected, list) or not all(isinstance(p, str) for p in protected):
            print("[guard] ERROR: protected_paths must be a list of strings", file=sys.stderr)
            return 1
        sot = manifest.get("source_of_truth", {})
        mapped = {e.get("installed_path") for e in sot.get("generated", [])} | {
            e.get("installed_path") for e in sot.get("unmapped", [])
        }
        missing = sorted(set(protected) - mapped)
        if missing:
            print(f"[guard] ERROR: protected paths without provenance: {missing}", file=sys.stderr)
            return 1
        print(f"[guard] manifest OK: {len(protected)} protected paths, provenance complete")
        return 0

    if args.enforce_immutability:
        enforced = enforce_immutability(guard.protected_paths(), dry_run=args.dry_run)
        if enforced:
            guard.ledger.append("immutability_enforced", {"paths": enforced})
            print(f"[guard] locked {len(enforced)} path(s)")
        else:
            print("[guard] all present critical paths already immutable (or absent)")
        return 0

    if args.status:
        print(
            json.dumps(
                {
                    "mode": guard.mode(),
                    "overlay_address": guard.overlay_address(),
                    "apply_enabled": args.apply_enabled,
                    "dry_run": args.dry_run,
                    "euid": os.geteuid(),
                    "protected_paths": len(guard.protected_paths()),
                    "capabilities": guard.authorization_capabilities(),
                },
                indent=2,
            )
        )
        return 0

    mode = guard.mode()
    overlay = guard.overlay_address()
    listen = args.listen or overlay
    if not listen:
        print("[guard] standalone mode: no overlay address configured; refusing to listen", file=sys.stderr)
        print("[guard] (the node still runs; run with --enforce-immutability or --status)", file=sys.stderr)
        return 3

    if mode == "standalone" and not args.bind_any:
        print(f"[guard] standalone mode: overlay {overlay} is not reachable; refusing to listen on it", file=sys.stderr)
        return 3

    if not args.bind_any and listen != overlay:
        print(f"[guard] refusing to bind {listen}: must be the node's overlay address {overlay}", file=sys.stderr)
        return 2

    if args.apply_enabled and os.geteuid() != 0 and not args.dry_run:
        print("[guard] ERROR: --apply-enabled requires euid 0", file=sys.stderr)
        return 2

    print(
        f"[guard] mode={mode} listening on {listen}:{args.port} "
        f"(apply_enabled={args.apply_enabled}, dry_run={args.dry_run})"
    )
    server = ThreadingHTTPServer((listen, args.port), _make_handler(guard, f"sparknet-guard@{listen}"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[guard] shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print("[guard] ERROR: address already in use", file=sys.stderr)
            raise SystemExit(1) from exc
        raise
