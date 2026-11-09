#!/usr/bin/env python3
"""The peer's registry client: verify and install a Master-signed publication.

The live channel is a push: the Master posts a publication to the guard's
``/api/v1/registry`` and the guard installs it (see
``shared/sparknet-registry-install.py``). This client is the *offline* path -
the same acceptance rule, for a node whose guard is not running yet, for a
recovery install, or for an operator who wants to inspect what arrived before it
lands.

It verifies; it never authorises. The capabilities a node holds come from the
registry entry the Master signed, and this client only decides whether the
document that carries it is the Master's and is newer than the last one.

Usage::

    sparknet-registry-client.py --status
    sparknet-registry-client.py --publication <publication.json> [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent
DEFAULT_VAULT_ROOT = SERVICE_DIR.parent
# services/ -> peer-node/ -> fabric root. The shared code is the fabric's, never
# a peer-local copy (two copies of the acceptance rule could drift).
DEFAULT_FABRIC_ROOT = DEFAULT_VAULT_ROOT.parent


def _load_shared(filename: str, modname: str):
    """Load a shared module by path, the way the guard does.

    The `sparknet_loader` module is not imported here: a service imports stdlib
    plus `cryptography` only (peer-node/tests/test_services.py), and the loader
    itself is one more file whose absence must not decide whether a node can
    verify a registry.
    """
    import importlib.util
    import os

    fabric = Path(os.environ.get("SPARKNET_FABRIC_ROOT", "").strip() or DEFAULT_FABRIC_ROOT)
    candidates = [Path(os.environ.get("SPARKNET_SHARED_DIR", "").strip() or fabric / "shared") / filename]
    candidates.append(fabric / "shared" / filename)
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise SystemExit(f"shared module not found: {filename}")
    if modname in sys.modules:
        return sys.modules[modname]
    spec = importlib.util.spec_from_file_location(modname, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


def _vault_root() -> Path:
    import os

    explicit = os.environ.get("SPARKNET_VAULT_ROOT", "").strip()
    return Path(explicit).resolve() if explicit else DEFAULT_VAULT_ROOT.resolve()


def _status(vault: Path, authz, install) -> dict:
    record = install.installation_record(vault)
    registry_path = vault / "manifests" / install.REGISTRY_FILE
    capabilities: list[str] = []
    identity_error = None
    try:
        identity = json.loads((vault / "manifests" / "node-identity.json").read_text())
        registry = json.loads(registry_path.read_text())
        public_key = install.load_pinned_key(vault, authz)
        if public_key is None:
            identity_error = "no pinned Master key"
        else:
            decision = authz.evaluate(identity, registry, public_key)
            capabilities = list(decision.capabilities) if decision.allowed else []
            if not decision.allowed:
                identity_error = decision.reason
    except (OSError, json.JSONDecodeError) as exc:
        identity_error = f"cannot read identity/registry: {exc}"

    return {
        "sequence": record.get("sequence", 0),
        "published_at": record.get("published_at"),
        "registry_sha256": record.get("registry_sha256"),
        "installed_at": record.get("installed_at"),
        "capabilities": capabilities,
        "identity_status": identity_error,
        "registry_path": str(registry_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify and install a signed registry publication (peer side)")
    parser.add_argument("--publication", help="the publication artifact to install")
    parser.add_argument("--status", action="store_true", help="print the installed publication and capabilities")
    parser.add_argument("--dry-run", action="store_true", help="verify the publication and write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--vault-root", help="override the peer vault root (tests)")
    args = parser.parse_args(argv)

    vault = Path(args.vault_root).resolve() if args.vault_root else _vault_root()

    authz = _load_shared("sparknet-authz.py", "sparknet_authz")
    schema = _load_shared("sparknet-registry-schema.py", "sparknet_registry_schema")
    install = _load_shared("sparknet-registry-install.py", "sparknet_registry_install")

    if args.status or not args.publication:
        if not args.status and not args.publication:
            parser.print_help()
        status = _status(vault, authz, install)
        if args.json:
            print(json.dumps(status, indent=2))
        else:
            print(f"installed sequence: {status['sequence']}")
            if status["published_at"]:
                print(f"published at:       {status['published_at']}")
            if status["registry_sha256"]:
                print(f"registry sha256:    {status['registry_sha256']}")
            print(f"capabilities:       {', '.join(status['capabilities']) or 'none'}")
            if status["identity_status"]:
                print(f"identity:           {status['identity_status']}")
        return 0

    publication_path = Path(args.publication)
    try:
        publication = json.loads(publication_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read the publication {publication_path}: {exc}", file=sys.stderr)
        return 2

    public_key = install.load_pinned_key(vault, authz)
    if public_key is None:
        print(
            f"no pinned Master key at {install.pinned_key_path(vault)}; "
            f"refusing to install a registry it cannot verify",
            file=sys.stderr,
        )
        return 1

    try:
        result = install.accept_publication(vault, publication, authz, schema, public_key, dry_run=args.dry_run)
    except install.InstallError as exc:
        print(f"refused [{exc.status} {exc.code}]: {exc.detail}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result.get("status") == "REGISTRY_ALREADY_INSTALLED":
            print(f"already installed: sequence {result['sequence']} (nothing written)")
        else:
            verb = "verified" if args.dry_run else "installed"
            print(
                f"{verb} publication sequence {result['sequence']} "
                f"({result['entries']} entries, sha256 {result['registry_sha256'][:16]}...)"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
