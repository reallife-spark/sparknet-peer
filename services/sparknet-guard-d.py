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
2. an approval addressed to *this* peer (its fingerprint is in the envelope),
3. an unexpired, un-replayed request (nonce + request_id + expiry),
4. a target listed in ``protected_paths`` of the critical manifest,
5. the node's own authorization entry to grant ``guard-apply``,
6. euid == 0,
7. ``--apply-enabled`` on the command line.

Any missing condition is a refusal, never a partial write. Verification and
auditing happen regardless of (6) and (7), so an unarmed daemon still leaves a
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
import stat
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
try:
    from sparknet_loader import load_shared  # noqa: E402 - shared/ is added by path above
except ImportError:  # a role copy without shared/ can still answer --help

    def load_shared(filename, modname, **_kwargs):
        raise FileNotFoundError(f"cannot load {filename}: shared/sparknet_loader.py is not present")


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
#
# The *rule* — which fields, and how they are serialised — is not written here.
# It lives in shared/sparknet-authz.py::guard_approval_message, because the
# Master signs with it and this daemon verifies with it. A second copy of that
# rule is how the two sides drift apart while each keeps working against its own
# assumption; this daemon used to carry exactly such a copy, and the Master used
# to carry a third.

#: Fallback when manifests/security-policies.json is unreadable. Fail to the
#: documented topology rather than to "accept everything".
DEFAULT_MASTER_ANCHOR_IP = "10.100.0.1"

#: A remote arrival alert is evidence, not part of acceptance. Bound the wait so
#: an unreachable Master cannot hold the RFI response open for the pusher default.
INBOX_ALERT_TIMEOUT = 3


def approval_message(payload: dict[str, Any]) -> bytes:
    """Deterministic bytes the Master signs for one approval.

    Delegates to the one implementation in ``shared/sparknet-authz.py``. The
    daemon does not decide what an approval looks like; it decides what to do
    with one.
    """
    authz = _load_authz_module()
    if authz is None:
        raise RuntimeError("shared/sparknet-authz.py not found; the guard cannot canonicalise an approval")
    return authz.guard_approval_message(payload)


#: Refusal detail for each way the operator-OS ceiling can fail. Fixed strings
#: rather than interpolated ones: the reason code is what an operator greps a
#: ledger for, and the detail must not vary with the payload it refused.
_OPERATOR_REFUSALS = {
    "operator_unattested": "the approval attests no operator OS identity, so a privileged apply is refused",
    "operator_insufficient": "the attested operator lacks the privilege this apply needs",
    "operator_malformed": "the approval's operator attestation is not decodable",
    "grant_malformed": "the approval is not an object the ceiling can be read from",
}


class Refusal(Exception):
    """A denied request. Carries the HTTP status and a machine-readable code."""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail


