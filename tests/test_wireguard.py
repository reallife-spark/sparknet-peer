"""WireGuard Master Anchor: template, rendering, and key hygiene.

IMPORTANT SCOPE NOTE
--------------------
`wg`, `wg-quick` and the wireguard kernel module are absent on this host and
the session has no CAP_NET_ADMIN, so these tests verify *generation and
validation* only. A live handshake, PersistentKeepalive behaviour and
`chattr +i` enforcement are UNVERIFIED HERE and are listed in
docs/wireguard.md as target-node checklist items. They are deliberately not
claimed to work.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

VAULT_ROOT = Path(__file__).resolve().parent.parent
# The parent holds the shared contract and the shared code both roles execute.
# Key hygiene has to cover it too: a private key in shared/ would be a leak in
# every role at once.
FABRIC_ROOT = VAULT_ROOT.parent
SHARED_DIR = FABRIC_ROOT / "shared"
TEMPLATE = VAULT_ROOT / "configs" / "wireguard" / "wg0-node.conf.template"
RENDER_SCRIPT = VAULT_ROOT / "scripts" / "sparknet-wireguard-render.sh"
KEYGEN = VAULT_ROOT / "scripts" / "sparknet-keygen.sh"

WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{43}=$")

# A syntactically valid but meaningless 32-byte key. Never a real secret.
FAKE_PRIVATE_KEY = base64.b64encode(bytes(range(32))).decode()
FAKE_MASTER_PUBKEY = base64.b64encode(bytes(range(32, 64))).decode()

RENDER_ENV = {
    "SPARKNET_WG_NODE_PRIVATE_KEY": FAKE_PRIVATE_KEY,
    "SPARKNET_WG_MASTER_PUBLIC_KEY": FAKE_MASTER_PUBKEY,
    "SPARKNET_WG_NODE_ADDRESS": "10.100.0.2/32",
    "SPARKNET_WG_MASTER_ENDPOINT": "master.example:51820",
}


def _render(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(RENDER_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **RENDER_ENV, **(env or {})},
    )


# --- template ---------------------------------------------------------------


def test_template_exists() -> None:
    assert TEMPLATE.is_file()


def test_template_has_no_literal_key() -> None:
    """The tracked template must never carry real key material."""
    text = TEMPLATE.read_text()
    assert FAKE_PRIVATE_KEY not in text
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("PrivateKey"):
            assert "${" in stripped, f"template PrivateKey must stay a placeholder: {stripped}"


def test_template_targets_only_the_master_by_default() -> None:
    text = TEMPLATE.read_text()
    assert "AllowedIPs          = 10.100.0.1/32" in text
    assert "PersistentKeepalive" in text, "NAT traversal needs keepalive to hold the anchor up"
    peer_sections = re.findall(r"^\[Peer\]", text, re.MULTILINE)
    # Only the one real [Peer]; the rest are commented examples.
    assert len(peer_sections) == 1


def test_template_documents_every_placeholder() -> None:
    declared = set(re.findall(r"\$\{([A-Z_]+)\}", TEMPLATE.read_text()))
    assert declared == {
        "NODE_PRIVATE_KEY",
        "NODE_ADDRESS",
        "NODE_LISTEN_PORT",
        "MASTER_PUBLIC_KEY",
        "MASTER_ENDPOINT",
    }


# --- rendering --------------------------------------------------------------


def test_render_fails_closed_without_input() -> None:
    proc = _render([], env={k: "" for k in RENDER_ENV})
    assert proc.returncode == 2
    assert "Missing required input" in proc.stdout + proc.stderr


def test_render_rejects_non_overlay_address() -> None:
    proc = _render(["--apply"], env={"SPARKNET_WG_NODE_ADDRESS": "192.168.1.5/24"})
    assert proc.returncode != 0
    assert "10.100.0" in proc.stdout + proc.stderr


def test_render_rejects_malformed_private_key() -> None:
    proc = _render(["--apply"], env={"SPARKNET_WG_NODE_PRIVATE_KEY": "not-a-key"})
    assert proc.returncode != 0
    assert "32-byte base64" in proc.stdout + proc.stderr


def test_render_rejects_malformed_master_key() -> None:
    proc = _render(["--apply"], env={"SPARKNET_WG_MASTER_PUBLIC_KEY": "short"})
    assert proc.returncode != 0
    assert "32-byte base64" in proc.stdout + proc.stderr


def test_render_is_dry_run_by_default(tmp_path: Path) -> None:
    render_dir = tmp_path / "render"
    proc = _render([], env={"SPARKNET_WG_RENDER_DIR": str(render_dir)})
    assert proc.returncode == 0
    assert "DRY-RUN" in proc.stdout
    assert not (render_dir / "wg0.conf").exists(), "dry-run must not write"
    assert not render_dir.exists(), "dry-run must not even create the directory"


def test_dry_run_redacts_the_private_key(tmp_path: Path) -> None:
    proc = _render([], env={"SPARKNET_WG_RENDER_DIR": str(tmp_path / "r")})
    assert FAKE_PRIVATE_KEY not in proc.stdout + proc.stderr, "dry-run leaked the private key to stdout"


def test_render_apply_produces_wg0_at_0600(tmp_path: Path) -> None:
    render_dir = tmp_path / "render"
    proc = _render(["--apply"], env={"SPARKNET_WG_RENDER_DIR": str(render_dir)})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rendered = render_dir / "wg0.conf"
    assert rendered.is_file()
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600


def test_rendered_config_is_complete_and_parseable(tmp_path: Path) -> None:
    render_dir = tmp_path / "render"
    proc = _render(["--apply"], env={"SPARKNET_WG_RENDER_DIR": str(render_dir)})
    assert proc.returncode == 0
    text = (render_dir / "wg0.conf").read_text()

    # No placeholder survived — the render fails closed, but assert it anyway.
    assert not re.search(r"\$\{[A-Z_]+\}", text), "unresolved placeholder in rendered config"

    assert "[Interface]" in text and "[Peer]" in text
    assert "Address    = 10.100.0.2/32" in text
    assert "ListenPort = 51820" in text
    assert f"PrivateKey = {FAKE_PRIVATE_KEY}" in text
    assert f"PublicKey           = {FAKE_MASTER_PUBKEY}" in text
    assert "AllowedIPs          = 10.100.0.1/32" in text
    assert "Endpoint            = master.example:51820" in text
    assert "PersistentKeepalive = 25" in text
    assert "MTU = 1400" in text


def test_rendered_peer_is_master_only(tmp_path: Path) -> None:
    """A fresh node must not be able to reach siblings before the Master says so."""
    render_dir = tmp_path / "render"
    _render(["--apply"], env={"SPARKNET_WG_RENDER_DIR": str(render_dir)})
    text = (render_dir / "wg0.conf").read_text()
    peer_lines = [ln for ln in text.splitlines() if ln.strip().startswith("AllowedIPs")]
    assert peer_lines == ["AllowedIPs          = 10.100.0.1/32"]


def test_render_is_idempotent(tmp_path: Path) -> None:
    render_dir = tmp_path / "render"
    first = _render(["--apply"], env={"SPARKNET_WG_RENDER_DIR": str(render_dir)})
    body_one = (render_dir / "wg0.conf").read_text()
    second = _render(["--apply"], env={"SPARKNET_WG_RENDER_DIR": str(render_dir)})
    body_two = (render_dir / "wg0.conf").read_text()
    assert first.returncode == second.returncode == 0
    assert body_one == body_two


def test_render_leaves_no_temp_files(tmp_path: Path) -> None:
    render_dir = tmp_path / "render"
    _render(["--apply"], env={"SPARKNET_WG_RENDER_DIR": str(render_dir)})
    leftovers = [p.name for p in render_dir.iterdir() if p.name != "wg0.conf"]
    assert not leftovers, f"stray temp files: {leftovers}"


# --- key hygiene ------------------------------------------------------------


def _fake_wg_bin(tmp_path: Path) -> Path:
    """A stand-in for wireguard-tools so keygen can be tested where wg is absent.

    Produces 32 random bytes base64-encoded, which is the shape `wg genkey`
    returns. This is a test double for the *tool*, never for the cryptography:
    the Ed25519 assertions below run against real `cryptography` output.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "wg"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  genkey) head -c 32 /dev/urandom | base64 ;;\n"
        "  pubkey) head -c 32 /dev/urandom | base64 ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    return bindir


