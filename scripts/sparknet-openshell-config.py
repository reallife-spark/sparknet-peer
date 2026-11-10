#!/usr/bin/env python3
"""Render the peer's OpenShell gateway config from the owner and the pin manifest.

The VM gateway template carries the owner's home, UID and GID; committing a
literal home path makes the template host-specific and lets the gateway's
``sandbox_uid`` drift from the engine's resolved owner. This renderer is the
single place that binds the two, and it writes an ``owner.json`` the engine
reads before it creates a sandbox.

Usage:
    sparknet-openshell-config.py --owner raxxar
    sudo sparknet-openshell-config.py --owner raxxar --out-dir /home/raxxar/.config/openshell

`--help` writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "manifests" / "openshell.json"
DEFAULT_TEMPLATE = ROOT / "configs" / "openshell" / "gateway-vm.toml.in"
GATEWAY_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
PLACEHOLDER_RE = re.compile(r"@[A-Z_]+@")


def _resolve_owner(args: argparse.Namespace) -> tuple[int, int, Path]:
    if args.uid is not None or args.gid is not None:
        if args.uid is None or args.gid is None:
            raise SystemExit("--uid and --gid must be supplied together")
        uid, gid = int(args.uid), int(args.gid)
        home = Path(args.home).expanduser() if args.home else Path.home()
    elif args.owner:
        try:
            record = pwd.getpwnam(args.owner)
        except KeyError as exc:
            raise SystemExit(f"owner {args.owner!r} does not exist on this host") from exc
        uid, gid = record.pw_uid, record.pw_gid
        home = Path(args.home).expanduser() if args.home else Path(record.pw_dir)
    else:
        uid, gid = os.getuid(), os.getgid()
        home = Path(args.home).expanduser() if args.home else Path.home()
    if uid == 0:
        raise SystemExit("the sandbox owner must not be uid 0")
    if not home.is_absolute():
        raise SystemExit(f"owner home must be absolute: {home}")
    return uid, gid, home


def _write(path: Path, text: str, mode: int, uid: int, gid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    if os.geteuid() == 0:
        os.chown(path, uid, gid, follow_symlinks=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the peer's OpenShell VM gateway config from the owner and manifest."
    )
    parser.add_argument("--owner", default=os.environ.get("SPARKNET_OWNER_USER", ""))
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    parser.add_argument("--home", help="owner home directory (default: passwd entry)")
    parser.add_argument("--gateway-name", default="sparknet-peer")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    parser.add_argument("--out-dir", help="default: <owner home>/.config/openshell")
    args = parser.parse_args(argv)

    if not GATEWAY_NAME_RE.match(args.gateway_name):
        raise SystemExit(f"invalid gateway name: {args.gateway_name!r}")
    uid, gid, home = _resolve_owner(args)
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    try:
        vm = manifest["vm"]
        images = manifest["images"]
    except KeyError as exc:
        raise SystemExit(f"manifest is missing {exc.args[0]!r}") from exc

    template = Path(args.template).read_text(encoding="utf-8")
    rendered = (
        template.replace("@OWNER_HOME@", str(home))
        .replace("@OWNER_UID@", str(uid))
        .replace("@OWNER_GID@", str(gid))
        .replace("@GATEWAY_NAME@", args.gateway_name)
        .replace("@BOOTSTRAP_IMAGE@", str(vm["bootstrap_image"]))
        .replace("@DEFAULT_IMAGE@", str(vm["default_image"]))
    )
    left = PLACEHOLDER_RE.search(rendered)
    if left:
        raise SystemExit(f"unrendered template placeholder: {left.group(0)}")

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else home / ".config" / "openshell"
    gateway = out_dir / "gateway.toml"
    owner_json = out_dir / "owner.json"
    gateway_env = out_dir / "gateway.env"
    owner = {
        "schema_version": "1.0",
        "gateway": "openshell",
        "gateway_name": args.gateway_name,
        "driver": "vm",
        "uid": uid,
        "gid": gid,
        "home": str(home),
        "rendered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    env_lines = [
        f"OPENSHELL_SANDBOX_RUNTIME_IMAGE={images['sandbox']}",
        f"OPENSHELL_SUPERVISOR_IMAGE={images['supervisor']}",
        "",
    ]
    _write(gateway, rendered, 0o600, uid, gid)
    _write(owner_json, json.dumps(owner, indent=2, sort_keys=True) + "\n", 0o644, uid, gid)
    _write(gateway_env, "\n".join(env_lines), 0o600, uid, gid)
    print(f"wrote {gateway}")
    print(f"wrote {owner_json}")
    print(f"wrote {gateway_env}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:  # noqa: BLE001 - a config renderer refuses plainly
        print(f"sparknet-openshell-config: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