#: Maps an RFI answer refusal reason (contracts/rfi-schema.json `refusals`, produced
#: by shared/sparknet-authz.py::verify_rfi_answer) to the HTTP status the guard
#: sends. `off_overlay` is absent because the source gate in do_POST refuses it
#: before the body is ever read. Attribution failures are 403, a bad artifact is
#: 400, and a duplicate is 409 — the same split the registry route uses.
RFI_REFUSAL_STATUS = {
    "malformed": 400,
    "extra_fields": 400,
    "missing_fields": 400,
    "unsafe_item_name": 400,
    "digest_mismatch": 400,
    "bad_signature": 403,
    "wrong_peer": 403,
    "expired": 403,
    "duplicate_answer": 409,
}


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
                str(key): float(value) for key, value in raw.get("seen", {}).items() if float(value) >= cutoff
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise Refusal(500, "replay_store_corrupt", f"replay store is unreadable: {exc}") from exc

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

    original = path.read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode) or 0o644
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
            os.chmod(tmp.name, mode)
            # Prove the filesystem can lock the staged file before replacing the
            # target. Otherwise a filesystem without chattr support would take
            # the write and only then fail to restore immutability.
            _run(["chattr", "+i", tmp.name], dry_run=False)
            _run(["chattr", "-i", tmp.name], dry_run=False)
            os.replace(tmp.name, path)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp.name)
            raise

        _run(["chown", "root:root", str(path)], dry_run=False)
        _run(["chmod", str(mode), str(path)], dry_run=False)
    except Refusal:
        if had_immutable:
            with contextlib.suppress(Refusal):
                _run(["chattr", "+i", str(path)], dry_run=False)
        raise
    except OSError as exc:
        if had_immutable:
            with contextlib.suppress(Refusal):
                _run(["chattr", "+i", str(path)], dry_run=False)
        raise Refusal(500, "write_failed", str(exc)) from exc

    # The write succeeded, so the path must end locked. A silent failure here
    # would leave a protected file patched but writable while the guard reports
    # success — the one outcome the immutable bit exists to prevent. Verify with
    # lsattr and refuse if the lock is not actually back.
    try:
        _run(["chattr", "+i", str(path)], dry_run=False)
    except Refusal as exc:
        if _is_immutable(path) is not True:
            # Never leave a partial write. Restore the exact prior bytes and mode
            # (still unlocked, because the filesystem refused the lock) and report
            # the failed transaction.
            restore = tempfile.NamedTemporaryFile(
                "wb", dir=path.parent, prefix=f".{path.name}.", suffix=".restore", delete=False
            )
            try:
                with restore:
                    restore.write(original)
                    restore.flush()
                    os.fsync(restore.fileno())
                os.chmod(restore.name, mode)
                os.replace(restore.name, path)
                _run(["chown", "root:root", str(path)], dry_run=False)
            except OSError:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(restore.name)
            raise Refusal(
                500,
                "immutability_not_restored",
                f"patched {path} but could not re-lock it: {exc.detail or exc.code}. "
                f"The file was restored and is no longer immutable.",
            ) from exc

    print(f"[guard] patched and re-locked {path} (was_immutable={had_immutable})")


def _is_immutable(target: Path) -> bool | None:
    """Read the immutable flag with lsattr.

    Returns True/False for the flag, or None when the flag cannot be *read*
    (lsattr missing, or the filesystem does not support the attribute). The
    three-way answer matters: "not immutable" and "cannot tell" must not
    collapse into the same branch, or a filesystem with no chattr support is
    silently treated as protected.
    """
    try:
        flags = subprocess.run(["lsattr", "-d", str(target)], capture_output=True, text=True, check=False)
    except OSError:
        return None
    if flags.returncode != 0:
        return None
    head = (flags.stdout.split(" ", 1)[0] if flags.stdout else "").strip()
    return "i" in head


def enforce_immutability(paths: list[str], *, dry_run: bool = False) -> list[str]:
    """Lock critical paths that are not already immutable. Standalone mode uses this.

    Fail-closed: a path that is neither already immutable nor newly locked is an
    error the caller must see, never a silent skip. The failure it guards against
    is a filesystem where ``chattr`` cannot protect anything — reporting "enforced"
    there would leave the node's protected paths writable while every surface says
    they are locked.
    """
    enforced: list[str] = []
    for raw in paths:
        target = Path(raw)
        if not target.exists():
            continue
        already = _is_immutable(target)
        if already:
            continue
        try:
            _run(["chattr", "+i", str(target)], dry_run=dry_run)
        except Refusal as exc:
            # Confirm with lsattr that the file really is not immutable now. If it
            # is (e.g. it already was, and lsattr could not read the flag), this is
            # not a failure; otherwise chattr could not protect the path and we
            # must refuse rather than claim success.
            if dry_run or _is_immutable(target) is True:
                continue
            raise Refusal(
                500,
                "immutability_not_enforced",
                f"cannot set the immutable bit on {target}: {exc.detail or exc.code} "
                f"(the filesystem may not support chattr +i)",
            ) from exc
        enforced.append(raw)
    return enforced