def test_keygen_produces_ed25519_keypair_too(tmp_path: Path) -> None:
    """The header documents an Ed25519 keypair; it must actually produce one.

    Previously keygen only emitted WireGuard keys, so the documented output did
    not exist. That silently blocked the whole authorization path: no Master key
    meant no signed entry.
    """
    key_dir = tmp_path / "keys"
    proc = subprocess.run(
        [str(KEYGEN), "--role", "master"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SPARKNET_KEY_DIR": str(key_dir), "PATH": f"{_fake_wg_bin(tmp_path)}:{os.environ['PATH']}"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    priv = key_dir / "ed25519-master.key.b64"
    pub = key_dir / "ed25519-master.pub.b64"
    assert priv.is_file(), f"no Ed25519 private key produced; got {sorted(p.name for p in key_dir.iterdir())}"
    assert pub.is_file()


def test_keygen_ed25519_private_key_is_usable_by_the_signer(tmp_path: Path) -> None:
    """End-to-end: the key keygen writes must load in sparknet-authz.py.

    This is the interoperability check. keygen writes base64 text while
    `sign --master-key` used to assume base64 and its help text claimed raw
    bytes; the two disagreed and signing failed with a UnicodeDecodeError.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key_dir = tmp_path / "keys"
    proc = subprocess.run(
        [str(KEYGEN), "--role", "master"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SPARKNET_KEY_DIR": str(key_dir), "PATH": f"{_fake_wg_bin(tmp_path)}:{os.environ['PATH']}"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    raw_priv = base64.b64decode((key_dir / "ed25519-master.key.b64").read_text().strip())
    assert len(raw_priv) == 32, "Ed25519 private seed must decode to 32 bytes"
    raw_pub = base64.b64decode((key_dir / "ed25519-master.pub.b64").read_text().strip())
    assert len(raw_pub) == 32

    # The published public key must actually belong to the private key.
    derived = (
        Ed25519PrivateKey.from_private_bytes(raw_priv)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    assert derived == raw_pub, "published public key does not match the private key"


def test_keygen_private_key_permissions_are_tight(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    subprocess.run(
        [str(KEYGEN), "--role", "node"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SPARKNET_KEY_DIR": str(key_dir), "PATH": f"{_fake_wg_bin(tmp_path)}:{os.environ['PATH']}"},
    )
    priv = key_dir / "ed25519-node.key.b64"
    if not priv.is_file():
        pytest.skip("cryptography unavailable; no Ed25519 key produced")
    assert stat.S_IMODE(priv.stat().st_mode) == 0o600, f"private key mode is {oct(stat.S_IMODE(priv.stat().st_mode))}"
    assert stat.S_IMODE((key_dir / "ed25519-node.pub.b64").stat().st_mode) == 0o644, "public key should be readable"
    assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700


def test_keygen_never_writes_into_the_vault(tmp_path: Path) -> None:
    key_dir = tmp_path / "keys"
    subprocess.run(
        [str(KEYGEN), "--role", "node"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SPARKNET_KEY_DIR": str(key_dir), "PATH": f"{_fake_wg_bin(tmp_path)}:{os.environ['PATH']}"},
    )
    produced = {p.name for p in key_dir.iterdir()}
    assert produced, "keygen produced nothing"
    for name in produced:
        assert not (VAULT_ROOT / name).exists(), f"{name} leaked into the vault tree"


def test_keygen_requires_wg_binary(tmp_path: Path) -> None:
    """Without wireguard-tools we must fail loudly rather than fake a key."""
    key_dir = tmp_path / "keys"
    proc = subprocess.run(
        [str(KEYGEN), "--role", "node"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SPARKNET_KEY_DIR": str(key_dir)},
    )
    if shutil.which("wg") is None:
        assert proc.returncode != 0
        assert "wireguard-tools" in proc.stdout + proc.stderr
        assert not key_dir.exists(), "keygen must not create output when it cannot generate"
    else:
        # wg exists: it must succeed, and the key must land in the given dir
        # (outside the vault) rather than anywhere under version control.
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (key_dir / "wg-node.key").is_file()
        assert not (VAULT_ROOT / "keys").exists(), "keys must never be written into the vault tree"


def test_keygen_refuses_to_overwrite(tmp_path: Path) -> None:
    """Rotating a key silently would orphan every peer config."""
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    (key_dir / "wg-node.key").write_text("existing\n")
    proc = subprocess.run(
        [str(KEYGEN), "--role", "node"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SPARKNET_KEY_DIR": str(key_dir)},
    )
    assert proc.returncode != 0
    assert "already exists" in proc.stdout + proc.stderr
    assert (key_dir / "wg-node.key").read_text() == "existing\n"


def test_keygen_rejects_unknown_role(tmp_path: Path) -> None:
    proc = subprocess.run(
        [str(KEYGEN), "--role", "peer"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SPARKNET_KEY_DIR": str(tmp_path / "k")},
    )
    assert proc.returncode != 0
    assert "--role must be" in proc.stdout + proc.stderr


#: Directories excluded from the key-hygiene scan. node_modules carries base64
#: integrity hashes that look like keys and are not ours to police; the rest are
#: build output, state, or third-party material.
SCAN_EXCLUDE = {
    ".git",
    ".venv",
    ".state",
    ".cache",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    "references",
    "03_Sources",
    "dist",
    ".opencode",
}


def _vault_source_files() -> list[Path]:
    """Every source file in the vault, tracked or not.

    Scanning only `git ls-files` was a real gap: newly written files are
    untracked until committed, so the key-hygiene guarantee silently did not
    cover them. Scan the tree instead and exclude vendored/build directories.
    """
    files = {
        p
        for p in VAULT_ROOT.rglob("*")
        if p.is_file() and not any(part in SCAN_EXCLUDE for part in p.relative_to(VAULT_ROOT).parts)
    }
    # shared/ and contracts/ live outside this subvault but are part of what a
    # peer deploys, so they are in scope for the key scan.
    for root in (SHARED_DIR, FABRIC_ROOT / "contracts"):
        if root.is_dir():
            files.update(
                p
                for p in root.rglob("*")
                if p.is_file() and not any(part in SCAN_EXCLUDE for part in p.relative_to(root).parts)
            )
    return sorted(files)


def _scrub_envelope_signatures(text: str) -> str:
    """Drop the provenance envelope's `sig:` value before key-shape scanning.

    An Ed25519 signature is exactly the shape this detector is hunting for —
    base64 of 64 bytes — and `tracking.signature.sig` is the one place that
    blob is *supposed* to live. Scanning it would flag every signed note as a
    WireGuard private key. The field is stripped, not the file: a real key in
    the body or anywhere else still trips the detector.
    """
    return re.sub(
        r'^(\s*)sig: ["\']?[A-Za-z0-9+/]{80,}={0,2}["\']?\s*$',
        r"\1sig: <envelope-signature>",
        text,
        flags=re.M,
    )


def _tracked_files() -> list[Path]:
    """Only git-tracked files, for assertions that are specifically about commits."""
    proc = subprocess.run(
        ["git", "-C", str(VAULT_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("not a git repository")
    return [VAULT_ROOT / name for name in proc.stdout.split("\0") if name]


def test_key_scan_covers_untracked_files_too() -> None:
    """Guard against the scan regressing to git-tracked files only.

    New work is untracked by definition, so a scan that only reads commits would
    pass while a freshly written template held a real private key. `references/`
    and `.opencode/` are deliberately out of scope, so the check is on in-scope
    tracked files plus the files we actually author.
    """
    # Both sides are compared resolved: a legacy name is now a shim, so
    # `concepts/x.md` and `05_Knowledge/concepts/x.md` are one file, and only
    # the numbered path is on disk to scan.
    scanned = {p.resolve() for p in _vault_source_files()}

    # A tracked path that is not on disk carries no bytes to scan — that
    # happens only while a rename is staged but uncommitted, and the file is
    # scanned under its new path either way.
    in_scope_tracked = {
        p for p in _tracked_files()
        if p.is_file()
        and not any(part in SCAN_EXCLUDE for part in p.resolve().relative_to(VAULT_ROOT).parts)
    }
    hidden = sorted(str(p) for p in in_scope_tracked if p.resolve() not in scanned)
    assert not hidden, f"in-scope tracked files invisible to the scan: {hidden[:5]}"

    must_cover = [
        VAULT_ROOT / "configs" / "wireguard" / "wg0-node.conf.template",
        VAULT_ROOT / "scripts" / "sparknet-keygen.sh",
        VAULT_ROOT / "scripts" / "sparknet-wireguard-render.sh",
        VAULT_ROOT / "scripts" / "sparknet-wireguard-anchor.sh",
        SHARED_DIR / "sparknet-authz.py",
        VAULT_ROOT / "services" / "sparknet-guard-d.py",
        VAULT_ROOT / "manifests" / "node-authorizations.json",
    ]
    # resolve(): a path built as VAULT_ROOT / "../shared/x" keeps the ".."
    # segment and would never compare equal to the resolved scan results.
    missing = [str(p) for p in must_cover if p.resolve() not in {s.resolve() for s in scanned}]
    assert not missing, f"key scan is skipping authored files: {missing}"


def test_no_private_key_material_in_the_vault() -> None:
    """No 64-byte base64 blob (a WireGuard private key) anywhere in the source tree."""
    offenders: list[str] = []
    for path in _vault_source_files():
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for match in re.findall(r"[A-Za-z0-9+/]{86}==", _scrub_envelope_signatures(text)):
            if len(base64.b64decode(match)) == 64:
                offenders.append(str(path.relative_to(VAULT_ROOT)))
    assert not offenders, f"possible WireGuard private key in: {sorted(set(offenders))}"


def test_no_tracked_private_key_material_in_vault() -> None:
    """No 64-byte base64 blob (a WireGuard private key) in any tracked file."""
    offenders: list[str] = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for match in re.findall(r"[A-Za-z0-9+/]{86}==", _scrub_envelope_signatures(text)):
            if len(base64.b64decode(match)) == 64:
                offenders.append(str(path.relative_to(VAULT_ROOT)))
    assert not offenders, f"possible WireGuard private key in: {sorted(set(offenders))}"


def test_templates_contain_no_placeholder_key() -> None:
    """Any source file naming a key must hold a placeholder or a public key only."""
    for path in _vault_source_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("PrivateKey") and not stripped.startswith("#"):
                assert "${" in stripped or "redacted" in stripped, (
                    f"{path.relative_to(VAULT_ROOT)}:{lineno} looks like a literal private key: {stripped}"
                )


def test_state_dir_is_gitignored() -> None:
    """Rendered keys land under .state/, which must never be committed."""
    ignored = (VAULT_ROOT / ".gitignore").read_text()
    assert ".state/" in ignored


def test_render_dir_defaults_inside_vault_state() -> None:
    """Never render into the vault root or /tmp by accident."""
    text = RENDER_SCRIPT.read_text()
    assert "SPARKNET_WG_RENDER_DIR:-$SPARKNET_STATE_DIR/wireguard" in text


def test_wg_key_shape() -> None:
    """Sanity: a 32-byte key base64-encodes to the 44-char form wg expects."""
    assert len(base64.b64encode(bytes(32))) == 44
    assert WG_KEY_RE.match(FAKE_PRIVATE_KEY)