def _load_shared_module(filename: str, modname: str):
    """Load a shared file through the one loader; optional modules return None."""
    return load_shared(filename, modname, missing_ok=True)


def _load_authz_module():
    """The shared sparknet-authz.py: verification and signing, one copy."""
    return _load_shared_module("sparknet-authz.py", "sparknet_authz")


def _load_registry_schema_module():
    """The shared registry checker: the shape a published registry must have.

    The same module the Master runs before publishing, loaded by path. A peer
    that installed a document this checker refuses would be installing a shape
    no other node agrees with.
    """
    return _load_shared_module("sparknet-registry-schema.py", "sparknet_registry_schema")


def _load_registry_install_module():
    """The shared acceptance rule: what makes a publication installable.

    Loaded by path, like every other shared module, so the guard and the offline
    registry client cannot disagree about it.
    """
    return _load_shared_module("sparknet-registry-install.py", "sparknet_registry_install")


def _load_rfi_install_module():
    """The shared RFI acceptance rule: what makes an answer installable.

    Loaded by path so the guard's ``/api/v1/rfi`` route and any operator-carried
    offline install accept and refuse exactly the same things — the same reason the
    registry rule lives in ``shared/`` rather than once per surface.
    """
    return _load_shared_module("sparknet-rfi-install.py", "sparknet_rfi_install")


def _load_provenance_module():
    """The shared provenance tool: it builds and appends the ``inbox_acceptance``
    block an installed RFI answer records. Loaded by path, one copy."""
    return _load_shared_module("sparknet-provenance.py", "sparknet_provenance")


def _load_notify_module():
    """The shared node-local notifier: it tells the human an item arrived.

    Loaded by path so the peer's inbox-arrival ping and the Master console's use
    the one implementation (``shared/sparknet-notify.py``). Optional: a node
    without it still installs — notification is a courtesy, never a gate.
    """
    return _load_shared_module("sparknet-notify.py", "sparknet_notify")


def _load_telemetry_pusher():
    """The peer's 70A pusher, loaded through the one loader from this role's services dir.

    ``inbox_alert`` rides the same envelope and endpoint as status/metrics, so the
    guard reuses the one pusher instead of building a second report by hand. It is
    optional: a guard without it still installs and still leaves the local spool.
    """
    return load_shared("sparknet-telemetry-push.py", "sparknet_telemetry_pusher", dirs=[SERVICE_DIR], missing_ok=True)


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


def load_master_pubkey(vault_root: Path) -> Ed25519PublicKey:
    """Load the Master pin from a non-writable, non-symlink location.

    A pin stored in the peer vault is writable by the peer's own user. Anyone
    who can write it can substitute their own key and forge approvals or
    registry publications. The loader prefers an operator-provided root-owned
    path, then the system path, and only falls back to the vault for a
    development checkout. Group/other write bits and symlinks are refused.
    """
    import base64

    candidates = []
    configured = os.environ.get("SPARKNET_MASTER_PUBKEY", "").strip()
    if configured and os.geteuid() != 0:
        candidates.append(Path(configured))
    candidates.append(Path("/etc/sparknet/keys/master-ed25519.pub.b64"))
    candidates.append(vault_root / "manifests" / "keys" / "master-ed25519.pub.b64")
    for path in candidates:
        if not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError(f"master pin {path} is a symlink")
        if path.stat().st_mode & 0o022:
            raise ValueError(f"master pin {path} is group/other writable")
        raw = base64.b64decode(path.read_text().strip(), validate=True)
        if len(raw) != 32:
            raise ValueError(f"master pin {path} is not a 32-byte Ed25519 key")
        return Ed25519PublicKey.from_public_bytes(raw)
    raise FileNotFoundError("no master pin at SPARKNET_MASTER_PUBKEY, /etc/sparknet/keys, or the vault")


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
        replay_path = (
            Path("/var/lib/sparknet/guard-replay.json")
            if os.geteuid() == 0 and Path("/var/lib/sparknet").is_dir()
            else vault_root / ".state" / "guard-replay.json"
        )
        self.replay = replay or ReplayGuard(store_path=replay_path)
        self.master_pubkey = master_pubkey
        configured_audit = os.environ.get("SPARKNET_GUARD_AUDIT", "").strip()
        if configured_audit:
            audit_path = Path(configured_audit)
        elif os.geteuid() == 0 and Path("/var/lib/sparknet").is_dir():
            audit_path = Path("/var/lib/sparknet/guard-audit.json")
        else:
            audit_path = vault_root / "blockchain" / "guard-audit.json"
        self.ledger = ledger or AuditLedger(audit_path)
        # One apply at a time: ThreadingHTTPServer hands each request its own
        # thread, and verification, replay bookkeeping, ledger appends and the
        # privileged write must never interleave.
        self._apply_lock = threading.Lock()

    # -- configuration ------------------------------------------------------

    def manifest(self) -> dict[str, Any]:
        path = self.vault_root / "manifests" / "critical-manifest.json"
        if os.geteuid() == 0 and Path("/etc/sparknet/critical-manifest.json").is_file():
            path = Path("/etc/sparknet/critical-manifest.json")
        return json.loads(path.read_text())

    def protected_paths(self) -> list[str]:
        return list(self.manifest().get("protected_paths", []))

    def policies(self) -> dict[str, Any]:
        path = self.vault_root / "manifests" / "security-policies.json"
        if os.geteuid() == 0 and Path("/etc/sparknet/security-policies.json").is_file():
            path = Path("/etc/sparknet/security-policies.json")
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
        if not (identity_path.is_file() and registry_path.is_file()):
            return []
        try:
            identity = json.loads(identity_path.read_text())
            registry = json.loads(registry_path.read_text())
            pubkey = load_master_pubkey(self.vault_root)
        except Exception:
            return []
        decision = sparknet_authz.evaluate(identity, registry, pubkey)
        return decision.capabilities if decision.allowed else []

    def own_fingerprint(self) -> str | None:
        """This node's own fingerprint, from its own identity manifest.

        An approval names the peer it is addressed to. Without this, a valid
        signature would say only *that* the Master signed something — not that
        the Master meant *this* node, and an approval minted for one peer could
        be applied by another that protects the same path.

        None means the node cannot say who it is, and the daemon refuses
        approvals rather than accepting one it cannot attribute.
        """
        sparknet_authz = _load_authz_module()
        if sparknet_authz is None:
            return None
        identity_path = self.vault_root / "manifests" / "node-identity.json"
        try:
            identity = json.loads(identity_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return sparknet_authz.fingerprint(identity)
        except Exception:
            return None

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

    # -- registry intake ----------------------------------------------------

    def installed_sequence(self) -> int:
        """The highest publication sequence this node has installed, 0 if none."""
        install = _load_registry_install_module()
        if install is None:
            return 0
        return install.installed_sequence(self.vault_root)

    def handle_registry(self, artifact: Any) -> dict[str, Any]:
        """Install one signed registry publication, or refuse it.

        This is the peer's half of the distribution channel. It is deliberately
        *not* gated on the peer's own ``guard-apply`` capability: the node being
        revoked must be able to receive the registry that revokes it, and a
        revocation the revoked node cannot hear is not a revocation.

        The acceptance rule itself lives once in
        ``shared/sparknet-registry-install.py``, because the offline registry
        client installs publications too.
        """
        with self._apply_lock:
            return self._handle_registry_locked(artifact)

    def _handle_registry_locked(self, artifact: Any) -> dict[str, Any]:
        authz = _load_authz_module()
        schema = _load_registry_schema_module()
        install = _load_registry_install_module()
        if self.master_pubkey is None:
            raise Refusal(503, "no_master_key", "guard has no Master public key loaded")
        if authz is None or schema is None or install is None:
            raise Refusal(503, "no_contract", "shared registry code is not available")
        try:
            result = install.accept_publication(
                self.vault_root,
                artifact,
                authz,
                schema,
                self.master_pubkey,
                dry_run=self.dry_run,
            )
        except install.InstallError as exc:
            sequence = artifact.get("sequence") if isinstance(artifact, dict) else None
            self.ledger.append("registry_refused", {"code": exc.code, "sequence": sequence, "detail": exc.detail[:500]})
            raise Refusal(exc.status, exc.code, exc.detail) from exc

        if self.dry_run:
            event = "registry_verified_dry_run"
        elif result.get("status") == "REGISTRY_ALREADY_INSTALLED":
            event = "registry_unchanged"
        else:
            event = "registry_installed"
        block = self.ledger.append(
            event,
            {
                "sequence": result["sequence"],
                "registry_sha256": result["registry_sha256"],
                "entries": result["entries"],
            },
        )
        return {**result, "audit_block": block["hash"]}

    # -- RFI answer intake --------------------------------------------------

    def installed_request_ids(self) -> list[str]:
        """The request_ids this node has already installed an answer for, so a
        re-delivery is a duplicate rather than a second copy of the same thing."""
        install = _load_rfi_install_module()
        if install is None:
            return []
        return install.installed_request_ids(self.vault_root)

    def handle_rfi(self, artifact: Any) -> dict[str, Any]:
        """Install one signed RFI answer into 01_Inbox, or refuse it.

        This is the peer's half of the intake channel (contracts/rfi-schema.json):
        the Master answers a request-for-information by pushing a digest-bound,
        signed answer to POST /api/v1/rfi on this port. Like the registry route it
        is *not* gated on the peer's own capability — information the Master
        addressed to this node must reach it — and the acceptance rule lives once
        in shared/sparknet-rfi-install.py, so this live route and an operator
        carrying an artifact to a node with no daemon cannot disagree about what a
        peer trusts.
        """
        with self._apply_lock:
            return self._handle_rfi_locked(artifact)

    def _handle_rfi_locked(self, artifact: Any) -> dict[str, Any]:
        authz = _load_authz_module()
        install = _load_rfi_install_module()
        provenance = _load_provenance_module()
        if self.master_pubkey is None:
            raise Refusal(503, "no_master_key", "guard has no Master public key loaded")
        if authz is None or install is None or provenance is None:
            raise Refusal(503, "no_contract", "shared rfi code is not available")

        mine = self.own_fingerprint()
        if mine is None:
            # An answer is bound to the peer it was prepared for; a node that
            # cannot say who it is cannot check that binding, so it refuses rather
            # than installing material it cannot attribute to itself.
            raise Refusal(503, "no_identity", "guard cannot determine this node's fingerprint")

        if self.dry_run:
            # Verify only, never write — the contract the registry route honours
            # too. No user record is needed because nothing is recorded.
            ok, reason, _content = authz.verify_rfi_answer(
                artifact,
                master_public_key=self.master_pubkey,
                peer_fingerprint=mine,
                installed_request_ids=self.installed_request_ids(),
            )
            if not ok:
                self._refuse_rfi(reason, artifact)
            return {
                "status": "RFI_VERIFIED_DRY_RUN",
                "request_id": str(artifact.get("request_id")),
                "content_sha256": str(artifact.get("content_sha256")),
                "item": f"01_Inbox/{artifact.get('item_name')}",
            }

        # Installing appends a dual-signed inbox_acceptance block, which needs a
        # filled user record and this node's keys. Check before writing anything:
        # accept_answer writes the item first, so a vault that cannot record the
        # acceptance would leave an item with no trail and a re-delivery refused
        # as a duplicate. Failing here, before the write, keeps the inbox honest.
        try:
            record = provenance.parse_user_record(self.vault_root)
            if not provenance.user_is_filled(record):
                raise provenance.ProvenanceError("user record is unfilled")
        except Exception:
            raise Refusal(503, "no_user_record", "this vault has no filled user record; run `make user-init`") from None

        result = install.accept_answer(
            self.vault_root,
            artifact,
            authz=authz,
            provenance=provenance,
            master_public_key=self.master_pubkey,
            peer_fingerprint=mine,
            user_record=record,
        )
        if result["status"] == "refused":
            self._refuse_rfi(result["reason"], artifact)

        block = self.ledger.append(
            "rfi_installed",
            {
                "request_id": result["request_id"],
                "content_sha256": result["content_sha256"],
                "item": result["item"],
                "acceptance_id": result.get("acceptance_id"),
            },
        )

        # Tell the human an item landed (fabric Step 67B): acceptance is a human
        # act, so an unnoticed item is an unaccepted one. Best-effort and *after*
        # the install and the ledger append, so a notification failure can never
        # undo or block a delivery that already succeeded. The durable record is
        # the inbox_acceptance block above; this is the alert that reaches a person.
        notified = False
        notify = _load_notify_module()
        if notify is not None:
            outcome = notify.notify_inbox_arrival(
                self.vault_root,
                item=result["item"],
                source="master-node",
                request_id=result["request_id"],
            )
            notified = bool(outcome.get("notified"))

        # 70F: the same arrival is evidence the Master can see. The local ping
        # above reaches whoever is at this node; a headless peer has nobody, so
        # the alert rides telemetry to the Master's admin surface. It is unsigned
        # and overlay-attributed like every report, and it cannot affect the
        # install: a failed push returns False and the item stays installed.
        alerted = self._emit_inbox_alert(result, block)
        return {**result, "audit_block": block["hash"], "notified": notified, "alerted": alerted}

    def _emit_inbox_alert(self, result: dict[str, Any], block: dict[str, Any]) -> bool:
        """Push an installed arrival as ``kind=inbox_alert``; never a gate.

        Gated on the peer's own ``telemetry`` capability, exactly as 70A. The
        payload carries the inbox item, its source, request id, content digest and
        the ``rfi_installed`` audit block hash — enough to correlate the alert
        with the ledger, no content and no authority. Overlay attribution, the
        freshness nonce and the Master's capability gate are the envelope's, not
        this method's; nothing here signs, grants or edits the registry.
        """
        pusher = _load_telemetry_pusher()
        if pusher is None:
            return False
        try:
            capabilities, identity, refusal = pusher.granted_capabilities(self.vault_root)
            if identity is None or refusal or "telemetry" not in capabilities:
                return False
            authz = _load_authz_module()
            if authz is None:
                return False
            endpoint = pusher.resolve_endpoint(self.vault_root)
            if not endpoint.startswith(("http://", "https://")):
                return False
            report = authz.build_telemetry_report(
                kind="inbox_alert",
                peer_fingerprint=authz.fingerprint(identity),
                payload={
                    "item": result.get("item"),
                    "source": "master-node",
                    "request_id": result.get("request_id"),
                    "content_sha256": result.get("content_sha256"),
                    "audit_block": block.get("hash"),
                },
            )
            ok, detail = pusher.post_report(endpoint, report, INBOX_ALERT_TIMEOUT)
            if not ok:
                print(f"[guard] inbox_alert push failed: {detail}", file=sys.stderr)
            return ok
        except Exception as exc:  # a courtesy must not break an install
            print(f"[guard] inbox_alert skipped: {exc}", file=sys.stderr)
            return False

    def _refuse_rfi(self, reason: str, artifact: Any) -> NoReturn:
        """Record an RFI refusal in the guard ledger, then raise it as HTTP.

        The reason is a fixed string from the contract's vocabulary, so it maps to
        a stable status and an operator can grep the ledger for it. The detail
        never echoes the artifact: a refused answer is untrusted input.
        """
        request_id = artifact.get("request_id") if isinstance(artifact, dict) else None
        self.ledger.append("rfi_refused", {"code": reason, "request_id": request_id})
        status = RFI_REFUSAL_STATUS.get(reason, 400)
        raise Refusal(status, reason, f"the answer was refused: {reason}")

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

        # The canonical bytes are a function of the parsed fields, so the payload
        # is parsed before the signature is checked. Parsing is not a trust
        # decision: no value below is read before the signature over the
        # canonical projection has verified.
        #
        # A guard with no Master key cannot verify anything, so it says so
        # before it reads anything: 503 is the honest answer to any request,
        # and it costs nothing to compute.
        if self.master_pubkey is None:
            raise Refusal(503, "no_master_key", "guard has no Master public key loaded")

        try:
            payload = json.loads(message.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Refusal(400, "malformed_payload", str(exc)) from exc
        if not isinstance(payload, dict):
            raise Refusal(400, "malformed_payload", "approval payload must be an object")

        # The one canonical rule, shared with the signer. A payload with a
        # missing or an extra field is refused here rather than half-read: the
        # signature covers exactly the canonical projection, so an extra field
        # would be unsigned authority-shaped data.
        try:
            canonical = approval_message(payload)
        except Exception as exc:
            raise Refusal(400, "malformed_approval", str(exc)) from exc

        # 1. Master signature, before anything else is trusted.
        try:
            self.master_pubkey.verify(signature, canonical)
        except InvalidSignature as exc:
            self.ledger.append("patch_refused", {"code": "bad_signature"})
            raise Refusal(403, "bad_signature", "Ed25519 verification failed") from exc

        # 2. The approval must be addressed to *this* peer. A signature says
        #    nothing about who an approval is for unless the payload says so,
        #    and without that an approval minted for one peer could be applied
        #    by another that protects the same path.
        mine = self.own_fingerprint()
        if mine is None:
            raise Refusal(503, "no_identity", "this node has no usable identity manifest")
        if payload.get("peer_fingerprint") != mine:
            self.ledger.append("patch_refused", {"code": "wrong_peer", "claimed": payload.get("peer_fingerprint")})
            raise Refusal(403, "wrong_peer", "the approval names another peer")

        # 3. Replay / expiry.
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

        # 4. Content integrity.
        content_b64 = payload.get("content_b64", "")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except Exception as exc:
            raise Refusal(400, "malformed_content", str(exc)) from exc
        actual = hashlib.sha256(content).hexdigest()
        if actual != payload.get("content_sha256"):
            self.ledger.append("patch_refused", {"code": "content_hash_mismatch"})
            raise Refusal(400, "content_hash_mismatch", "content does not match content_sha256")

        # 5. Target must be a registered critical path.
        target_path = payload.get("file_path")
        if not isinstance(target_path, str):
            raise Refusal(400, "missing_file_path", "file_path required")
        protected = self.protected_paths()
        if target_path not in protected:
            self.ledger.append("patch_refused", {"code": "not_protected", "file_path": target_path})
            raise Refusal(403, "not_protected", f"{target_path} is not in protected_paths")

        # 6. This node must be authorized to apply at all.
        capabilities = self.authorization_capabilities()
        if "guard-apply" not in capabilities:
            self.ledger.append(
                "patch_refused", {"code": "capability_missing", "file_path": target_path, "granted": capabilities}
            )
            raise Refusal(403, "capability_missing", f"guard-apply not granted (have: {capabilities or 'none'})")

        # 7. Dry-run short-circuits after verification: the whole point of
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

        # 8. Arming, then the operator's attested ceiling, then root.
        if not self.apply_enabled:
            self.ledger.append(
                "patch_verified_not_applied", {"code": "unarmed", "file_path": target_path, "sha256": actual}
            )
            raise Refusal(403, "unarmed", "guard is not running with --apply-enabled")

        # The human behind this approval, not the process serving it. The
        # attestation rides inside the signature, so it cannot be widened in
        # transit, and an absent one is *unattested* — which fails closed rather
        # than being read as permission. This complements the euid check below and
        # does not replace it: the attestation says whether the operator may ask,
        # the uid says whether this process can do, and a privileged apply needs
        # both. One rule, in shared/sparknet-authz.py, so the guard cannot drift
        # from the ceiling a grant-presenting path applies.
        ceiling_authz = _load_authz_module()
        if ceiling_authz is None:
            raise Refusal(
                503,
                "no_authz",
                "shared/sparknet-authz.py not found; the guard cannot check the operator ceiling",
            )
        operator_allowed, operator_reason = ceiling_authz.guard_approval_operator_allows(payload, requires_root=True)
        if not operator_allowed:
            self.ledger.append(
                "patch_verified_not_applied",
                {"code": operator_reason, "file_path": target_path, "sha256": actual},
            )
            raise Refusal(403, operator_reason, _OPERATOR_REFUSALS[operator_reason])

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


def build_listener(guard: Guard, server_name: str, *, bind: str, port: int, handler_class: Any = None) -> Any:
    """Build the guard's route-table listener; the caller owns start/stop."""
    http = _load_http_module()

    def permitted() -> set[str]:
        return guard.permitted_sources()

    def status(request: Any = None) -> tuple[int, dict[str, Any]]:
        return 200, {
            "service": server_name,
            "mode": guard.mode(),
            "apply_enabled": guard.apply_enabled,
            "dry_run": guard.dry_run,
            "protected_paths": len(guard.protected_paths()),
            "capabilities": guard.authorization_capabilities(),
            "registry_sequence": guard.installed_sequence(),
        }

    def post(handler: Any) -> Any:
        def route(request: Any) -> tuple[int, dict[str, Any]]:
            packet = request.json
            if not isinstance(packet, dict):
                raise Refusal(400, "malformed_packet", "the packet must be a JSON object")
            try:
                return 200, handler(packet)
            except Refusal as refusal:
                return refusal.status, {"error": refusal.detail or refusal.code, "code": refusal.code}
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[guard] internal error on {request.path}: {exc!r}", file=sys.stderr)
                return 500, {"error": "internal error", "code": "internal_error"}

        return route

    def refusal(exc: BaseException) -> tuple[int, dict[str, Any]]:
        if isinstance(exc, http.RequestError):
            if exc.reason in {"source-refused", "not-overlay", "not-loopback"}:
                return exc.status, {"error": "off-overlay", "code": "off_overlay"}
            if exc.reason == "malformed":
                return exc.status, {"error": exc.detail or "malformed packet", "code": "malformed_packet"}
            return exc.status, {"error": exc.detail or exc.reason, "code": exc.reason}
        return 500, {"error": "internal error", "code": "internal_error"}

    return http.listen(
        [
            http.Route(
                "/api/v1/status",
                status,
                method="GET",
                allowed_sources=permitted,
                parse_json=False,
                max_body=0,
            ),
            http.Route(
                "/api/v1/critical-file/patch",
                post(guard.handle_patch),
                method="POST",
                allowed_sources=permitted,
                max_body=8 * 1024 * 1024,
            ),
            http.Route(
                "/api/v1/registry",
                post(guard.handle_registry),
                method="POST",
                allowed_sources=permitted,
                max_body=8 * 1024 * 1024,
            ),
            http.Route(
                "/api/v1/rfi",
                post(guard.handle_rfi),
                method="POST",
                allowed_sources=permitted,
                max_body=8 * 1024 * 1024,
            ),
        ],
        name="guard",
        bind=bind,
        port=port,
        refusal=refusal,
        handler_class=handler_class,
    )


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

    try:
        master_pubkey = load_master_pubkey(vault_root)
    except (OSError, ValueError, TypeError) as exc:
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
    listener = build_listener(guard, f"sparknet-guard@{listen}", bind=listen, port=args.port)
    try:
        listener.wait()
    except KeyboardInterrupt:
        print("[guard] shutting down")
        listener.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print("[guard] ERROR: address already in use", file=sys.stderr)
            raise SystemExit(1) from exc
        raise
